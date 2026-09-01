# 宏观数据基石 — 越用越好的三飞轮 SOP

> 本文件是「宏观分析 agent 如何作为数据基石越用越好」的权威方案与落地底稿，随每次落地追加演进记录。
> **核心定位（2026-08-24 用户明确纠正）**：这个 agent 的价值 = **提供实时、准确、可靠的数据，不是预测预判**。
> 「越用越好」= 数据质量的**实时性、准确性、可靠性**持续提升；数据层只增不修、只读对外，学习产物写独立库、绝不回写数据层。

## 1. 数据质量三维度 × 三飞轮

| 维度 | 飞轮 | 借鉴来源 | 核心思想 | 落地方式 |
|---|---|---|---|---|
| 可靠性 | ① 数据厚度飞轮 | ALFRED（联储 Archival FRED） | vintage = "当时已知信息"的时点快照；引用必带采集日期；旧版本存档不覆盖 | 已有 append-only vintage + `collected_at`；补 `revision_stats` 量化初值→终值偏差 |
| 准确性 | ② 校验智慧飞轮 | Great Expectations / dbt / Soda | expectation 声明"好数据长什么样" + 失败留档(run history) + 分层 severity | 失败案例库 + 模式标签 + 历史命中（已落地轮 A） |
| 可信度 | ③ 源可信度治理飞轮 | FAIR / data provenance 分级 | 源分级：官方一手 > 官方二次 > 第三方；一手优先、多源交叉对账 | 源注册表已有 priority；待补：中国官方一手源 + 交叉对账 + 可信度评分 |

## 2. 数据源可信度现状（2026-08-24 核验）

| 国家 | 指标 | 当前源 | 是否官方一手 | 可信度 |
|---|---|---|---|---|
| 美国 | 非农 / 失业率 | BLS（美国劳工统计局） | ✅ 官方一手 | 高 |
| 美国 | （BLS 不可达时兜底） | FRED（联储，数据归属 BLS） | ⚠️ 官方二次发布 | 中 |
| 中国 | CPI 同比/环比、PPI 同比、制造业/非制造业 PMI | **NBS（国家统计局）** | ✅ 官方一手 | 高 |
| 中国 | CPI/PPI 同比指数、PPI 累计同比指数 | 东方财富 | ❌ 第三方（冗余派生/累计，评估后不补） | 低 |

**已补齐**：中国 5 个核心指标（CPI 同比/环比、PPI 同比、制造业/非制造业 PMI）已切 NBS 官方一手源（2026-08-24）。

**剩余 3 个指标评估结论（自行核验确认）**：
- 经 raw_json 核验，`cpi_base`/`ppi_base` 实为「上年同月=100」指数（= 同比 + 100），**并非**原标注的「2016=100」定基指数——unit 标注已修正。
- `cpi_base`/`ppi_base` 是同比的冗余派生，同比本身已是 NBS 官方一手，**无需再补一手源**。
- `ppi_accumulated` 是累计同比指数（年初至今累计），NBS 免费接口（nbsc 26 系列）未封装；补需 Playwright 发现 UUID 且会随 NBS 改版失效，**投入产出比低，不补**，保持东财第三方。

## 3. 三飞轮架构

```
① 数据厚度飞轮（已在跑：Collect→Verify→Store→Clean→MCP 只读）
        ↓ 反哺可靠性
② 校验智慧飞轮（轮 A：verify 失败案例库 + 模式命中，已落地）
        ↓ 反哺准确性
③ 源可信度治理飞轮（轮 B/C：一手源优先 + 多源交叉对账 + 可信度评分）
```

## 4. 数据模型（独立库表，均不回写数据层）

- 轮 A：`outputs/macro_verify.sqlite` → `verify_failures`（已落地）
- 轮 B：`outputs/macro_clean.sqlite` 增 `revision_stats`（初值/终值/平均修订幅度）+ `source_trust`（源可信度分级）
- 轮 C：中国官方一手源接入（NBS 等探测）+ 多源交叉对账表（源间偏差记录）

