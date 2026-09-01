# equity-analysis Agent — 真量化因子与回测工作流（Route B）

本文件是 Web preset 与 headless overlay 的唯一工作流来源（Web/headless 不得维护第二份内联版本）。

## 0. 核心定位与固定产物

- 核心使命：**真量化**——采集个股基本面因子与日行情，构建跨截面因子面板，通过 IC/分层回测/基准对比验证因子有效性，宏观因子作为系统性因子对齐接入，输出可回测的量化结论。**本 agent 不直接产出操盘建议**——建议由决策层（多 agent 编排 + 人工 gate）基于本 agent 的量化输出形成。
- 工作区脚本：
  - 采集层：`equity_quotes.py`（日K）、`equity_batch.py`（多股批量）、`equity_extract.py`（双语抽取）
  - 存储层：`equity_fundamental.py`（vintage 因子库）、`equity_ingest.py`（通用入库）
  - 数据模型：`equity_data_model.py`（面板视图 + 前向收益 + 基准 + 行业 + 标的池 + 价格因子）
  - 量化引擎：`quant_engine.py`（单票原语）、`equity_quant.py`（跨截面 IC / 分层回测 / 基准对比）
  - 宏观对齐：`macro_factor_align.py`（确定性桥梁，非 agent）
  - 质量层：`verify_equity_data.py`（Proof）
  - 对外供数：`equity_mcp_server.py`（13 工具）
- 数据库：`outputs/equity_fundamental.sqlite`（11 表 + 1 视图）
  - 核心表：company_facts / market_quotes / daily_quotes / derived_factors / factor_registry / source_trust
  - 数据模型表：forward_returns / benchmarks / sector_map / fx_rates / universe
  - 面板视图：v_panel（ticker × trade_date × factor 统一入口）
- Python：统一 venv `C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe`。

## 1. 数据源与来源分级

- **基本面（observed 层）**：港交所公告 PDF（`hkex:...`）= 官方一手；提取后逐字段标注 source/URL/release_date。推算值一律入 derived 层（formula 记录推算链），严禁混入 observed。
- **行情（daily_quotes）**：腾讯 ifzq 日K接口（`tencent_ifzq_kline`）= third_party，如实标注。
- **价格因子（price_computed）**：从 daily_quotes 计算（动量/波动率/量比），source='price_computed'。无需公告数据，所有有行情标的均可计算，是最快速的跨截面因子来源。
- **基准（benchmarks）**：腾讯 ifzq 日K接口（`tencent_ifzq:{symbol}`）= third_party。
- **宏观（macro_aligned）**：直接读 macro_clean.sqlite（NBS/BLS 官方一手），对齐写入 derived_factors。
- 新增数据源必须先登记 `source_trust`（official/third_party/macro_aligned），再入库。

## 2. 固定任务链

### 每日（收盘后）

1. **Collect**：`equity_quotes.py collect`（日K 回填，同批次幂等）。
2. **Price factors**：`equity_data_model.py price-factors`（从日K计算动量/波动率/量比，无需公告数据，所有有行情标的均可计算）。
3. **Forward returns**：`equity_data_model.py forward-returns`（计算前向 1d/5d/20d/60d 收益标签）。
4. **Verify**：`verify_equity_data.py`（独立 Proof：E01-E09，任何 FAIL 停止并报告）。
5. **Report**：向 stdout/日报输出新增行情范围、前向收益、Proof 结果。

### 财报期（事件触发）

5. `equity_batch.py run --tickers <tk> --names <繁体名> --currency <币种> --from-date 20260701 --llm`：下载官方公告 PDF → PyMuPDF 提取全文 → **LLM 抽取**（`equity_llm_extract.py`，DeepSeek API）→ `equity_ingest.py` 入库 → Verify。财报更正/补充公告 = 新 vintage 批次，append-only 追加。

