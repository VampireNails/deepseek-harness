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

## 4. 工具清单（5 个）

所有工具返回单个 JSON 对象（包在一条 text 内容块里），便于客户端解析。

| 工具 | 必填参数 | 说明 | 返回 |
| --- | --- | --- | --- |
| `list_indicators` | — | 列出可用指标（中美 CPI/PPI/PMI/非农/失业率） | `{indicators:[{indicator,country,label,unit,frequency,sa_method,first_period,last_period,n_obs,n_imputed,last_updated}]}` |
| `get_series` | `indicator` | 规范月度时序；`country` 省略=全部国家；`field`∈`value`(默认,原始规范观测,始终有值)/`value_sa`(季节调整后或插补回退)/`value_imputed`(插补填充值)；`start`/`end` 形如 `2024-01` | `{indicator,country,field,data:[{period,country,value,is_imputed,source}]}` |
| `get_latest` | `indicator` | 各匹配国家最新一期（含原始/季节调整后/插补值、来源、发布日、采集时点） | `{indicator,country,latest:[{country,period,value,value_sa,value_imputed,is_imputed,source,release_date,collected_at}]}` |
| `get_vintage` | `indicator`,`period` | 某发布周期的历次采集快照（vintage）。免费源返回稳定修订值，故记录每次采集的「值变化点」 | `{indicator,period,country,revisions:[{country,collected_at,value,original_value,is_revision,source,value_type}]}` |
| `get_metadata` | `indicator` | 指标元数据 | `{indicator,country,metadata:[{...同 list_indicators 字段}]}` |

参数缺失时返回 `isError=true` + `{"error":"..."}`（不会让 server 崩溃）。

## 5. 自检

```bat
%VENV%\Scripts\python.exe scripts\macro-analysis\test_mcp_server.py
```

预期：完成 `initialize` 握手、列出 5 个工具、逐个调用返回合法 JSON，最后一行为 `PASS 6/7 calls returned valid non-error JSON`（第 7 个是故意触发的报错用例）。

## 6. 数据事实与边界（已核验）

- 清洗库当前规模：`clean_series` 1336 行、`indicators` 11 个指标/国家组合、`vintage_traces` 1235 行。
- 指标键：`cpi_base`/`cpi_mom`/`cpi_yoy`/`ppi_base`/`ppi_yoy`/`ppi_accumulated`/`manufacturing_pmi`/`nonmanufacturing_pmi`（CN）、`nonfarm_payroll_change`/`nonfarm_payroll_level`/`unemployment_rate`（US）。
- 历史长缺口（如 `nonfarm_payroll_change` 回溯至 1939）：因 `MAX_GAP=6` 上限，超过 6 期的空洞**保持 NULL，不虚构历史**；这些点的 `value_sa`/`value` 可能为 `null`，属预期。
- 免费数据源（东财/BLS/FRED）返回的是稳定修订值，`is_revision` 恒为 0；vintage 语义改为记录「每次采集的值变化点」，而非官方初值 vs 修订值。
- `value_sa`：对 level 型指标做 STL 季节调整；对 `not_applicable` 型（如同比、PMI）回退为插补序列，保证该字段对外不为空。
