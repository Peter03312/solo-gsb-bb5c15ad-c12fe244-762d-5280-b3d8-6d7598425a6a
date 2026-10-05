# 化学试剂混配风险预检 HTTP API

提交**容器初始液体、按顺序执行的加液步骤、试剂浓度与体积、容器材质与温度**，
服务逐步计算混配后各组分的浓度，依据内置的公开化学相容性规则识别：

- **禁配组合**（forbidden_mix）：如强酸 + 漂白水 → 氯气、漂白水 + 氨水 → 氯胺、氧化剂 + 易燃物等；
- **超时接触**（overtime_contact）：允许短时接触的反应对超过允许接触秒数（可用 `contact_allowances` 声明覆盖默认值）；
- **材质不相容**（material_incompatible）：如 HF 进玻璃容器、强碱长期接触玻璃、酸对碳钢/铝的腐蚀等。

输出**首次触发步骤、涉及组分、触发时刻的估算浓度与阻断原因**；`dilute`（稀释）、`pour`（倒出）、
`clean`（清洗）会改变后续状态。输入存在问题时**逐项拒绝**（序号重复、体积非正、浓度越界、
未知试剂、清洗后仍引用旧状态等），一次性返回全部错误。

> 用途：实验室/工艺 SOP 的自动化预检与教学。规则库为简化的公开化学常识集合，
> **不能替代** MSDS/SDS 与专业 EHS 评估。

---

## 目录

