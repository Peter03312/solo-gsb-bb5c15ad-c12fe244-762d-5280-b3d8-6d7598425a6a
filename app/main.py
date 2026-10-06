"""化学试剂混配风险预检 HTTP API。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import errors
from .catalog import MATERIALS, REAGENTS
from .engine import simulate
from .models import ErrorDetail, ErrorResponse, PrecheckRequest, PrecheckResponse
from .rules import RULESET_VERSION, RULES_BY_ID
from .transfer_engine import simulate_transfer
from .transfer_models import TransferPrecheckRequest, TransferPrecheckResponse
from .transfer_validator import build_transfer_plan
from .validator import build_plan

app = FastAPI(
    title="化学试剂混配风险预检 API",
    version="1.0.0",
    description=(
        "提交容器初始液体、按顺序执行的加液步骤、试剂浓度与体积，以及容器材质和温度；"
        "逐步计算混配后各组分浓度，识别禁配组合、超时接触与材质不相容。"
    ),
)


def _validation_error_body(exc) -> JSONResponse:
    details = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"] if x not in ("body",))
        details.append(
            ErrorDetail(code=errors.MALFORMED_REQUEST, message=f"{loc}: {err['msg']}", field=loc or None).model_dump()
        )
    body = ErrorResponse(
        error="validation_error",
        message="请求结构不符合 schema，未进行模拟",
        details=details,
    ).model_dump()
    # 统一返回 400：结构错误与业务校验错误对调用方同为“请求被拒绝”
    return JSONResponse(status_code=400, content=body)


@app.exception_handler(ValidationError)
async def pydantic_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
    return _validation_error_body(exc)


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _validation_error_body(exc)


@app.exception_handler(errors.ValidationFailure)
async def validation_failure_handler(request: Request, exc: errors.ValidationFailure) -> JSONResponse:
    details = [ErrorDetail(**d).model_dump() for d in exc.details]
    body = ErrorResponse(
        error="validation_error",
        message="预检请求被逐项拒绝，未进行模拟",
        details=details,
    ).model_dump()
    return JSONResponse(status_code=400, content=body)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "ruleset_version": RULESET_VERSION}


@app.get("/catalog")
async def catalog() -> dict:
    return {
        "reagents": [
            {"reagent_id": r.id, "name": r.name, "cas": r.cas, "tags": list(r.tags), "max_concentration": r.max_concentration}
            for r in REAGENTS.values()
        ],
        "materials": [{"material_id": m.id, "name": m.name} for m in MATERIALS.values()],
        "rules": [
            {
                "rule_id": r.id,
                "name": r.name,
                "kind": r.kind,
                "severity": r.severity,
                "max_contact_seconds": r.max_contact_seconds,
                "selectors": {
                    "a": sorted(r.side_a.selectors),
                    "b": sorted(r.side_b.selectors),
                    "min_concentration": {"a": r.side_a.min_concentration, "b": r.side_b.min_concentration},
                },
            }
            for r in RULES_BY_ID.values()
        ],
        "ruleset_version": RULESET_VERSION,
    }


@app.post("/api/v1/precheck", response_model=PrecheckResponse)
async def precheck(req: PrecheckRequest) -> PrecheckResponse:
    plan = build_plan(req)
    result = simulate(plan)
    return PrecheckResponse(
        safe=not result["findings"],
        blocked=bool(result["findings"]),
        first_blocking_seq=result["first_blocking_seq"],
        ruleset_version=RULESET_VERSION,
        vessel_material=plan.vessel_material,
        temperature_c=plan.temperature_c,
        findings=result["findings"],
        final_state=result["final_state"],
        steps=result["traces"][1:],  # 第 0 条是 initial 快照
    )


@app.post("/api/v2/transfer-precheck", response_model=TransferPrecheckResponse)
async def transfer_precheck(req: TransferPrecheckRequest) -> TransferPrecheckResponse:
    """双容器定量转移预检：逐步扣减/计入组分，检查两侧禁配、超时接触与材质风险。"""
    plan = build_transfer_plan(req)
    result = simulate_transfer(plan)
    return TransferPrecheckResponse(
        safe=not result["findings"],
        blocked=bool(result["findings"]),
        first_blocking_seq=result["first_blocking_seq"],
        ruleset_version=RULESET_VERSION,
        temperature_c=plan.temperature_c,
        findings=result["findings"],
        vessels=result["initial_states"],
        steps=result["step_traces"],
        final_states=result["final_states"],
    )
