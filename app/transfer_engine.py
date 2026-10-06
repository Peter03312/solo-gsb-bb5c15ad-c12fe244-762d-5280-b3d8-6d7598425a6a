"""双容器转移逐步状态机。

沿用单容器引擎（engine.py）的规则匹配、纯物体积簿记与接触片段语义，
区别在于同时维护两只容器的独立状态（组分、体积、接触计时）。

transfer（充分混合假设）：
- 源容器各组分按转移比例 f = volume / total_ml 扣减纯物体积，总体积减少；
- 扣减量计入目标容器对应组分（同试剂合并纯物量），总体积增加；
- 源容器完全倒空时终止其全部接触片段。

接触计时（在每只容器内独立维护）：
- 转移后两侧既有且双方仍达阈值的接触片段沿用原开始时刻继续计时；
- 仅新达阈值的接触（含目标容器内新形成的共存）从转入时刻起计为新片段；
- 任一方跌破阈值（或源被倒空）即结束片段，重新满足则重新计时。

每步：先把时间推进到 at_seconds 并结算两只容器“此前”的超时接触，
再执行操作，然后对账新状态（即时禁配 / 即时材质不相容在片段首次建立时触发）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .catalog import MATERIALS, REAGENTS
from .engine import _RuleEpisode, match_rule
from .rules import RULES, Rule
from .transfer_validator import TransferPlan

_EPS = 1e-9


@dataclass
class _VesselState:
    vid: str
    material: str
    total_ml: float = 0.0
    pure_ml: dict[str, float] = field(default_factory=dict)
    # pair_key -> {rule_id: episode}
    contacts: dict[tuple[str, str], dict[str, _RuleEpisode]] = field(default_factory=dict)


class TransferEngine:
    def __init__(self, plan: TransferPlan):
        self.plan = plan
        self.vessels: list[_VesselState] = []
        for pv in plan.vessels:
            vs = _VesselState(vid=pv.vessel_id, material=pv.material)
            for rid, conc, vol in pv.initial:
                vs.pure_ml[rid] = vs.pure_ml.get(rid, 0.0) + conc * vol / 100.0
                vs.total_ml += vol
            self.vessels.append(vs)
        self.by_id = {vs.vid: vs for vs in self.vessels}
        self.now = 0.0
        self.findings: list[dict] = []
        self.step_traces: list[dict] = []

    # ---------------- 浓度与快照 ----------------
    def _concentrations(self, vs: _VesselState) -> dict[str, float]:
        if vs.total_ml <= _EPS:
            return {}
        return {r: p / vs.total_ml * 100.0 for r, p in vs.pure_ml.items() if p > _EPS}

    def _snapshot_components(self, vs: _VesselState) -> list[dict]:
        conc = self._concentrations(vs)
        return [
            {
                "reagent_id": rid,
                "name": REAGENTS[rid].name,
                "concentration": round(conc[rid], 4),
                "pure_volume_ml": round(vs.pure_ml[rid], 4),
            }
            for rid in sorted(conc)
        ]

    def _snapshot_vessel(self, vs: _VesselState) -> dict:
        return {
            "vessel_id": vs.vid,
            "material": vs.material,
            "liquid_volume_ml": round(vs.total_ml, 4),
            "components": self._snapshot_components(vs),
        }

    def _snapshot_all(self) -> list[dict]:
        return [self._snapshot_vessel(vs) for vs in self.vessels]

    # ---------------- 实体对与规则 ----------------
    def _active_pairs(self, vs: _VesselState) -> dict[tuple[str, str], dict[str, float]]:
        conc = self._concentrations(vs)
        present = sorted(conc.keys())
        pairs: dict[tuple[str, str], dict[str, float]] = {}
        for i in range(len(present)):
            for j in range(i + 1, len(present)):
                a, b = present[i], present[j]
                pairs[(a, b)] = {a: conc[a], b: conc[b]}
        mat_tok = f"material:{vs.material}"
        if vs.total_ml > _EPS:
            for r in present:
                key = tuple(sorted((r, mat_tok)))
                pairs[key] = {r: conc[r], mat_tok: float("inf")}
        return pairs

    def _rules_for_pair(self, key: tuple[str, str], pair_concs: dict[str, float]):
        a, b = key
        matched = []
        for rule in RULES:
            orient = match_rule(rule, a, b, pair_concs[a], pair_concs[b])
            if orient is not None:
                (ta, tb), (cc_a, cc_b) = orient
                matched.append((rule, (ta, tb), (cc_a, cc_b)))
        return matched

    # ---------------- 接触对账 ----------------
    def _reconcile(self, vs: _VesselState, seq: int) -> None:
        """操作完成后：建立/终止该容器的接触片段；立即类规则在片段首次建立时触发。"""
        active = self._active_pairs(vs)

        for key in list(vs.contacts.keys()):
            if key not in active:
                del vs.contacts[key]

        for key, pair_concs in active.items():
            episodes = vs.contacts.setdefault(key, {})
            matched = self._rules_for_pair(key, pair_concs)
            matched_ids = {r.id for r, _, _ in matched}

            # 阈值跌破 / 实体不再共存 -> 结束该规则的片段
            for rid in list(episodes.keys()):
                if rid not in matched_ids:
                    del episodes[rid]

            for rule, orient, concs in matched:
                is_new_episode = rule.id not in episodes
                if is_new_episode:
                    episodes[rule.id] = _RuleEpisode(rule_id=rule.id, start=self.now)
                # 立即禁配 / 立即材质不相容：片段首次建立即触发
                if is_new_episode and rule.max_contact_seconds is None:
                    self._emit(vs, seq, rule, orient, concs)

    # ---------------- 超时检查 ----------------
    def _check_overtime(self, vs: _VesselState, seq: int) -> None:
        active = self._active_pairs(vs)
        for key, episodes in vs.contacts.items():
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
                elapsed = self.now - ep.start
                if elapsed > allowed + _EPS:
                    ep.reported = True
                    self._emit(vs, seq, rule, orient, concs, contact_seconds=elapsed, allowed_seconds=allowed)

    def _effective_limit(self, rule: Rule, key: tuple[str, str]) -> float:
        al = self.plan.allowances.get(key)
        if al is not None:
            return al
        assert rule.max_contact_seconds is not None
        return rule.max_contact_seconds

    # ---------------- finding ----------------
    def _emit(
        self,
        vs: _VesselState,
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

        concentrations = {
            tok: round(c, 4)
            for tok, c in ((ta, ca), (tb, cb))
            if not tok.startswith("material:")
        }

        when = f"步骤 {seq} " if seq else "初始状态（步骤 0）"
        if category == "forbidden_mix":
            br = f"禁配组合：{rule.name}（{rule.id}）在容器 {vs.vid} 内，{when}首次共存"
        elif category == "material_incompatible":
            br = (
                f"材质不相容：{rule.name}（{rule.id}），容器 {vs.vid}"
                f"（{MATERIALS[vs.material].name}）不得盛装该试剂（{when.strip()}）"
            )
        else:
            br = (
                f"超时接触：{rule.name}（{rule.id}），容器 {vs.vid} 内实际接触 "
                f"{round(contact_seconds, 1)}s 超过允许 "
                f"{round(allowed_seconds, 1) if allowed_seconds is not None else None}s（{when.strip()}）"
            )

        temp = self.plan.temperature_c
        if rule.temp_note_threshold is not None and temp >= rule.temp_note_threshold and rule.temp_note:
            br += f"；当前 {temp:g}℃ —— {rule.temp_note}"

        self.findings.append(
            {
                "seq": seq if seq is not None else 0,
                "vessel_id": vs.vid,
                "category": category,
                "rule_id": rule.id,
                "rule_name": rule.name,
                "entities": [ta, tb],
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
    def _apply_transfer(self, st) -> None:
        src = self.by_id[st.from_vessel]
        dst = self.by_id[st.to_vessel]
        f = st.volume_ml / src.total_ml

        moved: dict[str, float] = {}
        for rid, pure in list(src.pure_ml.items()):
            take = pure * f
            moved[rid] = take
            left = pure - take
            if left <= _EPS:
                del src.pure_ml[rid]
            else:
                src.pure_ml[rid] = left
        src.total_ml -= st.volume_ml
        if src.total_ml <= _EPS:
            src.total_ml = 0.0
            src.pure_ml.clear()
            src.contacts.clear()  # 完全倒空：终止全部接触片段

        for rid, take in moved.items():
            dst.pure_ml[rid] = dst.pure_ml.get(rid, 0.0) + take
        dst.total_ml += st.volume_ml

    # ---------------- 主流程 ----------------
    def run(self) -> dict[str, Any]:
        # 初始状态也检查风险（seq=0）
        for vs in self.vessels:
            self._reconcile(vs, seq=0)
        initial_states = self._snapshot_all()

        for st in self.plan.steps:
            # 1) 时间推进 -> 先结算两只容器“此前”的超时接触
            self.now = st.at_seconds
            for vs in self.vessels:
                self._check_overtime(vs, st.seq)
            # 2) 执行操作
            if st.op == "transfer":
                self._apply_transfer(st)
                # 3) 对账新状态（仅源/目标发生变化）
                self._reconcile(self.by_id[st.from_vessel], st.seq)
                self._reconcile(self.by_id[st.to_vessel], st.seq)
            # hold：仅推进时间，无新对账
            self.step_traces.append(
                {
                    "seq": st.seq,
                    "op": st.op,
                    "at_seconds": round(self.now, 4),
                    "from_vessel": st.from_vessel,
                    "to_vessel": st.to_vessel,
                    "volume_ml": round(st.volume_ml, 4) if st.volume_ml is not None else None,
                    "note": st.note,
                    "vessels": self._snapshot_all(),
                }
            )

        # 稳定排序：seq，类别（禁配 → 材质 → 超时），容器声明顺序；同序保持发现顺序
        order = {"forbidden_mix": 0, "material_incompatible": 1, "overtime_contact": 2}
        vid_order = {vs.vid: i for i, vs in enumerate(self.vessels)}
        self.findings.sort(key=lambda f: (f["seq"], order[f["category"]], vid_order[f["vessel_id"]]))

        first_blocking = self.findings[0]["seq"] if self.findings else None
        return {
            "findings": self.findings,
            "initial_states": initial_states,
            "step_traces": self.step_traces,
            "final_states": self._snapshot_all(),
            "first_blocking_seq": first_blocking,
        }


def simulate_transfer(plan: TransferPlan) -> dict[str, Any]:
    return TransferEngine(plan).run()
