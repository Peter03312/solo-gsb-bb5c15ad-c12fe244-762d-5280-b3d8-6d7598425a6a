"""公开化学相容性规则库（静态、可版本化）。

规则三类：
- forbidden_mix        禁配组合：两种试剂一经共存即阻断（超过浓度阈值时）。
- limited_contact      限时接触：两种试剂允许短时共存，超过秒数（默认值或请求
                        中 contact_allowances 声明的值）即判超时接触。
- material_incompatible 材质不相容：试剂与容器材质不允许接触，或只允许限时接触。

规则侧（side）用标签选择器集合表示：
- 试剂侧：试剂自身标签（如 strong_acid）或具体编号（id:hcl），任一命中即匹配；
- 材质侧：material:<material_id>。
浓度阈值 min_concentration 针对“试剂侧”，默认 1.0%，低于阈值视为痕量、不触发。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RuleKind = Literal["forbidden_mix", "limited_contact", "material_incompatible"]
Severity = Literal["critical", "high", "medium"]


@dataclass(frozen=True)
class Side:
    selectors: frozenset[str]
    min_concentration: float = 1.0


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    kind: RuleKind
    side_a: Side
    side_b: Side
    reason: str
    guidance: str
    severity: Severity = "high"
    # 限时接触规则的默认允许接触秒数（forbidden_mix 为 None）
    max_contact_seconds: float | None = None
    # 温度达到该阈值时，在阻断原因中追加高温提示
    temp_note_threshold: float | None = None
    temp_note: str | None = None

    @property
    def timed(self) -> bool:
        return self.kind in ("limited_contact", "material_incompatible") and self.max_contact_seconds is not None


def _side(*selectors: str, min_concentration: float = 1.0) -> Side:
    return Side(frozenset(selectors), min_concentration)


RULES: tuple[Rule, ...] = (
    # -----------------------------------------------------------------
    # 禁配组合（立即阻断）
    # -----------------------------------------------------------------
    Rule(
        id="F01",
        name="强酸 + 次氯酸盐释放氯气",
        kind="forbidden_mix",
        side_a=_side("strong_acid", min_concentration=5.0),
        side_b=_side("hypochlorite", min_concentration=3.0),
        reason="强酸使次氯酸盐转化为氯气（Cl₂），高毒、强刺激，吸入可致死。",
        guidance="严禁向漂白水中加酸；废液须分别中和（次氯酸盐用硫代硫酸钠还原）后处置。",
        severity="critical",
        temp_note_threshold=30.0,
        temp_note="温度升高会显著加快氯气逸出速率。",
    ),
    Rule(
        id="F02",
        name="强酸 + 硫化物释放硫化氢",
        kind="forbidden_mix",
        side_a=_side("strong_acid", min_concentration=5.0),
        side_b=_side("sulfide", min_concentration=1.0),
        reason="强酸使硫化物转化为剧毒气体硫化氢（H₂S），低浓度即可致命。",
        guidance="严禁酸化硫化物废液；应在碱性条件下用次氯酸盐氧化破坏。",
        severity="critical",
    ),
    Rule(
        id="F03",
        name="次氯酸盐 + 游离氨生成氯胺",
        kind="forbidden_mix",
        side_a=_side("hypochlorite", min_concentration=3.0),
        side_b=_side("ammonia", min_concentration=1.0),
        reason="漂白剂与氨水反应生成氯胺（NH₂Cl/NHCl₃），剧毒、强刺激，高浓度可爆炸。",
        guidance="含氨与含氯消毒剂严禁混用；废液分别收集处置。",
        severity="critical",
    ),
    Rule(
        id="F04",
        name="氧化剂 + 易燃有机物",
        kind="forbidden_mix",
        side_a=_side("oxidizer", min_concentration=10.0),
        side_b=_side("flammable", min_concentration=5.0),
        reason="强氧化剂与易燃有机物混合可剧烈放热、起火或爆炸。",
        guidance="氧化剂与有机溶剂/易燃液体分开存放、分开配制，废液不得混收。",
        severity="critical",
        temp_note_threshold=40.0,
        temp_note="高温会降低氧化剂分解阈值，火灾风险进一步上升。",
    ),
    Rule(
        id="F05",
        name="强酸 + 强碱剧烈中和",
        kind="forbidden_mix",
        side_a=_side("strong_acid", min_concentration=10.0),
        side_b=_side("strong_base", min_concentration=10.0),
        reason="浓强酸与浓强碱中和剧烈放热，可致沸腾喷溅、容器破裂。",
        guidance="如需中和，应在搅拌、冷却下将一方缓慢、少量加入另一方，并控制温升。",
        severity="high",
    ),
    Rule(
        id="F06",
        name="铵盐 + 强碱释放氨气",
        kind="forbidden_mix",
        side_a=_side("ammonium_salt", min_concentration=5.0),
        side_b=_side("strong_base", min_concentration=5.0),
        reason="强碱使铵盐分解释放氨气（NH₃），强刺激且可与后续氧化性组分生成氯胺。",
        guidance="铵盐不得与强碱废液混合；接触前确认容器内无碱性残液。",
        severity="high",
    ),
    Rule(
        id="F07",
        name="次氯酸盐 + 酮类生成氯仿",
        kind="forbidden_mix",
        side_a=_side("hypochlorite", min_concentration=3.0),
        side_b=_side("ketone", min_concentration=5.0),
        reason="次氯酸盐与丙酮等酮类发生卤仿反应，放热并生成氯仿与氧气，有失控风险。",
        guidance="丙酮等酮类废液严禁倒入含氯漂白剂体系。",
        severity="high",
    ),
    Rule(
        id="F08",
        name="次氯酸盐 + 过氧化物",
        kind="forbidden_mix",
        side_a=_side("hypochlorite", min_concentration=5.0),
        side_b=_side("peroxide", min_concentration=5.0),
        reason="次氯酸盐与过氧化氢剧烈反应、瞬时大量放氧，并可能夹带氯气，可致喷溅。",
        guidance="两种氧化剂严禁直接混合，废液分开收集。",
        severity="high",
    ),
    # -----------------------------------------------------------------
    # 限时接触（超过允许接触秒数即阻断）
    # -----------------------------------------------------------------
    Rule(
        id="T01",
        name="过氧化物 + 卤盐限时接触",
        kind="limited_contact",
        side_a=_side("peroxide", min_concentration=5.0),
        side_b=_side("halide", min_concentration=5.0),
        reason="过氧化氢与卤离子共存会缓慢生成卤素（Cl₂/Br₂），接触越久逸出越多。",
        guidance="含卤盐体系加入过氧化物后应在允许接触时长内完成操作并尽快分离。",
        severity="high",
        max_contact_seconds=1800.0,  # 30 分钟
    ),
    Rule(
        id="T02",
        name="次氯酸盐 + 铵盐限时接触",
        kind="limited_contact",
        side_a=_side("hypochlorite", min_concentration=3.0),
        side_b=_side("ammonium_salt", min_concentration=3.0),
        reason="次氯酸盐与铵盐缓慢生成氯胺，接触时间越长有毒产物累积越多。",
        guidance="两种组分若必须共用容器，应在允许接触时长内完成并彻底清洗。",
        severity="high",
        max_contact_seconds=600.0,  # 10 分钟
    ),
    # -----------------------------------------------------------------
    # 容器材质不相容 / 限时接触
    # -----------------------------------------------------------------
    Rule(
        id="M01",
        name="氢氟酸严禁使用玻璃容器",
        kind="material_incompatible",
        side_a=_side("fluoride", min_concentration=1.0),
        side_b=_side("material:glass"),
        reason="氟离子腐蚀玻璃中的二氧化硅，生成气态四氟化硅（SiF₄），容器会渗漏破裂且 HF 极危险。",
        guidance="改用 PTFE/HDPE 容器，操作佩戴含葡萄糖酸钙凝胶的专用防护。",
        severity="critical",
    ),
    Rule(
        id="M02",
        name="氢氟酸严禁使用不锈钢容器",
        kind="material_incompatible",
        side_a=_side("fluoride", min_concentration=1.0),
        side_b=_side("material:stainless_steel"),
        reason="HF 穿透并破坏不锈钢钝化膜，造成快速点蚀穿漏。",
        guidance="改用 PTFE 衬里或 HDPE 容器。",
        severity="critical",
    ),
    Rule(
        id="M03",
        name="氢氟酸与天然橡胶限时接触",
        kind="material_incompatible",
        side_a=_side("fluoride", min_concentration=5.0),
        side_b=_side("material:natural_rubber"),
        reason="HF 会使天然橡胶溶胀、失去密封性能，存在渗漏风险。",
        guidance="改用 PTFE 或氟橡胶密封件；必须使用橡胶件时控制接触时长。",
        severity="high",
        max_contact_seconds=300.0,  # 5 分钟
    ),
    Rule(
        id="M04",
        name="强碱与玻璃限时接触",
        kind="material_incompatible",
        side_a=_side("strong_base", min_concentration=10.0),
        side_b=_side("material:glass"),
        reason="浓碱缓慢腐蚀玻璃（生成硅酸盐），并使磨口、活塞永久粘连。",
        guidance="长期盛装改用 HDPE/PP 容器；玻璃容器仅限短时操作并及时清洗。",
        severity="medium",
        max_contact_seconds=1800.0,
    ),
    Rule(
        id="M05",
        name="强酸与天然橡胶限时接触",
        kind="material_incompatible",
        side_a=_side("strong_acid", min_concentration=10.0),
        side_b=_side("material:natural_rubber"),
        reason="强酸使天然橡胶硬化、开裂，丧失密封能力。",
        guidance="改用 PTFE/氟橡胶密封件，或严格控制接触时长。",
        severity="medium",
        max_contact_seconds=3600.0,
    ),
    Rule(
        id="M06",
        name="氧化剂与天然橡胶限时接触",
        kind="material_incompatible",
        side_a=_side("oxidizer", min_concentration=10.0),
        side_b=_side("material:natural_rubber"),
        reason="强氧化剂使橡胶氧化溶胀、加速老化失效。",
        guidance="改用 PTFE 等耐氧化密封材料，或控制接触时长。",
        severity="medium",
        max_contact_seconds=1800.0,
    ),
    Rule(
        id="M07",
        name="强酸与碳钢限时接触",
        kind="material_incompatible",
        side_a=_side("strong_acid", min_concentration=10.0),
        side_b=_side("material:carbon_steel"),
        reason="强酸快速腐蚀碳钢并放出氢气，密闭时有氢爆风险。",
        guidance="改用不锈钢（依酸种评估）或衬塑容器，短时接触须敞口排气。",
        severity="high",
        max_contact_seconds=600.0,
        temp_note_threshold=40.0,
        temp_note="温度升高会显著加快酸对碳钢的腐蚀速率。",
    ),
    Rule(
        id="M08",
        name="强酸与铝限时接触",
        kind="material_incompatible",
        side_a=_side("strong_acid", min_concentration=5.0),
        side_b=_side("material:aluminum"),
        reason="酸破坏铝氧化膜后快速溶蚀金属并放出氢气。",
        guidance="改用 PTFE 衬里或塑料容器，或严格控制接触时长。",
        severity="high",
        max_contact_seconds=300.0,
    ),
    Rule(
        id="M09",
        name="强碱与铝限时接触",
        kind="material_incompatible",
        side_a=_side("strong_base", min_concentration=5.0),
        side_b=_side("material:aluminum"),
        reason="强碱溶解铝及氧化膜，快速放出氢气，密闭容器可发生氢爆。",
        guidance="碱性液体禁用铝质容器/工具，改用 HDPE 或不锈钢。",
        severity="high",
        max_contact_seconds=300.0,
    ),
    Rule(
        id="M10",
        name="浓盐酸与不锈钢限时接触",
        kind="material_incompatible",
        side_a=_side("id:hcl", min_concentration=10.0),
        side_b=_side("material:stainless_steel"),
        reason="氯离子破坏不锈钢钝化膜，造成点蚀与应力腐蚀开裂。",
        guidance="盐酸体系改用 PTFE/HDPE 容器，不锈钢仅限短时过渡。",
        severity="high",
        max_contact_seconds=1800.0,
    ),
    Rule(
        id="M11",
        name="有机溶剂与天然橡胶限时接触",
        kind="material_incompatible",
        side_a=_side("solvent", min_concentration=20.0),
        side_b=_side("material:natural_rubber"),
        reason="醇/酮等有机溶剂使天然橡胶溶胀、软化，密封迅速失效。",
        guidance="改用 PTFE 或耐溶剂橡胶（如氟橡胶）密封件。",
        severity="medium",
        max_contact_seconds=600.0,
    ),
    Rule(
        id="M12",
        name="浓碱与碳钢限时接触",
        kind="material_incompatible",
        side_a=_side("strong_base", min_concentration=20.0),
        side_b=_side("material:carbon_steel"),
        reason="高浓强碱在高温/应力条件下引发碳钢碱脆与腐蚀。",
        guidance="长期盛装改用不锈钢或衬塑容器，短时接触后及时清洗。",
        severity="medium",
        max_contact_seconds=3600.0,
    ),
)

RULES_BY_ID: dict[str, Rule] = {r.id: r for r in RULES}

# 规则库版本，随响应返回，便于结果可复现
RULESET_VERSION = "chem-rules-2026-10-01"
