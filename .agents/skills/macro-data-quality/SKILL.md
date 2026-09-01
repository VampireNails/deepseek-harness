---
name: macro-data-quality
description: 宏观数据采集/校验/清洗与派生指标治理的硬规则。当需要采集宏观指标（中国 NBS、美国 BLS、东财第三方兜底）、校验数据（verify 失败、月份正则）、清洗重建（clean 库）、或处理派生指标（observed/derived 分层）时使用。含源可信度红线、vintage 只增不修、月份正则陷阱、季度 GDP 频率、派生指标不得冒充官方一手等铁律。
---

# 宏观数据质量铁律（macro-analysis）

> 定位：本 agent 是「数据基石」，价值 = 实时、准确、可靠的**数据**，不是预测。数据层只增不修、只读对外；学习产物写独立库，绝不回写数据层。
> 权威底稿：`scripts/macro-analysis/flywheel-SOP.md`；脚本：`scripts/macro-analysis/{collect,verify,clean}_macro_data.py` + `macro_mcp_server.py`。

## 1. 源可信度红线（最高优先级）

- 分级：**官方一手 > 官方二次 > 第三方**（`source_registry.priority` 越小越可信）。
- **严禁把第三方源冒充官方一手**；`authority`/`attribution` 必须如实记录。
- 一手不可达时的兜底源（如 FRED）必须标记真实来源，不得伪称一手。
- 当前源现状：

| 指标 | 源 | 分级 |
|---|---|---|
| 中国 CPI 同比/环比、PPI 同比、制造业/非制造业/综合 PMI、GDP、M0/M1/M2、城镇调查失业率 | **NBS**（`nbsc` 库，已接入 18 个） | 官方一手 |
| 中国 `cpi_base`/`ppi_base`（实为上年同月=100，即同比+100 的冗余派生）、`ppi_accumulated` | 东方财富 | 第三方（不冒充一手、不补） |
| 美国 非农/失业率 | **BLS**（series id） | 官方一手 |
| 美国 兜底 | FRED（数据归属 BLS） | 官方二次 |

## 2. 采集铁律（collect_macro_data.py）

- **vintage 只增不修**：append-only；`insert_vintage` 幂等；同 (指标,国家,period) 新值标记 `is_revision`，旧版本存档不覆盖（防前视偏差）。
- **NBS 接口**：easyquery 已于 2026-05 废弃（403），一律用 `nbsc` 库封装的新 UUID API（`data.stats.gov.cn` 直连、免代理）。
- **月份正则陷阱**：normalize/norm period 的正则必须写 `(1[0-2]|0?[1-9])`（大数分支在前）。误写 `(0?[1-9]|1[0-2])` 会把 10/11/12 月错配成 "01"，导致 10/11/12 月数据在采集时被静默丢弃。
- **季度 GDP**：period 归一为季末月（`2025Q4`→`2025-12`）。

## 3. 校验铁律（verify_macro_data.py）

- Verify 失败不得进入 Clean（strict 下阻断）。
- 失败案例库 `outputs/macro_verify.sqlite`（`verify_failures` + `fail_tag` 模式标签 + 跨日期历史命中）。
- 派生行校验：`transform_version`/`derived_from` 非空、值区间合理。

## 4. 清洗/派生铁律（clean_macro_data.py）

- 频率：`freq=Q` 存季末月、**不做**月度 period_range/插值/STL；`freq=M` 正常。
- **派生指标 observed/derived 严格分层**：
  - 派生值 `source='derived'`，**绝不**写官方源名（nbs/bls…）、**绝不**覆盖官方出版值；官方出版值优先，公式派生仅缺口兜底。
  - `gdp_qoq` 是 NBS **季节调整后环比**，与未季调 `gdp_real` 口径不可比 → **不做**公式对账/兜底，仅观测记录。
  - 真·派生（可对账）：`m0/m1/m2_yoy`（水平同比）、`nonfarm_payroll_change`（level 差分）。
- 派生血缘：`layer`/`derived_from`/`transform`/`transform_version`/`computed_at`。

## 5. 新增维度（一次性注册，3 处）

- collect：`NBS_INDICATORS`（nbsc 函数+口径转换）/ `BLS_SERIES`（series_id+单位）/ `CHINA_SOURCES`（东财字段）；新源在 `init_db` 的 `source_registry` 加一行。
- clean：`METRICS`（label/unit/sa，sa=True 才做 STL）+ `KEEP_VALUE_TYPE`（保留的 value_type）。
- verify（可选）：`REQUIRED_INDICATORS`。
- 注册后每日自动化自动持续导入，5 张表自动跟踪（`vintage_traces`/`collection_checks`/`verify_failures`/`revision_stats`/`source_trust`/`indicators`），零额外代码。详见 `flywheel-SOP.md` §8。

## 6. 独立学习库（不回写数据层）

`outputs/macro_verify.sqlite`（校验失败）、`outputs/macro_clean.sqlite`（`revision_stats` + `source_trust`）、`outputs/macro_discovery.sqlite`（候选维度，pending→approved→registered）。
