"""逐步混配状态机。

状态模型（简化、透明的体积-浓度簿记）：
- 每个试剂记录“纯物体积” pure_ml；当前浓度 = pure_ml / 液体总体积。
- add:  pure_ml += conc * v / 100，总体积 += v
- dilute: 仅总体积 += v（加纯水）
- pour: 按倒出比例 f 同步缩减总体积与每个 pure_ml（均匀混合假设）
- clean: 清空全部状态，接触计时归零

每个实体对（试剂-试剂 / 试剂-材质）的每条匹配规则维护一个“接触片段”：
条件满足期间计时；超时在时间推进到下一个操作点时检查。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .catalog import MATERIALS, REAGENTS, material_token, reagent_tags
from .rules import RULES, Rule
from .validator import Plan, PlannedStep

_EPS = 1e-9


# ---------------------------------------------------------------------------
# 实体与规则匹配
# ---------------------------------------------------------------------------
def _entity_kind(tok: str) -> str:
    return "material" if tok.startswith("material:") else "reagent"


def _entity_tags(tok: str) -> frozenset[str]:
    if tok.startswith("material:"):
        return frozenset((tok,))
    return reagent_tags(tok)


def _side_matches(side, tok: str, concentration: float) -> bool:
    kind = _entity_kind(tok)
    selectors = side.selectors
    if any(s.startswith("material:") for s in selectors):
        if kind != "material":
            return False
    else:
        if kind != "reagent":
            return False
    if kind == "reagent" and concentration < side.min_concentration - _EPS:
        return False
    return len(selectors & _entity_tags(tok)) > 0


@dataclass
class _RuleEpisode:
    rule_id: str
    start: float          # 本接触片段开始时刻
    reported: bool = False  # 该片段内是否已出过超时 finding


@dataclass
class _PairState:
    a: str
    b: str


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
@dataclass
class _State:
    vessel_material: str
    temperature_c: float
    total_ml: float = 0.0
    pure_ml: dict[str, float] = field(default_factory=dict)
    cleaned: bool = False
    now: float = 0.0
    # pair_key -> {rule_id: episode}
    contacts: dict[tuple[str, str], dict[str, _RuleEpisode]] = field(default_factory=dict)


class Engine:
    def __init__(self, plan: Plan):
        self.plan = plan
        self.mat_tok = material_token(plan.vessel_material)
        self.state = _State(vessel_material=plan.vessel_material, temperature_c=plan.temperature_c)
        self.findings: list[dict] = []
        self.traces: list[dict] = []

    # ---------------- 浓度与组分 ----------------
    def _concentrations(self) -> dict[str, float]:
        s = self.state
        if s.total_ml <= _EPS:
            return {}
        return {r: p / s.total_ml * 100.0 for r, p in s.pure_ml.items() if p > _EPS}

    def _snapshot_components(self) -> list[dict]:
        conc = self._concentrations()
        out = []
        for rid in sorted(conc):
            out.append(
                {
                    "reagent_id": rid,
                    "name": REAGENTS[rid].name,
                    "concentration": round(conc[rid], 4),
                    "pure_volume_ml": round(self.state.pure_ml[rid], 4),
                }
            )
        return out

    def _trace(self, seq: int | None, op: str) -> dict:
        s = self.state
        return {
            "seq": seq,
            "op": op,
            "at_seconds": round(s.now, 4),
            "liquid_volume_ml": round(s.total_ml, 4),
            "cleaned": s.cleaned,
            "components": self._snapshot_components(),
        }

    # ---------------- 实体对与规则 ----------------
    def _active_pairs(self) -> dict[tuple[str, str], dict[str, float]]:
        """返回当前存活的实体对及各实体当前浓度（材质侧记 inf 以通过阈值检查）。"""
        s = self.state
        conc = self._concentrations()
        present = sorted(conc.keys())
        pairs: dict[tuple[str, str], dict[str, float]] = {}

        # 试剂-试剂
        for i in range(len(present)):
            for j in range(i + 1, len(present)):
                a, b = present[i], present[j]
                pairs[(a, b)] = {a: conc[a], b: conc[b]}
        # 试剂-材质（容器内有液体时）
        if s.total_ml > _EPS:
            for r in present:
                key = tuple(sorted((r, self.mat_tok)))
                pairs[key] = {r: conc[r], self.mat_tok: float("inf")}
        return pairs

    def _match_rule(self, rule: Rule, a: str, b: str, ca: float, cb: float):
        """返回 ((side_a 上的实体, side_b 上的实体), (side_a 浓度, side_b 浓度))，不匹配返回 None。"""
        if _side_matches(rule.side_a, a, ca) and _side_matches(rule.side_b, b, cb):
            return (a, b), (ca, cb)
        if _side_matches(rule.side_a, b, cb) and _side_matches(rule.side_b, a, ca):
            return (b, a), (cb, ca)
        return None

    def _rules_for_pair(self, key: tuple[str, str], pair_concs: dict[str, float]):
        a, b = key
        matched = []
        for rule in RULES:
            orient = self._match_rule(rule, a, b, pair_concs[a], pair_concs[b])
            if orient is not None:
                (ta, tb), (cc_a, cc_b) = orient
                matched.append((rule, (ta, tb), (cc_a, cc_b)))
        return matched

    # ---------------- 接触对账 ----------------
    def _reconcile(self, seq: int | None) -> None:
        """在操作完成后：建立/终止接触片段，立即类规则在片段首次建立时触发。"""
        s = self.state
        active = self._active_pairs()

        # 清理不再存活的对
        for key in list(s.contacts.keys()):
            if key not in active:
                del s.contacts[key]

        for key, pair_concs in active.items():
            episodes = s.contacts.setdefault(key, {})
            matched = self._rules_for_pair(key, pair_concs)
            matched_ids = {r.id for r, _, _ in matched}

            # 阈值跌破 -> 结束该规则的片段
            for rid in list(episodes.keys()):
                if rid not in matched_ids:
                    del episodes[rid]

            for rule, orient, concs in matched:
                is_new_episode = rule.id not in episodes
                if is_new_episode:
                    episodes[rule.id] = _RuleEpisode(rule_id=rule.id, start=s.now)

                # 立即禁配 / 立即材质不相容：片段首次建立即触发
                if is_new_episode and rule.max_contact_seconds is None:
                    self._emit(seq, rule, orient, concs)

    # ---------------- 超时检查 ----------------
    def _check_overtime(self, seq: int | None) -> None:
        s = self.state
        active = self._active_pairs()
        for key, episodes in s.contacts.items():
            pair_concs = active.get(key)
            if pair_concs is None:
                continue
            matched = {r.id: (r, orient, concs) for r, orient, concs in self._rules_for_pair(key, pair_concs)}
            for rid, ep in episodes.items():
                if ep.reported:
                    continue
                rule, orient, concs = matched.get(rid, (None, None, None))
                if rule is None or rule.max_contact_seconds is None:
                    continue
                allowed = self._effective_limit(rule, key)
                elapsed = s.now - ep.start
                if elapsed > allowed + _EPS:
                    ep.reported = True
                    self._emit(
                        seq,
                        rule,
                        orient,
                        concs,
                        contact_seconds=elapsed,
                        allowed_seconds=allowed,
                    )

    def _effective_limit(self, rule: Rule, key: tuple[str, str]) -> float:
        al = self.plan.allowances.get(key)
        if al is not None:
            return al
        assert rule.max_contact_seconds is not None
        return rule.max_contact_seconds

    # ---------------- 输出 finding ----------------
    def _emit(
        self,
        seq: int | None,
        rule: Rule,
        orient: tuple[str, str],
        concs: tuple[float, float],
        *,
        contact_seconds: float | None = None,
        allowed_seconds: float | None = None,
    ) -> None:
        ta, tb = orient
        ca, cb = concs
        if rule.kind == "forbidden_mix":
            category = "forbidden_mix"
        elif rule.kind == "limited_contact":
            category = "overtime_contact"
        else:
            category = "material_incompatible" if rule.max_contact_seconds is None else "overtime_contact"

        concentrations: dict[str, float] = {}
        for tok, c in ((ta, ca), (tb, cb)):
            if not tok.startswith("material:"):
                concentrations[tok] = round(c, 4)

        if category == "forbidden_mix":
            br = f"禁配组合：{rule.name}（{rule.id}）在步骤 {seq} 首次共存"
        elif category == "material_incompatible":
            br = f"材质不相容：{rule.name}（{rule.id}），{MATERIALS[self.plan.vessel_material].name} 容器不得盛装该试剂"
        else:
            br = (
                f"超时接触：{rule.name}（{rule.id}），实际接触 {round(contact_seconds, 1)}s "
                f"超过允许 {round(allowed_seconds, 1) if allowed_seconds is not None else None}s"
            )

        temp = self.plan.temperature_c
        if rule.temp_note_threshold is not None and temp >= rule.temp_note_threshold and rule.temp_note:
            br += f"；当前 {temp:g}℃ —— {rule.temp_note}"

        entities = [ta, tb]
        self.findings.append(
            {
                "seq": seq,
                "category": category,
                "rule_id": rule.id,
                "rule_name": rule.name,
                "entities": entities,
                "concentrations": concentrations,
                "contact_seconds": round(contact_seconds, 4) if contact_seconds is not None else None,
                "allowed_seconds": round(allowed_seconds, 4) if allowed_seconds is not None else None,
                "severity": rule.severity,
                "reason": rule.reason,
                "guidance": rule.guidance,
                "blocking_reason": br,
            }
        )

    # ---------------- 操作 ----------------
    def _apply(self, st: PlannedStep) -> None:
        s = self.state
        s.now = st.at_seconds
        if st.op == "add":
            rid = st.reagent_id
            s.cleaned = False
            s.pure_ml[rid] = s.pure_ml.get(rid, 0.0) + st.concentration * st.volume_ml / 100.0  # type: ignore[operator]
            s.total_ml += st.volume_ml  # type: ignore[operator]
        elif st.op == "dilute":
            s.total_ml += st.volume_ml  # type: ignore[operator]
        elif st.op == "pour":
            f = st.volume_ml / s.total_ml  # type: ignore[operator]
            s.total_ml -= st.volume_ml  # type: ignore[operator]
            for rid in list(s.pure_ml.keys()):
                s.pure_ml[rid] *= 1.0 - f
                if s.pure_ml[rid] < _EPS:
                    del s.pure_ml[rid]
            if s.total_ml <= _EPS:
                s.total_ml = 0.0
                s.pure_ml.clear()
        elif st.op == "clean":
            s.total_ml = 0.0
            s.pure_ml.clear()
            s.cleaned = True
            s.contacts.clear()
        # hold：仅推进时间

    # ---------------- 主流程 ----------------
    def run(self) -> dict:
        # 初始液体
        for rid, conc, vol in self.plan.initial:
            self.state.pure_ml[rid] = self.state.pure_ml.get(rid, 0.0) + conc * vol / 100.0
            self.state.total_ml += vol
        if self.plan.initial:
            self.state.cleaned = False
        self._reconcile(seq=0 if self.plan.initial else None)
        self.traces.append(self._trace(None, "initial"))

        for st in self.plan.steps:
            # 1) 时间推进 -> 检查已建立接触是否超时
            self.state.now = st.at_seconds
            self._check_overtime(st.seq)
            # 2) 执行操作
            self._apply(st)
            # 3) 对账新状态（含立即类规则首次触发）
            self._reconcile(seq=st.seq)
            self.traces.append(self._trace(st.seq, st.op))

        # 结果稳定排序：seq（None 视为 0），同 seq 保持发现顺序
        order = {"forbidden_mix": 0, "material_incompatible": 1, "overtime_contact": 2}
        self.findings.sort(key=lambda f: (f["seq"] if f["seq"] is not None else 0, order[f["category"]]))

        first_blocking = self.findings[0]["seq"] if self.findings else None
        final_state = self.traces[-1]
        return {
            "findings": self.findings,
            "traces": self.traces,
            "first_blocking_seq": first_blocking,
            "final_state": final_state,
        }


def simulate(plan: Plan) -> dict[str, Any]:
    return Engine(plan).run()
