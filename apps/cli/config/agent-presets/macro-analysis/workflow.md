# macro-analysis Agent — 宏观数据采集与清洗工作流

本文件是 Web preset 与 headless overlay 的唯一工作流来源。每次固定任务严格按
**Collect → Verify → Store → Clean** 推进；两端不得维护第二份内联版本。

## 0. 核心定位与固定产物

- 核心使命：每日按发布日历采集中美宏观指标，保存 vintage 快照与修订历史，构建清洗后的宏观分析数据库，通过 MCP server 对外只读提供。不做预测、不做自我学习。
- 工作区脚本：`scripts/macro-analysis/collect_macro_data.py`、`scripts/macro-analysis/verify_macro_data.py`、`scripts/macro-analysis/clean_macro_data.py`、`scripts/macro-analysis/macro_mcp_server.py`。
- 原始数据库：`outputs/<YYYY-MM-DD>/macro_indicators.sqlite`。
- 清洗数据库：`outputs/macro_clean.sqlite`（MCP server 的只读数据源）。
- 采集报告：`outputs/<YYYY-MM-DD>/macro_collection_report.md`。
- 必需指标：`cpi_yoy`、`ppi_yoy`、`manufacturing_pmi`、`nonfarm_payroll_level`、`nonfarm_payroll_change`、`unemployment_rate`。

## 1. 固定数据源与来源分级

### 中国

- 东方财富宏观数据中心：`https://datacenter-web.eastmoney.com/api/data/v1/get`。
- 固定报表：`RPT_ECONOMY_CPI`、`RPT_ECONOMY_PPI`、`RPT_ECONOMY_PMI`。
- 主要字段：CPI `NATIONAL_SAME/NATIONAL_BASE/NATIONAL_SEQUENTIAL`；PPI `BASE_SAME/BASE/BASE_ACCUMULATE`；PMI `MAKE_INDEX/NMAKE_INDEX`。
- 来源等级：第三方结构化源，报告中不得表述为统计机构原始响应。

### 美国

- 首选官方一手源：BLS Public Data API v2，`CES0000000001`（非农总量）与 `LNS14000000`（失业率）。
- BLS 网络不可达、403 或响应异常时才使用 FRED graph CSV：`PAYEMS`、`UNRATE`；明确标记为数据归属 BLS 的官方二次发布。
- 代理不是换源：代理成功时 `source=bls`；只有真实使用 FRED 才标记 `source=fred_csv`。

## 2. 网络代理契约

- 配置优先级：`--proxy-url` → `MACRO_PROXY_URL` → `MACRO_PROXY_CONFIG` 指向的文件 → `scripts/macro-analysis/macro_proxy.env` → `HTTPS_PROXY/HTTP_PROXY/ALL_PROXY`。
- 只支持 HTTP/HTTPS 代理；SOCKS 必须由本机代理软件提供 HTTP 监听端口，采集器不得静默绕过。
- 代理凭据不得写入 stdout、报告、SQLite、`raw_json` 或错误日志；报告最多显示代理主机/端口。
- 代理关闭时按固定降级：记录 BLS 失败 → 尝试 FRED → 两者都失败则 Proof 失败，不编造数据。

## 3. 发布日历

| 指标 | 频率 | 常见发布时间 |
|---|---|---|
| 中国 CPI/PPI | 月度 | 每月 9–15 日 |
| 中国官方制造业 PMI | 月度 | 每月最后一日 |
| 美国非农/失业率 | 月度 | 每月首个周五 |
| 中国 GDP | 季度 | 季度首月月中 |

每日任务即使没有新发布，也要执行检查并写入 `collection_checks`，不把“无更新”当成失败。

## 4. Collect（采集）

- 固定入口：`python scripts/macro-analysis/collect_macro_data.py --output-root outputs --date YYYY-MM-DD`。
- 每个来源记录成功、空结果或错误；原始响应保存到 `raw_json`，统一把 `period` 归一化为 `YYYY-MM`。
- Collect 只写 `macro_collection_report.md`；不得覆盖清洗库或历史报告。
- `release_date` 只有来源返回真实发布日期时才填写，不得用通常发布日推算冒充。
- Agent 不应临时改写数据源逻辑；需要探测新源时另行记录，不覆盖固定采集结果。

## 5. Verify（独立 Proof）

- 固定入口：`python scripts/macro-analysis/verify_macro_data.py --output-root outputs --date YYYY-MM-DD --strict`。
- Proof 独立读取 SQLite，不采信 Agent 自述；检查数据库、报告、必需指标、非空值、当前批次 source check、美国 BLS/FRED 至少一路成功。
- strict 额外检查 raw/period 一致性、vintage 唯一键、修订行的原始值、报告与数据库行数/修订数对账。
- Proof 不通过，必须停止后续步骤，先报告缺口与错误；清洗与 MCP 只读服务不受影响（清洗只处理已入库的真实数据）。

## 6. Store（vintage 入库）

- 每条记录保留：`indicator_name/country/period/value/value_type/release_date/collected_at/source/source_series/raw_json/is_revision/original_value`。
- 同一指标、国家、周期、口径、来源、采集时点同值幂等，不重复插入。
- 同一指标/国家/周期/口径/来源在更晚采集时点出现不同值，只能 append-only 追加 `is_revision=1`，`original_value` 保留首值，禁止 UPDATE 覆盖历史。
- 修订必须来自更晚采集时点；同批次重复不得标为修订。

## 7. Clean（清洗入库）

- 固定入口：`python scripts/macro-analysis/clean_macro_data.py --output-root outputs`。
- 幂等重建 `outputs/macro_clean.sqlite`：源优先级去重 → 单位/口径归一 → 周期对齐(YYYY-MM) → 缺失值插补(线性，MAX_GAP=6 上限，长缺口保持 NULL 不虚构) → STL 季节调整(仅 level 型指标)。
- 产物三表：`clean_series`(规范时序)、`indicators`(指标元数据)、`vintage_traces`(每次采集的值变化点)。
- 清洗只做确定性变换，不做预测、不做趋势判断；对外由 `scripts/macro-analysis/macro_mcp_server.py` 以本地 stdio 只读暴露。

## 红线

1. 严禁编造数据、URL、发布日期或模型结论。
2. 不得混用修订值与首次公布值而不声明时间点。
3. 不得把 Agent 自述当作 Proof。
4. Verify 失败不得进入 Clean；清洗不臆造历史（长缺口保持 NULL）。