- [快速开始（Docker Compose）](#快速开始docker-compose)
- [本地运行（不用 Docker）](#本地运行不用-docker)
- [API 说明](#api-说明)
- [逐步状态与浓度模型](#逐步状态与浓度模型)
- [操作类型](#操作类型)
- [规则、阈值与接触计时](#规则阈值与接触计时)
- [错误码（逐项拒绝）](#错误码逐项拒绝)
- [内置试剂 / 材质 / 规则](#内置试剂--材质--规则)
- [调用示例（curl / Python）](#调用示例curl--python)
- [测试](#测试)
- [项目结构](#项目结构)

---

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 服务监听 http://localhost:8000
curl http://localhost:8000/health
curl http://localhost:8000/catalog | python -m json.tool
```

发起一次预检（禁配：往漂白水里加盐酸 → 氯气）：

```bash
curl -s http://localhost:8000/api/v1/precheck \
  -H 'Content-Type: application/json' \
  --data @examples/forbidden_mix.json | python -m json.tool
```

## 本地运行（不用 Docker）

需要 Python 3.11+：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## API 说明

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查，返回规则库版本 |
| `GET` | `/catalog` | 内置试剂、容器材质与全部规则（含选择器、阈值、默认接触时长） |
| `POST` | `/api/v1/precheck` | 提交混配流程，返回风险与逐步状态 |

### 请求体

```json
{
  "vessel_material": "glass",
  "temperature_c": 22.0,
  "initial_liquid": [
    {"reagent_id": "naocl", "concentration": 10.0, "volume_ml": 100.0}
  ],
  "steps": [
    {"seq": 1, "op": "add", "reagent_id": "hcl",
     "concentration": 30.0, "volume_ml": 50.0, "at_seconds": 30}
  ],
  "contact_allowances": []
}
```

| 字段 | 说明 |
| --- | --- |
| `vessel_material` | 容器材质编号，见 `/catalog`（如 `glass`/`hdpe`/`ptfe`/`aluminum`） |
| `temperature_c` | 体系温度 ℃，允许 [-50, 300]；达到部分规则的高温阈值时阻断原因会追加风险提示 |
| `initial_liquid[]` | 容器初始液体；同一试剂不得重复出现 |
| `steps[]` | 按顺序执行的操作；`seq` 为正整数且**不得重复**，乱序提交会按 `seq` 稳定排序处理 |
| `steps[].at_seconds` | 该步骤完成时刻（相对流程起点，秒），非负、不早于上一步；缺省沿用上一步时刻 |
| `contact_allowances[]` | 为反应对声明允许接触时长（秒），覆盖规则默认值；端可写试剂编号或材质编号（`glass` 或 `material:glass` 均可） |

### 响应（发现风险时 HTTP 200，`blocked=true`）

```json
{
  "safe": false,
  "blocked": true,
  "first_blocking_seq": 1,
  "ruleset_version": "chem-rules-2026-10-01",
  "vessel_material": "glass",
  "temperature_c": 22.0,
  "findings": [
    {
      "seq": 1,
      "category": "forbidden_mix",
      "rule_id": "F01",
      "rule_name": "强酸 + 次氯酸盐释放氯气",
      "entities": ["hcl", "naocl"],
      "concentrations": {"hcl": 10.0, "naocl": 6.6667},
      "contact_seconds": null,
      "allowed_seconds": null,
      "severity": "critical",
      "reason": "强酸使次氯酸盐转化为氯气（Cl₂）……",
      "guidance": "严禁向漂白水中加酸……",
      "blocking_reason": "禁配组合：强酸 + 次氯酸盐释放氯气（F01）在步骤 1 首次共存"
    }
  ],
  "steps": [ /* 每个步骤完成后的状态快照，按 seq 排序 */ ],
  "final_state": { /* 末态快照 */ }
}
```

- `findings` 按 `(seq, 类别)` 稳定排序：初始液体触发时 `seq=0`；类别顺序为禁配 → 材质不相容 → 超时接触。
- 每个状态快照包含：`at_seconds`、`liquid_volume_ml`、`cleaned`、`components[]`
  （`reagent_id` / `name` / `concentration` / `pure_volume_ml`）。
- 超时类 finding 另含 `contact_seconds`（实际接触时长）与 `allowed_seconds`（允许时长）。

完整样例见 [`examples/response_blocked.json`](examples/response_blocked.json)。

### 错误响应（HTTP 400，逐项列出）

```json
{
  "error": "validation_error",
  "message": "预检请求被逐项拒绝，未进行模拟",
  "details": [
    {"code": "DUPLICATE_STEP_SEQ", "message": "步骤序号 2 重复", "seq": 2, "field": "seq"},
    {"code": "INVALID_VOLUME", "message": "体积须为正数（毫升），收到 0.0", "seq": 3, "field": "volume_ml"}
  ]
}
```

---

## 逐步状态与浓度模型

采用透明的“**纯物体积**”簿记（体积可加、充分混合假设）：

- 每个试剂记录纯物体积 `pure_ml`，当前浓度 `= pure_ml / 液体总体积 × 100%`；
- **add**：`pure_ml += concentration × volume / 100`，总体积增加；
- **dilute**：仅总体积增加（加纯水），各组分浓度等比下降；
- **pour**：按倒出比例同步缩减总体积与每个 `pure_ml`（浓度不变）；完全倒空会终止全部接触片段；
- **clean**：清空全部组分与总体积、所有接触计时归零，置 `cleaned=true`。

规则只在组分浓度**达到规则侧阈值**（默认 1%，强酸/强碱等规则更高）时生效，
痕量组分不会触发——稀释越过阈值会结束对应接触片段，重新越过则作为**新片段**重新计时。

## 操作类型

| `op` | 必填字段 | 语义 / 状态影响 |
| --- | --- | --- |
| `add` | `reagent_id`, `concentration`, `volume_ml` | 加入试剂；同一试剂多次加入会合并纯物量 |
| `dilute` | `volume_ml` | 加纯水稀释；**清洗后禁止**（旧状态已不存在） |
| `pour` | `volume_ml` | 均匀倒出，不得超过当前总体积；**清洗后/空容器禁止** |
| `clean` | — | 清洗：清空液体并重置接触计时 |
| `hold` | `at_seconds`（严格晚于当前时刻） | 静置等待，仅推进时间用于超时判定；**空容器/清洗后禁止** |

`at_seconds` 缺省沿用上一步时刻；时间只能前进，回退报 `NON_MONOTONIC_TIME`。

## 规则、阈值与接触计时

三类规则（完整列表以 `GET /catalog` 为准）：

- `forbidden_mix`：两种试剂一经共存且双方都过阈值，**立即阻断**（F01–F08）；
- `limited_contact`：试剂-试剂限时接触，超时阻断（T01–T02）；
- `material_incompatible`：试剂-容器材质。无接触时长的（如 HF/玻璃）立即阻断（M01、M02）；
  带时长的（如强碱/玻璃 1800 s、酸/碳钢 600 s）超时阻断。

计时规则：

1. 接触“片段”从**双方（含材质）条件同时满足**的那一刻开始；
2. 时间在下一个操作的 `at_seconds` 推进，推进后检查 `已接触时长 > 允许时长`（严格大于）；
3. 倒空、清洗或任一 方浓度跌破阈值都会结束片段；重新满足则重新计时；
4. `contact_allowances` 按反应对（试剂-试剂 或 试剂-材质）覆盖该对所有限时规则的默认秒数；
   它**不能豁免禁配规则**，且必须为正数、两端为不同类的已知实体，重复声明会被拒绝。

## 错误码（逐项拒绝）

| Code | 触发条件 |
| --- | --- |
| `MALFORMED_REQUEST` | JSON/类型/schema 错误（含未知字段，请求模型禁止额外字段） |
| `DUPLICATE_STEP_SEQ` | 步骤序号重复 |
| `INVALID_SEQ` | 序号非正整数 |
| `UNKNOWN_OPERATION` | 未知 `op` |
| `UNKNOWN_REAGENT` / `UNKNOWN_MATERIAL` / `UNKNOWN_ENTITY` | 未知试剂/材质/allowance 实体 |
| `CONCENTRATION_OUT_OF_RANGE` | 浓度 ≤0、>100，或超过该试剂现实可出现上限（如盐酸 37%） |
| `INVALID_VOLUME` | add/dilute/pour 的体积缺失或 ≤0 |
| `INVALID_STEP_FIELDS` | 操作与字段不匹配（如 dilute 带 reagent_id、clean 带体积） |
| `DUPLICATE_COMPONENT` | 初始液体中同一试剂重复出现 |
| `TEMPERATURE_OUT_OF_RANGE` | 温度超出 [-50, 300] ℃ |
| `INVALID_TIME` / `NON_MONOTONIC_TIME` | 时刻为负、hold 未推进时间、时刻回退 |
| `POUR_EXCEEDS_VOLUME` | 倒出体积超过当前液体体积 |
| `EMPTY_VESSEL_OPERATION` | 容器为空仍 pour/hold |
| `STALE_STATE_REFERENCE` | **清洗后仍对旧状态做 pour/dilute/hold** |
| `INVALID_ALLOWANCE` / `DUPLICATE_ALLOWANCE` | 接触时长声明非法（非正/同实体）或重复 |

## 内置试剂 / 材质 / 规则

- 试剂 21 种：`h2so4, hcl, hno3, hf, acoh, naoh, koh, nh3, nacl, nabr, naocl, h2o2,
  kmno4, k2cr2o7, nh4cl, nh4no3, na2s, ethanol, acetone, formaldehyde, water`
- 材质 7 种：`glass, hdpe, ptfe, natural_rubber, aluminum, carbon_steel, stainless_steel`
- 规则 22 条（F01–F08 禁配、T01–T02 限时、M01–M12 材质），均以分类标签匹配，
  规则文本与版本号见 `app/rules.py` / `GET /catalog`。

## 调用示例（curl / Python）

```bash
# 安全示例：限时接触 + allowance 覆盖
curl -s http://localhost:8000/api/v1/precheck \
  -H 'Content-Type: application/json' \
  --data @examples/allowance_safe.json | python -m json.tool
```

```python
import requests

payload = {
    "vessel_material": "glass",
    "temperature_c": 22.0,
    "initial_liquid": [{"reagent_id": "naocl", "concentration": 10, "volume_ml": 100}],
    "steps": [
        {"seq": 1, "op": "add", "reagent_id": "hcl",
         "concentration": 30, "volume_ml": 50, "at_seconds": 30}
    ],
    "contact_allowances": [],
}
r = requests.post("http://localhost:8000/api/v1/precheck", json=payload, timeout=10)
r.raise_for_status()
result = r.json()
if result["blocked"]:
    f = result["findings"][0]
    print(f"阻断于步骤 {f['seq']}：{f['blocking_reason']}")
    print("涉及组分浓度：", f["concentrations"])
```

也可直接查看 [`examples/`](examples/) 下的三个请求样例与一个响应样例。

## 测试

```bash
pip install pytest httpx
python3 -m pytest -q
```

覆盖：禁配识别与估算浓度、初始液体触发（seq=0）、浓度阈值与稀释、超时接触与 allowance
覆盖、倒空/稀释对接触片段的重置、HF/玻璃等材质规则、清洗后旧状态引用拒绝、全部输入拒绝码、
乱序 seq 的稳定处理（35 个用例）。

## 项目结构

```
app/
  catalog.py    # 试剂 / 材质目录与标签
  rules.py      # 相容性规则库（禁配 / 限时 / 材质）与版本
  models.py     # 请求响应 Pydantic 模型
  errors.py     # 错误码
  validator.py  # 逐项校验 + 归一化执行计划（状态流预检）
  engine.py     # 逐步混配状态机（浓度簿记 + 接触计时）
  main.py       # FastAPI 路由与异常映射
examples/       # curl 可用的请求/响应样例
tests/test_api.py
Dockerfile
docker-compose.yml
```
