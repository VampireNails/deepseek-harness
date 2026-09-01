# 宏观数据 MCP Server —— 接入说明

本地 stdio 传输的 MCP server，向任意 MCP 客户端（Claude Desktop / Cline / Cursor / 自定义客户端）提供**清洗后的宏观数据库**只读访问。不做预测、不自我学习，只暴露数据接口。

## 1. 组件一览

| 文件 | 作用 |
| --- | --- |
| `scripts/macro-analysis/macro_mcp_server.py` | MCP server（stdio，mcp 2.0 low-level `Server`） |
| `scripts/macro-analysis/clean_macro_data.py` | 进阶清洗脚本：合并原始库 → 源优先级去重 → 插补(MAX_GAP=6) → STL 季节调整 → 写 `outputs/macro_clean.sqlite` |
| `scripts/macro-analysis/collect_macro_data.py` | 原始采集（东财/BLS/FRED），append-only vintage 原始库 |
| `scripts/macro-analysis/test_mcp_server.py` | 端到端自检（官方 MCP 客户端跑一遍 5 个工具） |
| `outputs/macro_clean.sqlite` | 清洗结果库（对外暴露的数据源） |

依赖：managed venv `C:\Users\Administrator\.workbuddy\binaries\python\envs\default`（已装 `mcp 2.0.0`、`pandas`、`numpy`、`statsmodels`）。

## 2. 前置：确保清洗库存在

server 只读 `outputs/macro_clean.sqlite`，不存在会报错。生成/更新：

```bat
:: 先采集原始（可选；已有原始库可跳过）
%VENV%\Scripts\python.exe scripts\macro-analysis\collect_macro_data.py
:: 跑清洗，生成 macro_clean.sqlite
%VENV%\Scripts\python.exe scripts\macro-analysis\clean_macro_data.py
```

`%VENV%` = `C:\Users\Administrator\.workbuddy\binaries\python\envs\default`

> 每日自动化：定时任务「宏观数据每日 vintage 采集」（每天 10:00）已在采集 + 严格校验后自动执行 `clean_macro_data.py` 重建清洗库，故 `macro_clean.sqlite` 会自动保持最新，MCP server 无需手动刷新。

## 3. 客户端接入配置

在客户端的 MCP 配置里加入（路径用双反斜杠或正斜杠）：

```json
{
  "mcpServers": {
    "macro-clean-db": {
      "command": "C:\\Users\\Administrator\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe",
      "args": ["D:\\tmp\\deepseek-harness\\my-deepseek-harness\\deepseek-harness\\scripts\\macro-analysis\\macro_mcp_server.py"]
    }
  }
}
```

- 自定义数据库路径：加 `"env": { "MACRO_CLEAN_DB": "D:\\path\\to\\macro_clean.sqlite" }`；不设置则用脚本同级 `outputs/macro_clean.sqlite`。
- server 仅 stdio、无网络、只读，客户端以子进程方式拉起。

## 4. 工具清单（7 个）

所有工具返回单个 JSON 对象（包在一条 text 内容块里），便于客户端解析。

| 工具 | 必填参数 | 说明 | 返回 |
| --- | --- | --- | --- |
| `list_indicators` | — | 列出可用指标（中美 CPI/PPI/PMI/非农/失业率/GDP/M0/M1/M2） | `{indicators:[{indicator,country,label,unit,frequency,sa_method,first_period,last_period,n_obs,n_imputed,last_updated}]}` |
| `get_series` | `indicator` | 规范月度时序；`country` 省略=全部国家；`field`∈`value`(默认,原始规范观测,始终有值)/`value_sa`(季节调整后或插补回退)/`value_imputed`(插补填充值)；`start`/`end` 形如 `2024-01` | `{indicator,country,field,data:[{period,country,value,is_imputed,source,layer,derived_from,transform}]}` |
| `get_latest` | `indicator` | 各匹配国家最新一期（含原始/季节调整后/插补值、来源、发布日、采集时点） | `{indicator,country,latest:[{country,period,value,value_sa,value_imputed,is_imputed,source,release_date,collected_at}]}` |
| `get_vintage` | `indicator`,`period` | 某发布周期的历次采集快照（vintage）。免费源返回稳定修订值，故记录每次采集的「值变化点」 | `{indicator,period,country,revisions:[{country,collected_at,value,original_value,is_revision,source,value_type}]}` |
| `get_metadata` | `indicator` | 指标元数据 | `{indicator,country,metadata:[{...同 list_indicators 字段}]}` |
| `get_source_trust` | — | 数据源可信度分级（官方一手 > 官方二次 > 第三方），含 authority/attribution/priority | `{sources:[{source,authority,trust_level,attribution,priority}]}` |
| `get_derivation` | `indicator` | 派生血缘与一致性对账（数据基石透明度）：`layer`(observed/derived)、`derived_from`(底层源)、`transform`/`transform_version`/`computed_at`；有官方出版值时同时返回 `derived_checks` 的「观测值 vs 公式派生值」偏差与一致性标记 | `{indicator,country,lineage:[...],consistency_checks:[...]}` |

