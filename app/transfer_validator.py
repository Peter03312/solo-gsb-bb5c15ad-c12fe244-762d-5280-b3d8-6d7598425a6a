"""双容器转移预检：逐项校验 + 归一化执行计划。

校验通过后返回 TransferPlan：步骤按 seq 升序（稳定）排列，
同一时刻的多个步骤即按 seq 顺序执行；引擎只需重放该计划，不再处理非法输入。

逐项拒绝（任何错误都不返回部分模拟结果）：
- 容器数量不是两只、编号缺失/重复、材质未知；
- 初始液体重复试剂、未知试剂、浓度/体积越界；
- 步骤序号非正/重复、未知操作、字段不匹配、引用未知容器、源目相同；
- 转移体积非正、源容器为空、转移量超过源容器当前液体体积；
- 时刻为负、执行序 (at_seconds, seq) 上时间回退、hold 未推进时间且两容器皆空；
- allowance 实体未知/非法/重复。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import errors
from .catalog import MATERIALS
from .transfer_models import TransferPrecheckRequest, TransferStepIn, VesselIn
from .validator import _detail, _validate_component, resolve_entity


@dataclass
class PlannedTransferStep:
    index: int          # 在原始 steps 数组中的位置
    seq: int
    op: str
    from_vessel: str | None
    to_vessel: str | None
    volume_ml: float | None
    at_seconds: float
    note: str | None = None


@dataclass
class PlannedVessel:
    vessel_id: str
    material: str
    initial: list[tuple[str, float, float]] = field(default_factory=list)  # (reagent, conc%, vol)


@dataclass
class TransferPlan:
    vessels: list[PlannedVessel]
    temperature_c: float
    steps: list[PlannedTransferStep] = field(default_factory=list)
    allowances: dict[tuple[str, str], float] = field(default_factory=dict)


def _validate_vessel(v: VesselIn, idx: int, errs: list[dict]) -> PlannedVessel | None:
    pf = f"vessels[{idx}]"
    usable_ves = True
    if not v.vessel_id:
        errs.append(_detail(errors.DUPLICATE_VESSEL_ID, "容器编号不能为空字符串", fld=f"{pf}.vessel_id"))
        usable_ves = False
    if v.material not in MATERIALS:
        errs.append(_detail(errors.UNKNOWN_MATERIAL, f"未知容器材质编号：{v.material}", fld=f"{pf}.material"))
        usable_ves = False

    seen: set[str] = set()
    initial: list[tuple[str, float, float]] = []
    for j, c in enumerate(v.initial_liquid):
        cpf = f"{pf}.initial_liquid[{j}]"
        if c.reagent_id in seen:
            errs.append(
                _detail(
                    errors.DUPLICATE_COMPONENT,
                    f"容器 {v.vessel_id or idx} 初始液体中试剂 {c.reagent_id} 重复出现",
                    fld=cpf,
                )
            )
        else:
            seen.add(c.reagent_id)
        if _validate_component(c.reagent_id, c.concentration, c.volume_ml, errs, seq=None, fld_prefix=cpf):
            if c.reagent_id not in [x[0] for x in initial]:
                initial.append((c.reagent_id, c.concentration, c.volume_ml))

    if not usable_ves:
        return None
    return PlannedVessel(vessel_id=v.vessel_id, material=v.material, initial=initial)


def _validate_step(
    st: TransferStepIn,
    i: int,
    seq_seen: set[int],
    vessel_ids: set[str] | None,
    errs: list[dict],
) -> bool:
    ok = True
    if st.seq <= 0:
        errs.append(_detail(errors.INVALID_SEQ, f"步骤序号须为正整数，收到 {st.seq}", seq=st.seq, fld=f"steps[{i}].seq"))
        ok = False
    elif st.seq in seq_seen:
        errs.append(_detail(errors.DUPLICATE_STEP_SEQ, f"步骤序号 {st.seq} 重复", seq=st.seq, fld="seq"))
        ok = False
    else:
        seq_seen.add(st.seq)

    if st.at_seconds < 0:
        errs.append(_detail(errors.INVALID_TIME, f"时刻不能为负，收到 {st.at_seconds}", seq=st.seq, fld="at_seconds"))
        ok = False

    if st.op == "transfer":
        src, dst = st.from_vessel, st.to_vessel
        if src is None:
            errs.append(_detail(errors.INVALID_STEP_FIELDS, "transfer 缺少 from_vessel", seq=st.seq, fld="from_vessel"))
            ok = False
        elif vessel_ids is not None and src not in vessel_ids:
            errs.append(_detail(errors.UNKNOWN_VESSEL, f"transfer 引用了未知源容器编号：{src}", seq=st.seq, fld="from_vessel"))
            ok = False
        if dst is None:
            errs.append(_detail(errors.INVALID_STEP_FIELDS, "transfer 缺少 to_vessel", seq=st.seq, fld="to_vessel"))
            ok = False
        elif vessel_ids is not None and dst not in vessel_ids:
            errs.append(_detail(errors.UNKNOWN_VESSEL, f"transfer 引用了未知目标容器编号：{dst}", seq=st.seq, fld="to_vessel"))
            ok = False
        if src is not None and src == dst and (vessel_ids is None or src in vessel_ids):
            errs.append(
                _detail(
                    errors.SAME_VESSEL_TRANSFER,
                    f"transfer 的源容器与目标容器相同（{src}），转移必须发生在两只不同容器之间",
                    seq=st.seq,
                    fld="to_vessel",
                )
            )
            ok = False
        if st.volume_ml is None or st.volume_ml <= 0:
            errs.append(
                _detail(errors.INVALID_VOLUME, f"transfer 的转移体积须为正数，收到 {st.volume_ml}", seq=st.seq, fld="volume_ml")
            )
            ok = False
    elif st.op == "hold":
        extra = [f for f in ("from_vessel", "to_vessel") if getattr(st, f) is not None]
        if extra:
            errs.append(_detail(errors.INVALID_STEP_FIELDS, "hold（静置）不得提供 from_vessel/to_vessel", seq=st.seq))
            ok = False
        if st.volume_ml is not None:
            errs.append(_detail(errors.INVALID_STEP_FIELDS, "hold（静置）不得提供 volume_ml", seq=st.seq))
            ok = False
    return ok


def build_transfer_plan(req: TransferPrecheckRequest) -> TransferPlan:
    errs: list[dict] = []

    if not (-50.0 <= req.temperature_c <= 300.0):
        errs.append(
            _detail(
                errors.TEMPERATURE_OUT_OF_RANGE,
                f"温度须在 [-50, 300] ℃ 区间内，收到 {req.temperature_c}",
                fld="temperature_c",
            )
        )

    # --------------------------------------------------------------
    # 容器
    # --------------------------------------------------------------
    if len(req.vessels) != 2:
        errs.append(
            _detail(
                errors.INVALID_VESSEL_COUNT,
                f"转移预检恰好需要两只容器，收到 {len(req.vessels)} 只",
                fld="vessels",
            )
        )

    vessels: list[PlannedVessel] = []
    seen_ids: set[str] = set()
    for i, v in enumerate(req.vessels):
        pv = _validate_vessel(v, i, errs)
        if pv is not None:
            vessels.append(pv)
            if v.vessel_id:
                seen_ids.add(v.vessel_id)

    # 编号互异（要求里的“编号互异”：为空或重复逐项拒绝）
    id_counts: dict[str, int] = {}
    for v in req.vessels:
        if v.vessel_id:
            id_counts[v.vessel_id] = id_counts.get(v.vessel_id, 0) + 1
    for vid, n in id_counts.items():
        if n > 1:
            errs.append(_detail(errors.DUPLICATE_VESSEL_ID, f"容器编号 {vid} 重复，两只容器编号必须互异", fld="vessels"))

    id_set = set(seen_ids)

    # --------------------------------------------------------------
    # 接触时长声明（与单容器同一套规则，对两只容器同时生效）
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
    # 步骤：结构逐字段校验
    # --------------------------------------------------------------
    seq_seen: set[int] = set()
    valid_steps: list[tuple[int, TransferStepIn]] = []
    for i, st in enumerate(req.steps):
        if _validate_step(st, i, seq_seen, id_set if len(id_set) == 2 else None, errs):
            valid_steps.append((i, st))

    # --------------------------------------------------------------
    # 步骤：执行序 (at_seconds, seq) 的时序与状态流校验
    # --------------------------------------------------------------
    planned: list[PlannedTransferStep] = []
    vessels_ok = len(req.vessels) == 2 and len(id_set) == 2 and not any(
        d["code"] in (errors.DUPLICATE_VESSEL_ID, errors.UNKNOWN_MATERIAL) for d in errs
    )
    if vessels_ok and not any(e["code"] == errors.DUPLICATE_STEP_SEQ for e in errs):
        by_id = {pv.vessel_id: pv for pv in vessels}
        volume = {vid: sum(vol for _, _, vol in by_id[vid].initial) for vid in id_set}
        # 执行序：步骤按 seq 升序（稳定）；同一时刻的多个步骤即按 seq 顺序执行。
        ordered = sorted(valid_steps, key=lambda p: (p[1].seq, p[0]))
        t = 0.0
        stop = False
        for i, st in ordered:
            at = st.at_seconds
            if at + 1e-9 < t:
                errs.append(
                    _detail(
                        errors.NON_MONOTONIC_TIME,
                        f"步骤 {st.seq} 的时刻 {at:g}s 早于上一步时刻 {t:g}s，时间不得回退",
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
            elif st.op == "hold" and all(volume[v] <= 1e-12 for v in id_set):
                errs.append(
                    _detail(
                        errors.EMPTY_VESSEL_OPERATION,
                        f"两只容器均为空，hold（静置）没有可计时的接触；步骤 {st.seq}",
                        seq=st.seq,
                    )
                )
                stop = True
            elif st.op == "transfer":
                src, dst = st.from_vessel, st.to_vessel
                if volume[src] <= 1e-12:  # type: ignore[index]
                    errs.append(
                        _detail(
                            errors.EMPTY_SOURCE_VESSEL,
                            f"源容器 {src} 为空，无法转移（步骤 {st.seq}）",
                            seq=st.seq,
                            fld="from_vessel",
                        )
                    )
                    stop = True
                elif st.volume_ml > volume[src] + 1e-9:  # type: ignore[index,operator]
                    errs.append(
                        _detail(
                            errors.TRANSFER_EXCEEDS_VOLUME,
                            f"转移体积 {st.volume_ml:g} mL 超过源容器 {src} 当前液体体积 {volume[src]:g} mL（步骤 {st.seq}）",
                            seq=st.seq,
                            fld="volume_ml",
                        )
                    )
                    stop = True

            if stop:
                break

            if st.op == "transfer":
                volume[st.from_vessel] -= st.volume_ml  # type: ignore[index,operator]
                volume[st.to_vessel] += st.volume_ml  # type: ignore[index,operator]

            planned.append(
                PlannedTransferStep(
                    index=i,
                    seq=st.seq,
                    op=st.op,
                    from_vessel=st.from_vessel,
                    to_vessel=st.to_vessel,
                    volume_ml=st.volume_ml,
                    at_seconds=at,
                    note=st.note,
                )
            )
            t = at

    if errs:
        raise errors.ValidationFailure(errs)

    return TransferPlan(vessels=vessels, temperature_c=req.temperature_c, steps=planned, allowances=allowances)
