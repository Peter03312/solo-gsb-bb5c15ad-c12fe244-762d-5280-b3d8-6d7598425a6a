"""双容器转移预检状态机。

两只容器各维护一个 VesselRuntime（独立液相、材质界面与接触计时），
步骤按时刻推进、同刻按 seq 顺序执行：

1. 时间先推进到本步时刻，结算两侧此前已建立接触的超时；
2. transfer：源容器按充分混合比例扣减各组分（部分转出浓度不变），
   同一份纯物量与体积计入目标容器；两侧各自对账接触片段；
3. hold：仅推进时间；
4. 初始状态（t=0）同样检查风险。

两侧既有且仍达阈值的接触计时继续，仅新达阈值的接触从转入/本刻起计时，
跌破阈值（含源容器被转空）则结束片段。
"""
from __future__ import annotations

from typing import Any

from .catalog import MATERIALS
from .rules import Rule
from .runtime import RuleEvent, VesselRuntime
from .transfer_validator import TransferPlan

_EPS = 1e-9


class TwoVesselEngine:
    def __init__(self, plan: TransferPlan):
        self.plan = plan
        self.runtimes: list[VesselRuntime] = [
            VesselRuntime(vessel_id=spec.vessel_id, vessel_material=spec.vessel_material) for spec in plan.vessels
        ]
        self.by_id: dict[str, VesselRuntime] = {rt.vessel_id: rt for rt in self.runtimes}
        self.findings: list[dict] = []
        self.traces: list[dict] = []

    # ---------------- 快照 ----------------
    def _vessel_states(self) -> list[dict]:
        return [
            {
                "vessel_id": rt.vessel_id,
                "vessel_material": rt.vessel_material,
                **rt.snapshot(),
            }
            for rt in self.runtimes
        ]

    def _trace(self, seq: int | None, op: str, step=None) -> dict:
        return {
            "seq": seq,
            "op": op,
            "at_seconds": round(self.runtimes[0].state.now, 4),
            "from_vessel": step.from_vessel if step is not None else None,
            "to_vessel": step.to_vessel if step is not None else None,
            "volume_ml": round(step.volume_ml, 4) if step is not None and step.volume_ml is not None else None,
            "vessels": self._vessel_states(),
        }

    # ---------------- 规则事件 -> finding ----------------
    def _effective_limit(self, rule: Rule, key: tuple[str, str]) -> float:
        al = self.plan.allowances.get(key)
        if al is not None:
            return al
        assert rule.max_contact_seconds is not None
        return rule.max_contact_seconds

    def _emit(self, rt: VesselRuntime, seq: int | None, ev: RuleEvent) -> None:
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
            br = f"容器 {rt.vessel_id}：禁配组合：{rule.name}（{rule.id}）在步骤 {seq} 首次共存"
        elif category == "material_incompatible":
            br = (
                f"容器 {rt.vessel_id}：材质不相容：{rule.name}（{rule.id}），"
                f"{MATERIALS[rt.vessel_material].name} 容器不得盛装该试剂"
            )
        else:
            br = (
                f"容器 {rt.vessel_id}：超时接触：{rule.name}（{rule.id}），实际接触 "
                f"{round(ev.contact_seconds, 1)}s 超过允许 "
                f"{round(ev.allowed_seconds, 1) if ev.allowed_seconds is not None else None}s"
            )

        temp = self.plan.temperature_c
        if rule.temp_note_threshold is not None and temp >= rule.temp_note_threshold and rule.temp_note:
            br += f"；当前 {temp:g}℃ —— {rule.temp_note}"

        self.findings.append(
            {
                "seq": seq,
                "vessel_id": rt.vessel_id,
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

    def _reconcile(self, rt: VesselRuntime, seq: int | None) -> None:
        for ev in rt.reconcile():
            self._emit(rt, seq, ev)

    def _check_overtime(self, rt: VesselRuntime, seq: int | None) -> None:
        for ev in rt.check_overtime(self._effective_limit):
            self._emit(rt, seq, ev)

    # ---------------- 主流程 ----------------
    def run(self) -> dict[str, Any]:
        # 初始液体（统一 t=0）：初始状态也检查风险
        for rt, spec in zip(self.runtimes, self.plan.vessels):
            rt.load_initial(spec.initial)
        for rt in self.runtimes:
            self._reconcile(rt, seq=0)
        self.traces.append(self._trace(None, "initial"))

        for st in self.plan.steps:
            # 1) 时间推进 -> 先结算两侧此前超时
            for rt in self.runtimes:
                rt.advance(st.at_seconds)
            for rt in self.runtimes:
                self._check_overtime(rt, st.seq)

            # 2) 执行操作
            if st.op == "transfer":
                src = self.by_id[st.from_vessel]  # type: ignore[index]
                dst = self.by_id[st.to_vessel]  # type: ignore[index]
                moved = src.transfer_out(st.volume_ml)  # type: ignore[arg-type]
                dst.transfer_in(moved, st.volume_ml)  # type: ignore[arg-type]

            # 3) 两侧对账（先源后目标；同刻按 seq 已定序，同一容器内按规则顺序确定）
            if st.op == "transfer":
                self._reconcile(src, st.seq)
                self._reconcile(dst, st.seq)
            else:
                for rt in self.runtimes:
                    self._reconcile(rt, st.seq)

            self.traces.append(self._trace(st.seq, st.op, step=st))

        # 稳定排序：seq（None 视为 0） -> 类别 -> 容器提交顺序
        category_order = {"forbidden_mix": 0, "material_incompatible": 1, "overtime_contact": 2}
        vessel_order = {rt.vessel_id: i for i, rt in enumerate(self.runtimes)}
        self.findings.sort(
            key=lambda f: (
                f["seq"] if f["seq"] is not None else 0,
                category_order[f["category"]],
                vessel_order[f["vessel_id"]],
            )
        )

        first = self.findings[0] if self.findings else None
        final_states = self._vessel_states()
        return {
            "findings": self.findings,
            "traces": self.traces,
            "final_states": final_states,
            "first_blocking_seq": first["seq"] if first else None,
            "first_blocking_vessel_id": first["vessel_id"] if first else None,
        }


def simulate_transfer(plan: TransferPlan) -> dict[str, Any]:
    return TwoVesselEngine(plan).run()