参数缺失时返回 `isError=true` + `{"error":"..."}`（不会让 server 崩溃）。

## 5. 自检

```bat
%VENV%\Scripts\python.exe scripts\macro-analysis\test_mcp_server.py
```

预期：完成 `initialize` 握手、列出 6 个工具、逐个调用返回合法 JSON，最后一行为 `PASS 7/8 calls returned valid non-error JSON`（第 8 个是故意触发的报错用例）。

## 6. 数据事实与边界（已核验）

- 清洗库当前规模：`clean_series` 1519 行、`indicators` 24 个指标/国家组合（21 中国 + 3 美国）、`vintage_traces` 1478 行、`derived_checks` 33 条一致性对账。
- 指标键（24）：`cpi_base`/`cpi_mom`/`cpi_yoy`/`ppi_base`/`ppi_mom`/`ppi_yoy`/`ppi_accumulated`/`manufacturing_pmi`/`nonmanufacturing_pmi`/`composite_pmi`/`cn_unemployment_rate`/`m0`/`m0_yoy`/`m1`/`m1_yoy`/`m2`/`m2_yoy`/`gdp_nominal`/`gdp_real`/`gdp_yoy`/`gdp_qoq`（CN）、`nonfarm_payroll_change`/`nonfarm_payroll_level`/`unemployment_rate`（US）。
- 历史长缺口（如 `nonfarm_payroll_change` 回溯至 1939）：因 `MAX_GAP=6` 上限，超过 6 期的空洞**保持 NULL，不虚构历史**；这些点的 `value_sa`/`value` 可能为 `null`，属预期。
- 数据源可信度分级（`get_source_trust` / 清洗库 `source_trust` 表）：`nbs`=官方一手、`bls`=官方一手、`fred_csv`=官方二次、`eastmoney`=第三方。中国 18 个核心指标（CPI 同比/环比、PPI 同比/环比、制造业/非制造业/综合 PMI、M0/M1/M2 及同比、城镇调查失业率、GDP 现价/不变价/同比/环比）已用 NBS 官方一手源；`cpi_base`/`ppi_base`/`ppi_accumulated` 定基/累计指数仍用东财（NBS 免费接口未封装）。
- 派生层（`get_derivation`）：`m0/m1/m2_yoy`、`nonfarm_payroll_change` 为公式派生（`yoy_from_level`/`diff_level`），与官方出版值做一致性对账（`derived_checks` 33 条，全 consistent）；官方出版值存在时不覆盖、不兜底，故 `layer='derived'` 行数为 0。`gdp_qoq` 因 SA 口径不可比，仅观测记录，不入派生规格。
- 修订捕获：官方一手源（BLS）有真实修订——`nonfarm_payroll_change` 捕获 79 次修订（平均幅度 145.6 千人）；第三方源（东财）返回稳定修订值、`is_revision` 恒为 0。vintage 语义 = 每次采集的值变化点。
- `value_sa`：对 level 型指标做 STL 季节调整；对 `not_applicable` 型（如同比、PMI）回退为插补序列，保证该字段对外不为空。
