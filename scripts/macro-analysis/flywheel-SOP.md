# 宏观数据基石 — 越用越好的三飞轮 SOP

> 本文件是「宏观分析 agent 如何作为数据基石越用越好」的权威方案与落地底稿，随每次落地追加演进记录。
> 核心定位：**数据层只增不修、只读对外；所有"学习"产物写入独立库，绝不回写数据层。** 预测/学习与数据基石严格解耦。

## 1. 开源借鉴（四类，均已调研核验）

| 飞轮 | 借鉴来源 | 核心思想 | 我们怎么用 |
|---|---|---|---|
| ① 数据厚度 | ALFRED（圣路易斯联储 Archival FRED） | vintage = "当时已知信息"的时点快照；引用数据必须带采集日期；旧版本存档、不覆盖 | 已有 append-only vintage + `collected_at`；补 `revision_stats` 量化"初值→终值"偏差，反哺可信度评分 |
| ② 校验智慧 | Great Expectations / dbt tests / Soda | expectation suite 声明"好数据长什么样"；失败留档(run history)；分层 severity；quarantine 隔离坏行 | 把固定 Proof 升级为"失败案例库 + 模式标签 + 历史命中"，即 GE 的迷你开源版（复用 sqlite，不引重依赖） |
| ③ 案例沉淀 | Reflexion + Voyager（agent 经验闭环） | 反思写入 episodic memory 下次复用；成功经验固化为可检索技能库；改进是 in-context 而非改权重 | 分析 agent 每次判断→记假设→结果落地后反思→存 case 库→反哺 dsh case reference |
| ③ 打分标准 | Metaculus / Brier score / superforecasting | proper scoring rule（Brier/CRPS）+ 校准(calibration)+分辨力(resolution)；只报命中率会骗人 | 预测 case 用 Brier/CRPS + 校准报告，禁止只报命中率 |

## 2. 三飞轮架构

```
① 数据厚度飞轮（已在跑：Collect→Verify→Store→Clean→MCP 只读）
        ↓ 反哺
② 校验智慧飞轮（轮 A：verify 失败案例库 + 模式命中）
        ↓ 提升基石可信度后
③ 预测/案例沉淀飞轮（轮 C：分析 agent + case 库 + Brier 打分，独立于数据层）
```

## 3. 数据模型（新增三张独立库表，均不回写数据层）

- **轮 A**：`outputs/macro_verify.sqlite` → `verify_failures`（`checked_at`/`date`/`fail_tag`/`message`，append-only）
- **轮 B**：`outputs/macro_clean.sqlite` 增 `revision_stats`（`indicator`/`country`/`first_value`/`last_value`/`mean_abs_rev`/`n_rev`）
- **轮 C**：`outputs/analysis_cases.sqlite` → `cases`（假设/依据/预测/真实值/结论）+ `forecast_scores`（Brier/CRPS/calibration）

## 4. 落地清单

- [x] 轮 A：verify 失败案例库 + 模式命中（`verify_macro_data.py`）
- [ ] 轮 B：`revision_stats` 可信度评分（`clean_macro_data.py`）
- [ ] 轮 C：分析 agent preset + case 库 + Brier/CRPS 打分

## 5. 红线

1. 数据层只增不修；所有学习产物只写独立库，绝不回写 `macro_indicators` / `macro_clean`。
2. Verify 失败不得进入 Clean（沿用）。
3. 预测 case 必须记录假设 + 依据 + 真实值，否则不计入打分。
4. 命中率必须配 Brier/校准报告，禁止只报命中率。

## 6. 演进记录

- 2026-08-24 定稿三飞轮 + 四类开源借鉴映射；落地轮 A（校验智慧飞轮）。