## 5. 落地清单

- [x] 轮 A：verify 失败案例库 + 模式命中
- [x] 轮 B：`revision_stats` + `source_trust` 源可信度分级 + MCP `get_source_trust` 工具
- [x] 轮 C：中国官方一手源 NBS 接入（nbsc 库），5 核心指标切换官方一手

## 6. 红线

1. 数据层只增不修；学习产物只写独立库，绝不回写 `macro_indicators` / `macro_clean`。
2. Verify 失败不得进入 Clean（沿用）。
3. **严禁把第三方源冒充官方一手**；源归属（authority/attribution）必须如实记录。
4. 一手源不可达时的兜底源（如 FRED）必须标记真实来源，不得伪称一手。

## 7. 演进记录

- 2026-08-24 定稿三飞轮 + 开源借鉴；落地轮 A（校验智慧飞轮）。
- 2026-08-24 方向纠正：定位从「预测预判越用越聪明」改为「数据实时/准确/可靠越用越好」；飞轮③ 从「案例沉淀」改为「源可信度治理」。
- 2026-08-24 轮 B+轮 C 落地并验证：`revision_stats`+`source_trust`+MCP `get_source_trust`；NBS 官方一手源接入（easyquery 已于 2026-05 废弃，改用 nbsc 库封装的新 UUID API），5 核心中国指标切换官方一手。验证发现：BLS 一手源 `nonfarm_payroll_change` 捕获 79 次修订（平均 145.6 千人），东财第三方 n_rev=0——量化印证「官方一手有修订可捕获、第三方没有」。
- 2026-08-24 新增飞轮④（候选发现）：落地 `discover_candidates.py`（官方覆盖度扫描 v1），枚举 nbsc 25 官方系列差集出 17 个候选（13 核心：GDP×4、M0/M1/M2×6、城镇调查失业率、综合 PMI、PPI 环比）。新闻反查(GDELT)/FRED 日历/NBS 树 diff 评估后延后（噪声/需 key/易碎）。

## 8. 新增统计维度：持续导入与跟踪 SOP

**核心结论**：新增一个维度只需「注册一次」，之后每日自动化流水线（collect→verify→clean）自动持续导入；跟踪由 5 张表 + 源可信度自动完成，新增维度零额外代码。

### 8.1 注册一处新维度（一次性，3 处配置）

按维度所属采集源，在 `collect_macro_data.py` 加一处：
- 中国官方一手（优先）：`NBS_INDICATORS` 加 `{指标: (nbsc函数名, 口径转换lambda)}`，并确认 `nbsc` 已装。
- 中国第三方兜底：`CHINA_SOURCES` 加 `{reportName: [(指标, 东财字段)]}`。
- 美国官方一手：`BLS_SERIES` 加 `{指标: (series_id, 单位)}`。
- 新数据源：在 `init_db` 的 `source_registry` 加一行（source/authority/endpoint/access_mode/attribution/priority/active），priority 越小越可信。

在 `clean_macro_data.py` 加两处：
- `METRICS` 加 `{指标: {label, unit, sa}}`（sa=True 才做 STL 季节调整）。
- `KEEP_VALUE_TYPE` 加 `{指标: 保留的 value_type}`（如 `"reported"` / `"level_thousand_sa"`）。

（可选）在 `verify_macro_data.py` 的 `REQUIRED_INDICATORS` 加该指标，使其参与强制校验。

### 8.2 持续导入（自动）

注册后无需任何手动操作：本机每日自动化（10:14 已验证）触发 `collect_macro_data.py --date <今日>`，对新维度执行 append-only 插入（`insert_vintage` 幂等、修订自动标记 `is_revision`）；随后 `verify`（strict，失败落 `verify_failures` 库）+ `clean`（重建 `macro_clean.sqlite`，新维度自动进入 `clean_series`/`indicators`/`vintage_traces`）。

### 8.3 跟踪（自动）

