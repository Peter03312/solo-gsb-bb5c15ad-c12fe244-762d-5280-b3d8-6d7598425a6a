"""单容器逐步混配状态机（现有单容器预检）。

状态与接触计时的逐容器逻辑见 app.runtime；本模块只负责重放执行计划、
把规则事件翻译为带阻断原因的 findings，并生成逐步/最终快照。
"""
from __future__ import annotations

from typing import Any

from .catalog import MATERIALS
from .rules import Rule
from .runtime import RuleEvent, VesselRuntime
from .validator import Plan, PlannedStep

_EPS = 1e-9


class Engine:
    def __init__(self, plan: Plan):
        self.plan = plan
        self.runtime = VesselRuntime(
            vessel_id="vessel", vessel_material=plan.vessel_material
        )
        self.findings: list[dict] = []
        self.traces: list[dict] = []

    # ---------------- 快照 ----------------
    def _trace(self, seq: int | None, op: str) -> dict:
        snap = self.runtime.snapshot()
        return {
            "seq": seq,
            "op": op,
            "at_seconds": round(self.runtime.state.now, 4),
            **snap,
        }

    # ---------------- 规则事件 -> finding ----------------
    def _effective_limit(self, rule: Rule, key: tuple[str, str]) -> float:
        al = self.plan.allowances.get(key)
        if al is not None:
            return al
        assert rule.max_contact_seconds is not None
        return rule.max_contact_seconds

    def _emit(self, seq: int | None, ev: RuleEvent) -> None:
        rule = ev.rule
        ta, tb = ev.orient
        ca, cb = ev.concs
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
                f"超时接触：{rule.name}（{rule.id}），实际接触 {round(ev.contact_seconds, 1)}s "
                f"超过允许 {round(ev.allowed_seconds, 1) if ev.allowed_seconds is not None else None}s"
            )

        temp = self.plan.temperature_c
        if rule.temp_note_threshold is not None and temp >= rule.temp_note_threshold and rule.temp_note:
            br += f"；当前 {temp:g}℃ —— {rule.temp_note}"

        self.findings.append(
            {
                "seq": seq,
                "category": category,
                "rule_id": rule.id,
                "rule_name": rule.name,
                "entities": [ta, tb],
                "concentrations": concentrations,
                "contact_seconds": round(ev.contact_seconds, 4) if ev.contact_seconds is not None else None,
                "allowed_seconds": round(ev.allowed_seconds, 4) if ev.allowed_seconds is not None else None,
                "severity": rule.severity,
                "reason": rule.reason,
                "guidance": rule.guidance,
                "blocking_reason": br,
            }
        )

    # ---------------- 操作 ----------------
    def _apply(self, st: PlannedStep) -> None:
        rt = self.runtime
        if st.op == "add":
            rt.apply_add(st.reagent_id, st.concentration, st.volume_ml)  # type: ignore[arg-type]
        elif st.op == "dilute":
            rt.apply_dilute(st.volume_ml)  # type: ignore[arg-type]
        elif st.op == "pour":
            rt.apply_pour(st.volume_ml)  # type: ignore[arg-type]
        elif st.op == "clean":
            rt.apply_clean()
        # hold：仅推进时间

    # ---------------- 主流程 ----------------
    def run(self) -> dict:
        rt = self.runtime

        # 初始液体
        rt.load_initial(self.plan.initial)
        for ev in rt.reconcile():
            self._emit(0 if self.plan.initial else None, ev)
        self.traces.append(self._trace(None, "initial"))

        for st in self.plan.steps:
            # 1) 时间推进 -> 检查已建立接触是否超时
            rt.advance(st.at_seconds)
            for ev in rt.check_overtime(self._effective_limit):
                self._emit(st.seq, ev)
            # 2) 执行操作
            self._apply(st)
            # 3) 对账新状态（含立即类规则首次触发）
            for ev in rt.reconcile():
                self._emit(st.seq, ev)
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
