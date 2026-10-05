"""化学试剂 / 容器材质目录（内置静态数据）。

- 试剂使用短编号（reagent_id）标识，带分类标签（tag）与库存常见浓度上限。
- 浓度上限仅用于请求校验（CONCENTRATION_OUT_OF_RANGE）。
- 规则库引用的是分类标签而不是具体试剂编号，新增同类型试剂时自动适用规则。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Reagent:
    id: str
    name: str
    cas: str
    tags: tuple[str, ...]
    max_concentration: float  # 该试剂在现实体系中可出现的最高质量/体积百分比，仅用于越界校验


@dataclass(frozen=True)
class Material:
    id: str
    name: str
    tags: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 试剂目录
# ---------------------------------------------------------------------------
REAGENTS: dict[str, Reagent] = {
    r.id: r
    for r in [
        Reagent("h2so4", "浓硫酸", "7664-93-9", ("strong_acid", "oxidizer", "water_reactive"), 98.0),
        Reagent("hcl", "盐酸（氯化氢）", "7647-01-0", ("strong_acid", "halide"), 37.0),
        Reagent("hno3", "浓硝酸", "7697-37-2", ("strong_acid", "oxidizer"), 98.0),
        Reagent("hf", "氢氟酸", "7664-39-3", ("strong_acid", "halide", "fluoride"), 49.0),
        Reagent("acoh", "乙酸（醋酸）", "64-19-7", ("weak_acid", "flammable"), 100.0),
        Reagent("naoh", "氢氧化钠溶液", "1310-73-2", ("strong_base",), 50.0),
        Reagent("koh", "氢氧化钾溶液", "1310-58-3", ("strong_base",), 50.0),
        Reagent("nh3", "氨水", "1336-21-6", ("weak_base", "ammonia"), 30.0),
        Reagent("nacl", "氯化钠溶液", "7647-14-5", ("halide",), 26.0),
        Reagent("nabr", "溴化钠溶液", "7647-15-6", ("halide",), 50.0),
        Reagent("naocl", "次氯酸钠溶液（漂白水）", "7681-52-9", ("hypochlorite", "oxidizer"), 15.0),
        Reagent("h2o2", "过氧化氢（双氧水）", "7722-84-1", ("oxidizer", "peroxide"), 60.0),
        Reagent("kmno4", "高锰酸钾溶液", "7722-64-7", ("oxidizer", "permanganate"), 20.0),
        Reagent("k2cr2o7", "重铬酸钾溶液", "7778-50-9", ("oxidizer", "chromate"), 95.0),
        Reagent("nh4cl", "氯化铵溶液", "12125-02-9", ("ammonium_salt", "halide"), 40.0),
        Reagent("nh4no3", "硝酸铵溶液", "6484-52-2", ("ammonium_salt", "nitrate", "oxidizer"), 99.0),
        Reagent("na2s", "硫化钠溶液", "1313-82-2", ("sulfide",), 25.0),
        Reagent("ethanol", "乙醇", "64-17-5", ("flammable", "solvent"), 100.0),
        Reagent("acetone", "丙酮", "67-64-1", ("flammable", "solvent", "ketone"), 100.0),
        Reagent("formaldehyde", "甲醛溶液（福尔马林）", "50-00-0", ("aldehyde",), 40.0),
        Reagent("water", "纯水", "7732-18-5", ("water",), 100.0),
    ]
}

# ---------------------------------------------------------------------------
# 容器材质目录
# ---------------------------------------------------------------------------
MATERIALS: dict[str, Material] = {
    m.id: m
    for m in [
        Material("glass", "普通硼硅玻璃"),
        Material("hdpe", "高密度聚乙烯（HDPE）"),
        Material("ptfe", "聚四氟乙烯（PTFE/特氟龙）"),
        Material("natural_rubber", "天然橡胶"),
        Material("aluminum", "铝及铝合金"),
        Material("carbon_steel", "碳钢"),
        Material("stainless_steel", "不锈钢（304/316 类）"),
    ]
}

# 规则中用 material:<id> 这样的 token 表示材质侧
MATERIAL_PREFIX = "material:"


def reagent_tags(reagent_id: str) -> frozenset[str]:
    r = REAGENTS[reagent_id]
    return frozenset((f"id:{r.id}", *r.tags))


def material_token(material_id: str) -> str:
    return f"{MATERIAL_PREFIX}{material_id}"