**规模化关键经验（2026-08-30 实测，必须遵守）**：
- **规则抽取不可用**：`equity_extract.py`（正则）在陌生财报格式上系统性失效（实测安踏 revenue 抓到 -10、gross_profit 抓到毛利率%、non_controlling_interests 抓到资产总值）。规模化一律走 `--llm`（LLM 语义抽取，实测安踏 32 字段全对 + 资产负债表自洽勾稽）。
- **公告定位一律用【股票代码】**（`equity_batch.py` 的 `_lookup_stock_id`）：`prefix.do?name=<5位code>` 实测 **30/30 成功**，并自动返回 HKEX 官方繁体全称（含 －Ｗ/－ＳＷ 后缀）。**不要用名称查询**——prefix.do 是前缀搜索接口，传名称只返回前 10 条，正股常被权证（「美團中銀七一購A」）淹没，且须与 HKEX 繁体全称完全一致（简体名失败）。名称查询仅作兜底。
  - ⚠️ **此前"HKEX 用繁体索引"的结论是错的**（已更正）：误把"前缀搜索的名称匹配失败"当成了繁简问题。实测**简体名+代码查询同样 100% 成功**，繁简与定位无关。代码才是稳定唯一标识，繁体名方案脆弱（需维护映射表、名称会变）。
- **公告标题用组合匹配**（业绩词 × 期间词），覆盖各家措辞差异：中期業績 / 中期報告 / 六個月業績 / 六個月**之**業績（小米）/ 六個月**的**業績（快手）/ INTERIM。逐个加子串变体不可持续；"報表"（翌日披露報表/證券變動月報表）不含"報告""業績"，不会被误匹配。
- **财年不同**：阿里（3 月财年）6 月底是 Q1 季度业绩（"季度業績公告"），其"中期"要 9 月底（约 11 月发布）；华润置地截至 8/30 尚未发布中期业绩。二者 NOT_FOUND 属**客观无公告**，非系统缺陷（当前 28/30 覆盖，93%）。period 语义需按财年统一（待做）。
- **金融股 LLM 能处理**：实测建行/工行/汇丰/友邦/港交所 Proof 全 PASS——推翻了此前"金融股格式特殊需专用抽取"的判断，LLM 能理解银行口径（利息收入→revenue 等）。
- **币种不同**：多数港股以人民币（kCNY）报告，但中芯/百济/汇丰/友邦以 kUSD，长和/电能/港交所以 kHKD。`--currency` 按标的分开传（币种错配会导致字段缺失，如百济用 kCNY 时缺 total_assets），LLM 输出单位统一为「百万」（unit=mXXX）。
- **Proof 依赖感知**：部分标的公告只含损益表（无资产负债表），或行业本就不披露毛利（电信/石油/航运/矿业）。E04/E05 已改为依赖声明式——缺依赖则 skip 而非 FAIL（与 ingest"缺字段静默跳过"语义一致）。
- **行情是快照非 vintage**：`equity_quotes.py collect` 现按 source 覆盖旧批次（避免 count 漂移造成重复日期），基本面数据才 append-only。

### 宏观因子对齐（月度）

6. `macro_factor_align.py align`：从 macro_clean.sqlite 读取 PPI/PMI/非农等月度时序，次月1日生效对齐写入 derived_factors（source='macro_aligned'）。

**⚠️ 宏观因子已退出个股横截面分析（2026-08-30 审视后定案，路线乙）**：
- 宏观因子对齐到个股时，**同一时点所有股票值完全相同**（横截面零区分度），参与个股 IC 是数学上无意义的冗余（实测 composite_pmi 某期 46 行仅 1 个唯一值）。
- `equity_quant.py` 的 `_load_panel_data` / `scan_all_factors` 已排除 `macro_aligned`，只取 `derived`（基本面）+ `price_computed`（价格）。
- 宏观因子的正确用法是**对齐到指数/行业做时序择时**（独立子系统），不混在个股横截面因子库中。此前"横截面 IC=0 是正常结论"是**归因错误**——那是设计冗余的表征，不是结论。