| 表 | 自动跟踪什么 | 何时写入 |
|---|---|---|
| `vintage_traces` | 每个 (指标,国家,period) 的值变化点 = 修订轨迹 | collect 每次 |
| `collection_checks` | 每源每批 ok/empty/error + detail | collect 每次 |
| `verify_failures` | 校验失败模式（fail_tag 标签 + 历史命中） | verify 每次 |
| `revision_stats` | 修订次数 / 平均修订幅度 | clean 每次 |
| `indicators` | 覆盖区间 / 观测数 / 插补数 / 更新时间 | clean 每次 |
| `source_trust` | 源可信度分级（官方一手>二次>第三方） | clean 每次 |

### 8.4 暴露

clean 库经 MCP 6 工具只读暴露（`list_indicators`/`get_series`/`get_latest`/`get_vintage`/`get_metadata`/`get_source_trust`），新增维度自动出现在 `list_indicators` 与 `get_metadata`，无需改 MCP。

### 8.5 已知增强方向（未做，可选）

当前维度是「硬编码注册表」，新增需改 3 处文件。可收敛为**单一声明式 `dimensions.yaml`**（collect/clean/verify 均从它读取），新增维度只改一个文件。评估：当前 11 个维度改动频率低、收益有限；维度数显著增长（>30）时再做。

## 9. 飞轮④：候选发现（信号驱动的主动扩维）

**核心结论**：注册是手动的，但「发现哪些维度该注册」可以自动化。v1 采用**官方一手覆盖度扫描**（确定性差集，零网络/零 LLM/零 key），而不是新闻反查——因为官方一手"已能提供但漏跟踪"的缺口，比新闻热点更可靠、更该先补。

### 9.1 开源思路映射（2026-08-24 核验当前可用性）

| 环节 | 思路 | 状态 | 结论 |
|---|---|---|---|
| 热点探测 | GDELT 2.0 DOC API（免费、无 key、15 分钟刷新，volume/tone） | 可用 | **v3**：噪声大 + 滞后于官方发布，需关键词映射 + LLM |
| 反向查源(美) | FRED API `series/search` + `releases/dates`（80 万+ 序列） | 可用 | **v2**：需 FRED API key（项目现无） |
| 反向查源(中) | NBS 目录树实时 diff（nbsc 已暴露 root UUID + `external/new/` 树接口） | 可用但**易碎** | **v2**：NBS 2026-06 已换 endpoint、有 WAF JS 挑战、UUID 会失效 |
| 结构变更门 | Airbyte schema-change「Detect & approve myself」 | 可用 | 借鉴其**人工审批 gate** 模式，已落入 §9.3 状态机 |

### 9.2 v1 落地：官方覆盖度扫描

`discover_candidates.py`：枚举 nbsc `codes.json` 的 **25 个官方 NBS 系列** + 精选 `CANDIDATES` 目录（17 个逻辑指标，含中文标签/分类/core/单位/频率/nbsc 函数），与已跟踪集合（动态从 `NBS_INDICATORS` ∪ `METRICS` 推导）做差集，输出候选。

**核验发现**：官方一手已能提供但漏跟踪的核心缺口 = **GDP（7 系列）、M0/M1/M2（6 系列）、中国城镇调查失业率、综合 PMI、PPI 环比**——共 13 个核心 + 4 个次要候选。

### 9.3 提案库 schema 与状态机

独立库 `outputs/macro_discovery.sqlite`，表 `candidate_dimensions`：

| 字段 | 含义 |
|---|---|
| suggested_name / label_zh / category / core / unit / freq | 建议指标名、中文标签、分类、是否核心、单位、频率 |
| source / authority_level | `nbs` / `official_primary`（如实，绝不冒充一手） |
| nbsc_fn | 审批通过后要接入的 nbsc 访问函数 |
| confidence / status | 置信度（v1 恒 1.0，确定性）；`pending→approved→registered`（可 `rejected`） |

状态机：`pending`（自动产出）→ 人工 `approved` → 走 §8 的 3 文件注册 → `registered`。**红线**：本脚本只读 + 只写独立提案库，绝不调用 collect、绝不写 `macro_indicators`/`macro_clean`。
