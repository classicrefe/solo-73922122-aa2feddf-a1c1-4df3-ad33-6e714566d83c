# 水质平台 · 报文契约发布

固件升级前确认新报文能被分析程序读取、程序升级后仍能读旧归档的契约发布网页。
按设备型号登记**历代固件写入格式**（只追加）与**现用程序读取格式**（按程序号替换），
各至多 8 份。每次发布前用 **z3** 对「全部历代写入格式 × 全部现用程序」做**全量**兼容判定
（逻辑证明，非抽样试样例）：任一写入格式允许的全部报文必须被每个现用程序接受，
否则拒绝发布，并给出**合法反例报文**与**拒收程序**。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

- 操作入口（网页）：http://localhost:8000
- 健康检查：http://localhost:8000/api/health
- 数据保存在命名卷 `pgdata`（PostgreSQL），服务重启后已发布格式保留。

## 网页操作

1. 首页创建设备型号（如 `WQ-5000`），进入型号管理页。
2. 在「发布新格式」粘贴受限 JSON Schema：
   - 左栏**追加固件写入格式**（版本号自动递增，只追加）；
   - 右栏**发布/替换程序读取格式**（同一程序号覆盖旧版）。
3. 发布结果即时展示：成功，或拒绝 + 每个不兼容组合的反例报文与拒收程序；
   页面下方「发布日志」保留最近 50 条成功/拒绝/无效记录（含拒绝依据）。
4. 页面内置三个示例，点按钮即可填入编辑器：
   - **温度扩范围**：固件 v2 扩到 -40~100℃，直接发布会被旧程序（-10~50℃）拒收，
     反例如 `{"temp": -40, ...}`；先升级程序再发固件则通过。
   - **质控码缺省**：固件缺省 `qc`（或写 `null`），把 `qc` 列为 `required` 的程序会被拒，
     反例为不含 `qc` 的报文；程序改为可选并允许 `null` 后通过。
   - **顶层 anyOf**：程序同时接受多种报文形态。

## 受限 JSON Schema 语法（其余一律拒绝）

- 平面对象：`{"type":"object","properties":{...},"required":[...],"additionalProperties":false}`
  （`required` 可省略，默认空；`additionalProperties` 必须显式为 `false`）
- 顶层 `anyOf`：`{"anyOf":[<平面对象>, ...]}`，且只能单独出现在顶层
- 整数上下界：`{"type":"integer","minimum":m,"maximum":M}`（上下界均可省略）
- 字符串/整数/空值枚举：`{"enum":[...]}`，可配 `"type"` 限定；枚举值只允许字符串、整数、`null`
- 空值类型：`{"type":"null"}`

嵌套对象、数组、`pattern`、`oneOf`/`allOf`/`not`、浮点/布尔值、`string` 无枚举等语法均被拒绝。

## 库内筛查表达式（PostgreSQL 16）

旧报文已进入暂存表（报文列约定为 `json` 类型的 `payload`）后，可在型号页
**选择一版已发布的固件写入格式**，生成可直接放入 `WHERE` 子句的布尔表达式，
筛出不符合该版契约的行：

```sql
SELECT * FROM staging WHERE NOT ( <生成的表达式> );   -- 不符合契约的行
SELECT * FROM staging WHERE ( <生成的表达式> );       -- 符合契约的行
```

语义与平台现有的纯 Python 判定（`schema_spec.message_valid`）逐条对齐：

- **区分字段缺省与显式 `null`**：`->` 取字段时，缺省得 SQL NULL、显式 `null`
  得 `'null'::json`，两者天然区分；
- **区分整数写法与 `1.0`、`1e0` 等写法**：`json` 列保留数字原始文本，
  用整数写法正则 `^-?(0|[1-9][0-9]*)$` 判定（`-0` 视为整数 0）；
- **支持顶层 `anyOf`**：各分支表达式以 `OR` 连接；
- **每行只返回真或假**：数值比较在 `CASE` 保护内完成（不会对非整数文本误做
  `::numeric` 转换），最外层 `COALESCE(..., false)`，payload 为 SQL NULL 判假；
- 键集合检查用 `CASE` 保护的 `json_object_keys`（不用 `::jsonb`——大指数数字
  如 `1e999999` 会让 jsonb 报 `value overflows numeric`）。

**试跑对照**：型号页粘贴逐行 JSON（每行一条报文），系统在数据库内执行拟交付的
表达式并与 Python 判定逐条对照。**全部一致才展示可交付表达式；试跑出错或结果
有分歧时说明原因、暂不提供表达式。** 非法/非标准 JSON 行（如 `NaN`）两端均无法
读取，不参与对照并单独标注。

