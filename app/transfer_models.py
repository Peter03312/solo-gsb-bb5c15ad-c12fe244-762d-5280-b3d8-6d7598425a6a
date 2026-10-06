"""双容器转移预检（v2）请求 / 响应数据模型（Pydantic v2）。

与单容器预检（models.py）并列：
- 一次请求描述编号互异的两只容器（各有材质与初始液体）、统一温度；
- 步骤仅含 transfer（定量转移）与 hold（静置），同一时刻按 seq 顺序执行；
- 时间必须非负且单调不回退（沿 (at_seconds, seq) 的执行序检查）。
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .models import ComponentIn, ComponentState, ContactAllowanceIn, Finding, _Strict


class VesselIn(_Strict):
    vessel_id: str = Field(..., min_length=1, description="容器编号，两只容器须互异（非空字符串）")
    material: str = Field(..., description="容器材质编号，见 /catalog（如 glass/hdpe/ptfe/aluminum）")
    initial_liquid: list[ComponentIn] = Field(default_factory=list, description="该容器初始液体；同一试剂不得重复出现")


class TransferStepIn(_Strict):
    seq: int = Field(..., description="步骤序号，正整数，整个流程内不得重复；同一时刻按 seq 顺序执行")
    op: Literal["transfer", "hold"] = Field(..., description="transfer=定量转移并充分混合, hold=静置等待")
    from_vessel: str | None = Field(None, description="op=transfer 必填：源容器编号")
    to_vessel: str | None = Field(None, description="op=transfer 必填：目标容器编号")
    volume_ml: float | None = Field(None, description="op=transfer 必填：转移体积（毫升，须 > 0，不得超过源容器当前液体体积）")
    at_seconds: float = Field(..., description="该步骤时刻（秒，相对流程起点），须非负且不早于执行序上一步")
    note: str | None = Field(None, description="可选备注，不参与计算")


class TransferPrecheckRequest(_Strict):
    vessels: list[VesselIn] = Field(..., description="两只容器，编号互异")
    temperature_c: float = Field(..., description="统一体系温度（摄氏度），允许范围 -50 ~ 300")
    steps: list[TransferStepIn] = Field(default_factory=list, description="按 (at_seconds, seq) 执行的转移 / 静置步骤")
    contact_allowances: list[ContactAllowanceIn] = Field(
        default_factory=list, description="可选：为某些反应对声明允许接触时长（覆盖规则默认值），对两只容器同时生效"
    )


# ---------------------------------------------------------------------------
# 响应
# ---------------------------------------------------------------------------
class VesselState(_Strict):
    vessel_id: str
    material: str
    liquid_volume_ml: float
    components: list[ComponentState]


class TransferStepTrace(_Strict):
    seq: int
    op: str
    at_seconds: float
    from_vessel: str | None = None
    to_vessel: str | None = None
    volume_ml: float | None = None
    note: str | None = None
    vessels: list[VesselState] = Field(..., description="该步完成后两只容器的状态（按请求中容器顺序）")


class TransferFinding(Finding):
    vessel_id: str | None = Field(..., description="风险所在容器编号；试剂-试剂共存时为承载容器")


class TransferPrecheckResponse(_Strict):
    safe: bool
    blocked: bool
    first_blocking_seq: int | None
    ruleset_version: str
    temperature_c: float
    findings: list[TransferFinding]
    vessels: list[VesselState] = Field(..., description="初始状态（seq=0 风险检查时点），按请求中容器顺序")
    steps: list[TransferStepTrace]
    final_states: list[VesselState] = Field(..., description="末态，按请求中容器顺序")
