"""请求逐项校验：步骤序号、体积、浓度、未知试剂、清洗后状态引用等。

校验通过后返回归一化的执行计划（步骤按 seq 稳定排序、时刻补全），
引擎只需重放该计划，不再处理非法输入。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import errors
from .catalog import MATERIALS, REAGENTS
from .models import PrecheckRequest, StepIn


@dataclass
class PlannedStep:
    index: int          # 在原始 steps 数组中的位置（稳定排序用）
    seq: int
    op: str
    reagent_id: str | None
    concentration: float | None
    volume_ml: float | None
    at_seconds: float
    note: str | None = None


@dataclass
class Plan:
    vessel_material: str
    temperature_c: float
    initial: list[tuple[str, float, float]] = field(default_factory=list)  # (reagent, conc%, vol)
    steps: list[PlannedStep] = field(default_factory=list)
    allowances: dict[tuple[str, str], float] = field(default_factory=dict)


def _detail(code: str, message: str, seq: int | None = None, fld: str | None = None) -> dict:
    d: dict = {"code": code, "message": message}
    if seq is not None:
        d["seq"] = seq
    if fld is not None:
        d["field"] = fld
    return d


def resolve_entity(token: str) -> tuple[str, str] | None:
    """把 allowance 中的一端解析为 (kind, token)。

    kind='reagent' -> token 为试剂编号；kind='material' -> token 为 material:<id>。
    """
    if token in REAGENTS:
        return "reagent", token
    if token.startswith("material:"):
        mid = token[len("material:"):]
        if mid in MATERIALS:
            return "material", token
        return None
    if token in MATERIALS:
        return "material", f"material:{token}"
    return None


def _validate_component(
    reagent_id: str | None,
    concentration: float | None,
    volume_ml: float | None,
    errs: list[dict],
    *,
    seq: int | None,
    fld_prefix: str,
) -> bool:
    """校验一个加液组分；返回该组分是否可用于后续状态流模拟。"""
    if reagent_id is None:
        errs.append(_detail(errors.INVALID_STEP_FIELDS, "缺少 reagent_id", seq, f"{fld_prefix}.reagent_id"))
        return False

    known = reagent_id in REAGENTS
    if not known:
        errs.append(_detail(errors.UNKNOWN_REAGENT, f"未知试剂编号：{reagent_id}", seq, f"{fld_prefix}.reagent_id"))

    usable = known
    if concentration is None:
        errs.append(_detail(errors.INVALID_STEP_FIELDS, "缺少 concentration", seq, f"{fld_prefix}.concentration"))
        usable = False
    else:
        if concentration <= 0 or concentration > 100:
            errs.append(
                _detail(
                    errors.CONCENTRATION_OUT_OF_RANGE,
                    f"浓度须在 (0, 100] 区间内，收到 {concentration}",
                    seq,
                    f"{fld_prefix}.concentration",
                )
            )
            usable = False
        elif known:
            cap = REAGENTS[reagent_id].max_concentration
            if concentration > cap + 1e-9:
                errs.append(
                    _detail(
                        errors.CONCENTRATION_OUT_OF_RANGE,
                        f"{REAGENTS[reagent_id].name}（{reagent_id}）浓度超过该试剂可出现的上限 "
                        f"{cap:g}%，收到 {concentration:g}%",
                        seq,
                        f"{fld_prefix}.concentration",
                    )
                )
                usable = False
            if reagent_id == "water" and abs(concentration - 100.0) > 1e-9:
                errs.append(
                    _detail(
                        errors.CONCENTRATION_OUT_OF_RANGE,
                        "water 仅支持 100%；稀释加水请直接使用 dilute 操作",
                        seq,
                        f"{fld_prefix}.concentration",
                    )
                )
                usable = False

    if volume_ml is None or volume_ml <= 0:
        errs.append(
            _detail(
                errors.INVALID_VOLUME,
                f"体积须为正数（毫升），收到 {volume_ml}",
                seq,
                f"{fld_prefix}.volume_ml",
            )
        )
        usable = False

    return usable


def build_plan(req: PrecheckRequest) -> Plan:
    errs: list[dict] = []

    if req.vessel_material not in MATERIALS:
        errs.append(_detail(errors.UNKNOWN_MATERIAL, f"未知容器材质编号：{req.vessel_material}", fld="vessel_material"))
    if not (-50.0 <= req.temperature_c <= 300.0):
        errs.append(
            _detail(
                errors.TEMPERATURE_OUT_OF_RANGE,
                f"温度须在 [-50, 300] ℃ 区间内，收到 {req.temperature_c}",
                fld="temperature_c",
            )
        )

    # --------------------------------------------------------------
    # 初始液体
    # --------------------------------------------------------------
    seen_initial: set[str] = set()
    initial: list[tuple[str, float, float]] = []
    init_volume = 0.0
    for i, c in enumerate(req.initial_liquid):
        pf = f"initial_liquid[{i}]"
        if c.reagent_id in seen_initial:
            errs.append(_detail(errors.DUPLICATE_COMPONENT, f"初始液体中试剂 {c.reagent_id} 重复出现", fld=pf))
        else:
            seen_initial.add(c.reagent_id)
        usable = _validate_component(
            c.reagent_id, c.concentration, c.volume_ml, errs, seq=None, fld_prefix=pf
        )
        if usable and c.reagent_id not in [x[0] for x in initial]:
            initial.append((c.reagent_id, c.concentration, c.volume_ml))  # type: ignore[arg-type]
            init_volume += c.volume_ml  # type: ignore[operator]

    # --------------------------------------------------------------
    # 接触时长声明
    # --------------------------------------------------------------
    allowances: dict[tuple[str, str], float] = {}
    for i, al in enumerate(req.contact_allowances):
        pf = f"contact_allowances[{i}]"
        ea = resolve_entity(al.a)
        eb = resolve_entity(al.b)
        if ea is None:
            errs.append(_detail(errors.UNKNOWN_ENTITY, f"allowance 端 '{al.a}' 既不是已知试剂也不是已知材质", fld=f"{pf}.a"))
        if eb is None:
            errs.append(_detail(errors.UNKNOWN_ENTITY, f"allowance 端 '{al.b}' 既不是已知试剂也不是已知材质", fld=f"{pf}.b"))
        if al.max_contact_seconds <= 0:
            errs.append(
                _detail(
                    errors.INVALID_ALLOWANCE,
                    f"max_contact_seconds 须为正数，收到 {al.max_contact_seconds}",
                    fld=f"{pf}.max_contact_seconds",
                )
            )
        if ea and eb:
            _, toka = ea
            _, tokb = eb
            if toka == tokb:
                errs.append(
                    _detail(
                        errors.INVALID_ALLOWANCE,
                        "接触声明必须是两个不同实体之间的反应对（试剂-试剂 或 试剂-材质）",
                        fld=pf,
                    )
                )
            else:
                key = tuple(sorted((toka, tokb)))
                if key in allowances:
                    errs.append(
                        _detail(
                            errors.DUPLICATE_ALLOWANCE,
                            f"反应对 {key[0]} / {key[1]} 的接触时长被重复声明",
                            fld=pf,
                        )
                    )
                else:
                    allowances[key] = al.max_contact_seconds

    # --------------------------------------------------------------
    # 步骤：逐字段校验
    # --------------------------------------------------------------
    seq_seen: set[int] = set()
    valid_steps: list[tuple[int, StepIn]] = []
    for i, st in enumerate(req.steps):
        ok = True

        if st.seq <= 0:
            errs.append(_detail(errors.INVALID_SEQ, f"步骤序号须为正整数，收到 {st.seq}", seq=st.seq, fld=f"steps[{i}].seq"))
            ok = False
        elif st.seq in seq_seen:
            errs.append(_detail(errors.DUPLICATE_STEP_SEQ, f"步骤序号 {st.seq} 重复", seq=st.seq, fld="seq"))
            ok = False
        else:
            seq_seen.add(st.seq)

        if st.at_seconds is not None and st.at_seconds < 0:
            errs.append(_detail(errors.INVALID_TIME, f"时刻不能为负，收到 {st.at_seconds}", seq=st.seq, fld="at_seconds"))
            ok = False

        op = st.op
        if op == "add":
            if _validate_component(
                st.reagent_id, st.concentration, st.volume_ml, errs, seq=st.seq, fld_prefix=f"steps[{i}]"
            ) is False:
                ok = False
        elif op == "dilute":
            if st.reagent_id is not None or st.concentration is not None:
                errs.append(_detail(errors.INVALID_STEP_FIELDS, "dilute（加水稀释）不得提供 reagent_id/concentration", seq=st.seq))
                ok = False
            if st.volume_ml is None or st.volume_ml <= 0:
                errs.append(_detail(errors.INVALID_VOLUME, f"dilute 的加水体积须为正数，收到 {st.volume_ml}", seq=st.seq, fld="volume_ml"))
                ok = False
        elif op == "pour":
            if st.reagent_id is not None or st.concentration is not None:
                errs.append(_detail(errors.INVALID_STEP_FIELDS, "pour（倒出）不得提供 reagent_id/concentration", seq=st.seq))
                ok = False
            if st.volume_ml is None or st.volume_ml <= 0:
                errs.append(_detail(errors.INVALID_VOLUME, f"pour 的倒出体积须为正数，收到 {st.volume_ml}", seq=st.seq, fld="volume_ml"))
                ok = False
        elif op == "clean":
            if st.volume_ml is not None or st.reagent_id is not None or st.concentration is not None:
                errs.append(_detail(errors.INVALID_STEP_FIELDS, "clean（清洗）不得携带 reagent_id/concentration/volume_ml", seq=st.seq))
                ok = False
        elif op == "hold":
            if st.volume_ml is not None or st.reagent_id is not None or st.concentration is not None:
                errs.append(_detail(errors.INVALID_STEP_FIELDS, "hold（静置等待）不得携带 reagent_id/concentration/volume_ml", seq=st.seq))
                ok = False
            if st.at_seconds is None:
                errs.append(_detail(errors.INVALID_TIME, "hold 必须显式给出晚于当前时刻的 at_seconds", seq=st.seq, fld="at_seconds"))
                ok = False
        else:  # 理论上已被 Literal 拦截
            errs.append(_detail(errors.UNKNOWN_OPERATION, f"未知操作类型：{op}", seq=st.seq))
            ok = False

        if ok:
            valid_steps.append((i, st))

    # --------------------------------------------------------------
    # 步骤：时序与状态流校验（按 seq 稳定排序重放体积）
    # --------------------------------------------------------------
    planned: list[PlannedStep] = []
    if not any(e["code"] == errors.DUPLICATE_STEP_SEQ for e in errs):
        ordered = sorted(valid_steps, key=lambda pair: pair[1].seq)  # 稳定：seq 唯一
        t = 0.0
        volume = init_volume
        cleaned = False
        stop = False
        for i, st in ordered:
            at = st.at_seconds if st.at_seconds is not None else t
            if at + 1e-9 < t:
                errs.append(
                    _detail(
                        errors.NON_MONOTONIC_TIME,
                        f"步骤 {st.seq} 的时刻 {at:g}s 早于上一步时刻 {t:g}s",
                        seq=st.seq,
                        fld="at_seconds",
                    )
                )
                stop = True
            elif st.op == "hold" and at <= t + 1e-9:
                errs.append(
                    _detail(
                        errors.INVALID_TIME,
                        f"hold 的时刻须严格晚于当前时刻 {t:g}s，收到 {at:g}s",
                        seq=st.seq,
                        fld="at_seconds",
                    )
                )
                stop = True
            elif st.op == "hold" and volume <= 1e-12:
                code = errors.STALE_STATE_REFERENCE if cleaned else errors.EMPTY_VESSEL_OPERATION
                msg = (
                    "清洗后已无液体状态，hold 仍引用旧状态（等待必须针对当前液体内的接触对）"
                    if cleaned else
                    "容器为空，无法对不存在的液体执行 hold（静置等待）"
                )
                errs.append(_detail(code, msg, seq=st.seq))
                stop = True
            elif st.op == "pour" and cleaned:
                errs.append(
                    _detail(
                        errors.STALE_STATE_REFERENCE,
                        "容器已清洗，pour 仍引用旧状态（清洗后不存在可倒出的液体）",
                        seq=st.seq,
                    )
                )
                stop = True
            elif st.op == "pour" and volume <= 1e-12:
                errs.append(
                    _detail(
                        errors.EMPTY_VESSEL_OPERATION,
                        "容器内已无液体可倒出（pour 超出当前体积）",
                        seq=st.seq,
                    )
                )
                stop = True
            elif st.op == "pour" and st.volume_ml > volume + 1e-9:  # type: ignore[operator]
                errs.append(
                    _detail(
                        errors.POUR_EXCEEDS_VOLUME,
                        f"倒出体积 {st.volume_ml:g} mL 超过当前液体体积 {volume:g} mL",
                        seq=st.seq,
                        fld="volume_ml",
                    )
                )
                stop = True
            elif st.op == "dilute" and cleaned:
                errs.append(
                    _detail(
                        errors.STALE_STATE_REFERENCE,
                        "容器已清洗，dilute 不能再稀释旧状态中的液体；重新配制请使用 add",
                        seq=st.seq,
                    )
                )
                stop = True

            if stop:
                break

            if st.op == "add":
                cleaned = False
                volume += st.volume_ml  # type: ignore[operator]
            elif st.op == "dilute":
                volume += st.volume_ml  # type: ignore[operator]
            elif st.op == "pour":
                volume -= st.volume_ml  # type: ignore[operator]
            elif st.op == "clean":
                volume = 0.0
                cleaned = True

            planned.append(
                PlannedStep(
                    index=i,
                    seq=st.seq,
                    op=st.op,
                    reagent_id=st.reagent_id,
                    concentration=st.concentration,
                    volume_ml=st.volume_ml,
                    at_seconds=at,
                    note=st.note,
                )
            )
            t = at

    if errs:
        raise errors.ValidationFailure(errs)

    return Plan(
        vessel_material=req.vessel_material,
        temperature_c=req.temperature_c,
        initial=initial,
        steps=planned,
        allowances=allowances,
    )
