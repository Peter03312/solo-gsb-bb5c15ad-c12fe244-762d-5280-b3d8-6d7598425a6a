"""请求 / 响应数据模型（Pydantic v2）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Operation = Literal["add", "dilute", "pour", "clean", "hold"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ComponentIn(_Strict):
    reagent_id: str = Field(..., description="试剂编号，见 /catalog")
    concentration: float = Field(..., description="试剂浓度（质量/体积百分比，0-100）")
    volume_ml: float = Field(..., description="该加液的体积（毫升，须 > 0）")


class StepIn(_Strict):
    seq: int = Field(..., description="步骤序号，正整数，整个流程内不得重复")
    op: Operation = Field(..., description="add=加液, dilute=加水稀释, pour=倒出, clean=清洗, hold=静置等待")
    reagent_id: str | None = Field(None, description="op=add 时必填：加入的试剂编号")
    concentration: float | None = Field(None, description="op=add 时必填：加入试剂的浓度（0-100）")
    volume_ml: float | None = Field(
        None,
        description="add/dilute: 加入体积；pour: 倒出体积；均须 > 0。clean/hold 不得提供。",
    )
    at_seconds: float | None = Field(
        None,
        description="该步骤完成时刻（秒，相对流程起点），须非负且不早于上一步。缺省沿用上一步时刻。",
    )
    note: str | None = Field(None, description="可选备注，不参与计算")


class ContactAllowanceIn(_Strict):
    a: str = Field(..., description="反应对一端：试剂编号，或材质编号（材质用 material:<id> 或直接 <id>）")
    b: str = Field(..., description="反应对另一端：试剂编号或材质编号")
    max_contact_seconds: float = Field(..., description="声明允许的接触时长（秒，须 > 0）")


class PrecheckRequest(_Strict):
    vessel_material: str = Field(..., description="容器材质编号，见 /catalog")
    temperature_c: float = Field(..., description="体系温度（摄氏度），允许范围 -50 ~ 300")
    initial_liquid: list[ComponentIn] = Field(default_factory=list, description="容器初始液体")
    steps: list[StepIn] = Field(default_factory=list, description="按顺序执行的加液/操作步骤")
    contact_allowances: list[ContactAllowanceIn] = Field(
        default_factory=list, description="可选：为某些反应对声明允许接触时长（覆盖规则默认值）"
    )


# ---------------------------------------------------------------------------
# 响应
# ---------------------------------------------------------------------------
class ComponentState(_Strict):
    reagent_id: str
    name: str
    concentration: float
    pure_volume_ml: float


class StepTrace(_Strict):
    seq: int | None
    op: str
    at_seconds: float
    liquid_volume_ml: float
    cleaned: bool
    components: list[ComponentState]


class Finding(_Strict):
    seq: int | None = Field(..., description="首次触发的步骤序号；初始液体触发时为 0")
    category: Literal["forbidden_mix", "overtime_contact", "material_incompatible"]
    rule_id: str
    rule_name: str
    entities: list[str] = Field(..., description="涉及组分：试剂编号或 material:<id>")
    concentrations: dict[str, float] = Field(..., description="涉及试剂在触发时刻的估算浓度（%）")
    contact_seconds: float | None = Field(None, description="超时类：实际接触时长（秒）")
    allowed_seconds: float | None = Field(None, description="超时类：允许接触时长（秒）")
    severity: str
    reason: str
    guidance: str
    blocking_reason: str = Field(..., description="阻断原因（人类可读）")


class PrecheckResponse(_Strict):
    safe: bool
    blocked: bool
    first_blocking_seq: int | None
    ruleset_version: str
    vessel_material: str
    temperature_c: float
    findings: list[Finding]
    final_state: StepTrace
    steps: list[StepTrace]


class ErrorDetail(_Strict):
    code: str
    message: str
    seq: int | None = None
    field: str | None = None


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    error: Literal["validation_error", "simulation_error", "internal_error"]
    message: str
    details: list[ErrorDetail]