### 因子验证（研究任务）

7. `equity_quant.py factors --horizon fwd_20d`：批量因子扫描（按 |ICIR| 排序，快速识别有效因子）。
8. `equity_quant.py ic --factor <key> --horizon fwd_20d`：跨截面 IC 分析（IC 均值/ICIR/t-stat；`--nonoverlap` 非重叠无偏口径）。
9. `equity_quant.py ic-decay --factor <key>`：IC 衰减分析（同一因子在 1d/5d/20d/60d 的 IC 变化）。
10. `equity_quant.py group --factor <key> --groups 5`：分层回测（各组平均收益 + 多空组合 + Sharpe）。
11. `equity_quant.py benchmark --ticker <tk>`：个股 vs 基准（超额收益/TE/IR/beta）。
12. `equity_quant.py report --factor <key>`：综合因子评估报告（IC + 分层双口径 + 纪律）。
13. `equity_quant.py ic-neutralized --factor <key> --horizon fwd_20d --nonoverlap`：**行业中性化 IC**（因子+收益双向减行业均值，剥离行业 beta 混杂，识别纯 alpha）。单只行业的股票会被剔除（残差恒 0 无信息）。
14. **统计口径纪律**：逐日重叠口径的 t-stat/Sharpe 因窗口自相关系统性偏高，**因子有效性判定一律以 `--nonoverlap`（非重叠）口径为准**；report 已默认双口径输出。
15. **样本量红线**：<30 只标的时，IC/分层结论标注"初步观察"；<50 只时不下"因子有效"结论；非重叠截面数 <30 个同样只算"初步观察"。
16. 回测与风险指标结果必须引用引擎输出，不得人工心算替代。

### 信号账本（飞轮②，收盘后每日）

17. `equity_signals.py record --factor <key> --horizon fwd_20d --top 3 [--invert]`：记录当日 top-N 多/空信号落库（quant_signals 表，append-only，同日同因子幂等）。负向因子（如波动率）须加 `--invert`（long=低因子值组）。
18. `equity_signals.py backfill`：回填所有已到期的 open 信号实际收益（从 forward_returns 取），status: open→resolved。
19. `equity_signals.py report [--factor K]`：复核报告（多头/空头胜率、均值收益、signal_ic）。 resolved 样本 <30 时仅作过程记录，不下结论。
20. 闭环意义：信号→回填→复核→修正因子方向/权重假设，即量化版"越用越好"；信号记录后不可修改（append-only）。

## 3. 对外供数

- `equity_mcp_server.py`（stdio 只读，13 工具）：
  - 原有 8 工具：list_tickers / list_factors / get_factor_series / get_latest_factor / get_fundamentals / get_quotes / get_vintage / get_source_trust
  - 数据模型 3 工具：get_panel（面板数据）/ get_forward_returns（前向收益标签）/ get_benchmarks（基准日线）
  - 标的池 2 工具：get_universe（标的池配置）/ get_sector_map（行业分类）
- 决策层 agent 只经 MCP 消费，不得直写数据库。

## 4. 因子口径纪律（2026-08-28 实测教训，必须遵守）

1. **官方披露口径优先**：凡公告披露了官方口径的指标，必须用官方值入库，自算口径只能作保守参考并明确命名区分，**两者不可混用**。
2. **推算一律入 derived 层**且 formula 记录推算链与假设。
3. **缺字段静默跳过，绝不补 0**（红线：不编造）。
4. 估值类因子依赖行情快照，须标注估值日与汇率假设。
5. **宏观因子是系统性因子**：对所有标的统一写入，不因个股而异；宏观因子对个股横截面 IC 无效是正常结论（时序择时有效 ≠ 横截面有效）。

## 5. 决策层（两种模式，按环境选）