已知边界（不属于"两端均可读取的标准 JSON"）：含未成对代理项转义（`\ud800` 等）
的报文 PostgreSQL 无法处理，试跑会报错并暂不提供表达式；重复键两端均取最后出现
的值，行为一致但非标准用法。

## 校验原理

对每对（写入格式 W，读取格式 R），把报文编码为 z3 变量（每个字段：是否出现、
值类型 null/int/string、整数值、字符串枚举下标），求解 `W 合法 ∧ R 拒收`：

- **UNSAT** ⇒ 不存在反例，`L(W) ⊆ L(R)` 成立——这是对全部报文的证明，不是抽样；
- **SAT** ⇒ 从模型还原一个反例报文，并用独立的纯 Python 判定器复核
  （确实满足 W、确实被 R 拒收）后随拒绝结果返回。

## 并发与事务

- 每次发布在事务内先对型号行加锁（PostgreSQL `SELECT ... FOR UPDATE`），
  再基于最新已提交状态做**全量交叉检查**后落库——并发升级被串行化，
  不会各自通过却留下不兼容组合；
- 检查失败整体回滚，**已发布格式不变**；拒绝/无效尝试另写发布日志留痕；
- 固件格式只追加（无修改/删除入口），程序格式按程序号替换；各至多 8 份。

## JSON API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/models` | 创建型号 `{"name": "WQ-5000"}` |
| GET | `/api/models/{name}` | 查看当前固件/程序格式 |
| GET | `/api/models/{name}/log` | 发布日志（含拒绝依据） |
| POST | `/api/models/{name}/firmware` | 追加固件格式 `{"schema": {...}}` |
| POST | `/api/models/{name}/programs` | 发布/替换程序格式 `{"program_id": "...", "schema": {...}}` |
| POST | `/api/models/{name}/firmware/{v}/sqlcheck` | 选固件 v 生成表达式并库内试跑 `{"lines": ["{...}", ...]}` |

响应：成功 `200 {"ok": true, ...}`；不兼容 `409 {"ok": false, "problems": [{firmware_version, program_id, counterexample}]}`；语法/上限错误 `422`。

`/sqlcheck` 响应：全部对照一致时 `200 {"ok": true, "expression": "...", "results": [...]}`
（`expression` 即可交付的 WHERE 布尔表达式）；试跑出错或有分歧时 `422` 且**不返回**
`expression`，`results` 逐行给出 Python/库内判定与原因（`agree`/`diverge`/`skipped`/
`sql-error`）。库内试跑仅在 PostgreSQL 下可用（表达式面向 PostgreSQL 16）。

## 本地开发与自测

```bash
pip install -r app/requirements.txt httpx
python3 tests/test_units.py                 # 语法子集 + z3 判定 + SQL 生成结构自测
cd app && DATABASE_URL=sqlite:////tmp/contracts.db uvicorn main:app --port 8000 &
python3 tests/selftest.py                   # 集成自测（示例/上限/并发/页面；接 PG 时含库内试跑）
DIFF_DATABASE_URL=postgresql://water:water@127.0.0.1:5432/contracts \
  python3 tests/test_sql_diff.py            # 库内差分：表达式 vs Python 逐条对照（需 PG 16）
```

`DATABASE_URL` 缺省为 SQLite（便于本地自测）；Compose 中注入 PostgreSQL 连接串。
库内试跑（`/sqlcheck`）需要 PostgreSQL；SQLite 下该功能会明确提示不可用，其余功能不受影响。

## 目录结构

```
app/
  main.py         FastAPI 入口（HTML 页面 + JSON API）
  service.py      发布事务：加锁 → 全量检查 → 落库/回滚
  solver.py       z3 编码与全量兼容判定（含反例复核）
  schema_spec.py  受限 JSON Schema 子集解析与纯 Python 报文判定
  sqlgen.py       契约 → PostgreSQL 16 WHERE 布尔表达式编译器
  sqltrial.py     库内试跑：执行表达式并与 Python 逐条对照，一致才交付
  db.py           SQLAlchemy 模型（PostgreSQL JSONB / SQLite JSON）
  examples.py     温度扩范围、质控码缺省、anyOf 示例与试跑示例报文
  templates/      页面模板
tests/
  test_units.py   单元自测（含 SQL 生成结构）
  selftest.py     集成自测（含库内试跑全流程）
  test_sql_diff.py 库内差分测试（需 PostgreSQL 16）
docker-compose.yml
```
