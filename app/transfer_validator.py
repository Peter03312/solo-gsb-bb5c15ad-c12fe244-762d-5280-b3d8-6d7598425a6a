"""双容器转移预检的逐项校验与执行计划归一化。

校验内容：
- 恰好两只容器、编号非空互异、材质已知、初始液体合法（沿用单容器组分规则）；
- 步骤序号正整数且不重复、transfer/hold 字段匹配、时刻非负且不回退；
- transfer 的源/目标必须为两只互异的已知容器、体积为正、不超源容器当前量、
  源容器不得为空；hold 在两侧皆空时拒绝（静置必须针对当前液体内的接触对）。
校验通过后返回按 seq 稳定排序、时刻补全的 TransferPlan；任一错误都整体拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import errors
from .catalog import MATERIALS
from .models import TransferPrecheckRequest, TransferStepIn
from .validator import _detail, _validate_allowances, _validate_component


@dataclass
class PlannedTransferStep:
    index: int          # 在原始 steps 数组中的位置（稳定排序用）
    seq: int
    op: str
    from_vessel: str | None
    to_vessel: str | None
    volume_ml: float | None
    at_seconds: float
    note: str | None = None


@dataclass
class VesselSpec:
    vessel_id: str
    vessel_material: str
    initial: list[tuple[str, float, float]] = field(default_factory=list)  # (reagent, conc%, vol)


@dataclass
class TransferPlan:
    vessels: list[VesselSpec]
    temperature_c: float
    steps: list[PlannedTransferStep] = field(default_factory=list)
    allowances: dict[tuple[str, str], float] = field(default_factory=dict)


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
    specs: list[VesselSpec] = []
    if len(req.vessels) != 2:
        errs.append(
            _detail(
                errors.INVALID_VESSEL,
                f"转移预检必须恰好描述两只容器，收到 {len(req.vessels)} 只",
                fld="vessels",
            )
        )

    seen_ids: set[str] = set()
    known_ids: set[str] = set()
    for i, v in enumerate(req.vessels):
        pf = f"vessels[{i}]"
        vid = v.vessel_id
        if vid in seen_ids:
            errs.append(_detail(errors.DUPLICATE_VESSEL_ID, f"容器编号 {vid} 重复（两只容器编号须互异）", fld=f"{pf}.vessel_id"))
        else:
            seen_ids.add(vid)
        if v.vessel_material not in MATERIALS:
            errs.append(_detail(errors.UNKNOWN_MATERIAL, f"未知容器材质编号：{v.vessel_material}", fld=f"{pf}.vessel_material"))

        seen_components: set[str] = set()
        initial: list[tuple[str, float, float]] = []
        for j, c in enumerate(v.initial_liquid):
            cpf = f"{pf}.initial_liquid[{j}]"
            if c.reagent_id in seen_components:
                errs.append(_detail(errors.DUPLICATE_COMPONENT, f"容器 {vid} 初始液体中试剂 {c.reagent_id} 重复出现", fld=cpf))
            else:
                seen_components.add(c.reagent_id)
            usable = _validate_component(
                c.reagent_id, c.concentration, c.volume_ml, errs, seq=None, fld_prefix=cpf
            )
            if usable and c.reagent_id not in [x[0] for x in initial]:
                initial.append((c.reagent_id, c.concentration, c.volume_ml))  # type: ignore[arg-type]

        # 仅两只互异容器都成立时才登记为“已知容器”，避免步骤校验产生级联噪声
        specs.append(VesselSpec(vessel_id=vid, vessel_material=v.vessel_material, initial=initial))

    if len(req.vessels) == 2 and not any(
        e["code"] in (errors.INVALID_VESSEL, errors.DUPLICATE_VESSEL_ID) for e in errs
    ):
        known_ids = {v.vessel_id for v in req.vessels}

    # --------------------------------------------------------------
    # 接触时长声明
    # --------------------------------------------------------------
    allowances = _validate_allowances(req.contact_allowances, errs)

    # --------------------------------------------------------------
    # 步骤：逐字段校验
    # --------------------------------------------------------------
    seq_seen: set[int] = set()
    valid_steps: list[tuple[int, TransferStepIn]] = []
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

        if st.op == "transfer":
            if st.from_vessel is None:
                errs.append(_detail(errors.INVALID_STEP_FIELDS, "transfer 缺少源容器编号 from_vessel", seq=st.seq, fld="from_vessel"))
                ok = False
            if st.to_vessel is None:
                errs.append(_detail(errors.INVALID_STEP_FIELDS, "transfer 缺少目标容器编号 to_vessel", seq=st.seq, fld="to_vessel"))
                ok = False
            if st.volume_ml is None or st.volume_ml <= 0:
                errs.append(
                    _detail(
                        errors.INVALID_VOLUME,
                        f"转移体积须为正数（毫升），收到 {st.volume_ml}",
                        seq=st.seq,
                        fld="volume_ml",
                    )
                )
                ok = False
            if st.from_vessel is not None and known_ids and st.from_vessel not in known_ids:
                errs.append(
                    _detail(errors.UNKNOWN_VESSEL, f"未知源容器编号：{st.from_vessel}", seq=st.seq, fld="from_vessel")
                )
                ok = False
            if st.to_vessel is not None and known_ids and st.to_vessel not in known_ids:
                errs.append(
                    _detail(errors.UNKNOWN_VESSEL, f"未知目标容器编号：{st.to_vessel}", seq=st.seq, fld="to_vessel")
                )
                ok = False
            if (
                st.from_vessel is not None
                and st.to_vessel is not None
                and st.from_vessel in known_ids
                and st.to_vessel in known_ids
                and st.from_vessel == st.to_vessel
            ):
                errs.append(
                    _detail(
                        errors.SAME_VESSEL,
                        f"转移的源容器与目标容器不得相同（均为 {st.from_vessel}）",
                        seq=st.seq,
                    )
                )
                ok = False
        elif st.op == "hold":
            if st.volume_ml is not None or st.from_vessel is not None or st.to_vessel is not None:
                errs.append(
                    _detail(
                        errors.INVALID_STEP_FIELDS,
                        "hold（静置等待）不得携带 from_vessel/to_vessel/volume_ml",
                        seq=st.seq,
                    )
                )
                ok = False
            if st.at_seconds is None:
                errs.append(_detail(errors.INVALID_TIME, "hold 必须显式给出晚于当前时刻的 at_seconds", seq=st.seq, fld="at_seconds"))
                ok = False
        else:  # 理论上已被 Literal 拦截
            errs.append(_detail(errors.UNKNOWN_OPERATION, f"未知操作类型：{st.op}", seq=st.seq))
            ok = False

        if ok:
            valid_steps.append((i, st))

    # --------------------------------------------------------------
    # 步骤：时序与状态流校验（按 seq 稳定排序重放两侧体积）
    # --------------------------------------------------------------
    planned: list[PlannedTransferStep] = []
    vessel_blocked = not known_ids or any(
        e["code"] in (errors.DUPLICATE_STEP_SEQ, errors.INVALID_SEQ) for e in errs
    )
    if not vessel_blocked:
        ordered = sorted(valid_steps, key=lambda pair: pair[1].seq)  # 稳定：seq 唯一
        t = 0.0
        volumes = {spec.vessel_id: sum(v for _, _, v in spec.initial) for spec in specs}
        for i, st in ordered:
            at = st.at_seconds if st.at_seconds is not None else t
            stop = False
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
            elif st.op == "hold" and all(vol <= 1e-12 for vol in volumes.values()):
                errs.append(
                    _detail(
                        errors.EMPTY_VESSEL_OPERATION,
                        "两只容器均为空，无法对不存在的液体执行 hold（静置等待）",
                        seq=st.seq,
                    )
                )
                stop = True
            elif st.op == "transfer":
                src, dst, vol = st.from_vessel, st.to_vessel, st.volume_ml
                src_vol = volumes[src]  # type: ignore[index]
                if src_vol <= 1e-12:
                    errs.append(
                        _detail(
                            errors.EMPTY_SOURCE_VESSEL,
                            f"源容器 {src} 为空，步骤 {st.seq} 无液体可转出",
                            seq=st.seq,
                            fld="from_vessel",
                        )
                    )
                    stop = True
                elif vol > src_vol + 1e-9:  # type: ignore[operator]
                    errs.append(
                        _detail(
                            errors.TRANSFER_EXCEEDS_VOLUME,
                            f"转移体积 {vol:g} mL 超过源容器 {src} 当前液体体积 {src_vol:g} mL",
                            seq=st.seq,
                            fld="volume_ml",
                        )
                    )
                    stop = True

            if stop:
                break

            if st.op == "transfer":
                volumes[st.from_vessel] -= st.volume_ml  # type: ignore[index,operator]
                volumes[st.to_vessel] += st.volume_ml  # type: ignore[index,operator]
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

    return TransferPlan(vessels=specs, temperature_c=req.temperature_c, steps=planned, allowances=allowances)