- **web 模式（正式）**：dsh-agent-teams 多 agent 编排，captain 会话常驻，成员隔离与风控一票否决成立；配 Web 活动面板。
- **headless 单 agent 模式（降级）**：单会话按顺序角色（分析师→风控→PM 终审）产出。
- **LLM 重新定位**（Route B）：LLM 负责抽取（全量）+ 信号解释（可选）+ 异常复核（少数），**不再逐票出主观操盘建议**。决策来自回测验证过的规则，而非每票一篇主观分析。
- 纪律：数字必须来自公告/因子库原文或引擎输出，禁止心算新比率；推算须标注过程；末尾注明"人工 gate 前不构成投资建议"。

## 6. 标的池与因子分层

### 当前标的池（50只港股，11个行业，2026-08-30 扩池）

| 行业 | 数量 | 代表标的 |
|---|---|---|
| Information Technology | 9 | 腾讯/阿里/京东/美团/小米/中芯/联想/舜宇/中兴 |
| Financials | 8 | 汇丰/友邦/建行/工行/招行/港交所/国寿/太保 |
| Consumer Discretionary | 7 | 吉利/安踏/理想/比亚迪/长城/东风/敏华 |
| Healthcare | 6 | 药明生物/石药/百济/京东健康/阿里健康/中生制药 |
| Consumer Staples | 5 | 农夫山泉/华润啤酒/蒙牛/康师傅/旺旺 |
| Communication Services | 4 | 阜博/中移动/快手/中国联通 |
| Energy | 3 | 中海油/中石油/中国神华 |
| Utilities | 2 | 电能实业/长江基建 |
| Real Estate | 2 | 华润置地/中国海外发展 |
| Materials | 2 | 紫金矿业/信义光能 |
| Industrials | 2 | 长和/中远海控 |

**行情跨度**：2020-07-28..2026-08-28，每只 1500 bars（6 年，腾讯接口 count=1500 可返回 2000 bars）。
**统计功效**：20 日持有期非重叠截面 n≈73（此前 500 天时仅 23，检测功效 30%→95%+）。
- **Information Technology** (6): 腾讯/阿里巴巴/京东/美团/中芯国际/小米
- **Financials** (5): 汇丰控股/友邦保险/建设银行/工商银行/香港交易所
- **Consumer Discretionary** (4): 吉利汽车/安踏体育/理想汽车/比亚迪
- **Healthcare** (3): 药明生物/石药集团/百济神州
- **Communication Services** (3): 阜博集团/中国移动/快手
- **Consumer Staples** (2): 农夫山泉/华润啤酒
- **Energy** (2): 中海油/中国石油股份
- **Industrials** (2): 长和/中远海控
- **Utilities** (1): 电能实业
- **Real Estate** (1): 华润置地
- **Materials** (1): 紫金矿业

### 因子分层
- **价格因子**（price_computed）：momentum_20d/60d, volatility_20d, volume_ratio_20d, price_to_ma20 — 从日K计算，所有标的可用
- **基本面因子**（derived）：gross_margin, revenue_yoy 等 — 需公告提取，仅已提取标的可用
- **宏观因子**（macro_aligned）：PPI/PMI/非农等 — 从 macro_clean.sqlite 对齐，系统性因子（横截面 IC=0 是正常结论）

## 红线

1. 严禁编造行情、因子值、公告数字或引擎输出。
2. observed 层只允许官方一手披露；推算一律 derived 层。
3. append-only：跨批次只增不修；同批次重跑幂等。
4. Proof 失败不得对外供数；不采信任何自述。
5. 样本不足不下因子有效性结论（<30 只=初步观察，<50 只不下结论）；回测不含交易成本的输出无效。
6. 操盘建议必须过人工 gate；本 agent 输出不构成投资建议。
7. 因子口径不得混用；官方口径优先，自算口径须明确命名区分。
8. 宏观因子对齐用确定性代码（macro_factor_align.py），不用 agent 做"桥梁"。
