#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A股策略注册表 —— 把已验证的策略沉淀成可被 Agent 查询的结构化资产。

## 为什么必须有这张表（这是蓝图缺失的那一环）

用户蓝图是「输入任意股票代码 → 系统智能匹配最优量化策略 → 执行分析 → 给出决策」。
前三步都需要一个**机器可读的策略清单**，而现有 `factor_registry`（在
`equity_fundamental.sqlite`，51 个港股基本面因子）装不下它 —— 那张表只有
factor_key/formula/layer，没有：

  - **适用池**：农业窄池验出来的东西不能拿去跑全市场（用户明确要求"不要妄想
    一个策略能完成所有股票池"）
  - **数据前置**：策略需要哪些字段。没有这个，`build_signal` 的 `np.nanmean`
    会在字段缺失时**静默退化成子集策略**且不报错（实测宽池就会把
    comp_turn_mom 悄悄变成单因子 20 日反转）
  - **三关结果**：功效 / 显著性 / 经济可行性 各自的量化值
  - **归因结论**：超额是残差 alpha 还是风格暴露 —— 这决定它能不能叫"选股能力"
  - **适用边界与失效条件**：成本上限、容量、regime 依赖

## 四张表

    pool_registry          股票池 + 该池的**数据能力**（有无换手率/成交额）
    strategy_registry      策略 + 三关结果 + 归因 + 交付定级 + 边界
    strategy_evidence      每条结论指向的产物文件（可追溯，不允许口头结论）
    ashare_factor_registry A股价量单因子 + 跨池(宽池/农业)验证结果（治理项②）

## 交付定级（verdict）四级分类

    deliverable_alpha        过三关 且 正交化后仍为正 → 真选股能力
    deliverable_beta         过三关 但 归因=风格暴露  → 可交付的风格轮动/beta 工具
                                                       （必须显式标注，不得当 alpha 卖）
    true_but_undeliverable   显著但归因/子样本不稳，或成本吃光
    insufficient_power       |效应| < MDE，样本量不足以判定（≠ 无效）
    informative_null         功效充足且效应确实接近零 → 有信息的零结果

⚠️ `insufficient_power` 与 `informative_null` 绝不可混淆：前者是"测不出"，
   后者是"确实没有"。混淆会导致把好因子误杀或把坏因子误留。

## 用法
    python ashare_strategy_registry.py --init              # 建表
    python ashare_strategy_registry.py --load-agri         # 写入农业池已验证策略
    python ashare_strategy_registry.py --register-factors  # 登记 5 价量因子跨池结果
    python ashare_strategy_registry.py --list              # 查看
    python ashare_strategy_registry.py --match 000998      # 按代码匹配可用策略
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]
OUT_DIR = ROOT / "outputs"
# ★ 独立库：遵循项目纪律「数据层只增不修、学习产物只写独立库」
REG_DB = OUT_DIR / "ashare_strategy_registry.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pool_registry (
    pool_key      TEXT PRIMARY KEY,
    label         TEXT NOT NULL,
    universe_rule TEXT NOT NULL,
    n_codes       INTEGER,
    price_db      TEXT,
    fund_db       TEXT,
    date_from     TEXT,
    date_to       TEXT,
    -- ★ 数据能力标记：策略匹配的前置闸门
    has_close     INTEGER DEFAULT 1,
    has_volume    INTEGER DEFAULT 1,
    has_amount    INTEGER DEFAULT 0,   -- 真实成交额（非 volume×price 回退）
    has_turnover  INTEGER DEFAULT 0,   -- 换手率
    has_fundamental INTEGER DEFAULT 0,
    codes         TEXT,                -- JSON 数组
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS strategy_registry (
    strategy_key    TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    pool_key        TEXT NOT NULL,
    components      TEXT NOT NULL,   -- JSON [[factor_key, sign], ...]
    required_fields TEXT NOT NULL,   -- JSON ["close","volume","turnover_pct"]
    params          TEXT NOT NULL,   -- JSON {holding, buy_thr, sell_thr, min_cross, ...}

    -- 第①关 统计功效
    gate1_n_obs     INTEGER,
    gate1_sigma_true REAL,
    gate1_mde       REAL,
    gate1_effect    REAL,
    gate1_ratio     REAL,            -- |effect| / MDE
    gate1_pass      INTEGER,

    -- 第②关 显著性
    gate2_ic        REAL,
    gate2_t_hac     REAL,
    gate2_mcc_thr   REAL,
    gate2_n_tests   INTEGER,
    gate2_pass      INTEGER,

    -- 第③关 经济可行性（扣成本）
    gate3_gross     REAL,
    gate3_net       REAL,
    gate3_ir        REAL,
    gate3_ir_min    REAL,            -- 全相位最小值（防网格相位假象）
    gate3_turnover  REAL,
    gate3_pos_years TEXT,            -- "11/13"
    gate3_monotonic INTEGER,         -- 五分位严格单调
    gate3_pass      INTEGER,

    -- 稳健性
    phase_robust    TEXT,            -- JSON {mean,min,max,std,pass_frac,n_phase}
    neighborhood    TEXT,            -- JSON {n_cells,all_positive,ir_min,ir_mean}
    cost_ceiling    REAL,            -- 往返成本上限（IR 跌到 0.50 的临界值）

    -- 归因：决定能不能叫 alpha
    attribution     TEXT,            -- style_exposure | residual_alpha | mixed
    attribution_note TEXT,

    verdict         TEXT NOT NULL,
    boundary        TEXT NOT NULL,   -- 适用边界与失效条件（人读）
    version         TEXT NOT NULL,
    validated_at    TEXT NOT NULL,
    FOREIGN KEY (pool_key) REFERENCES pool_registry(pool_key)
);

CREATE TABLE IF NOT EXISTS strategy_evidence (
    strategy_key TEXT NOT NULL,
    gate         TEXT NOT NULL,      -- gate1 | gate2 | gate3 | attribution | phase | neighborhood
    artifact     TEXT NOT NULL,      -- 产物文件相对路径
    note         TEXT,
    PRIMARY KEY (strategy_key, gate, artifact)
);

-- ★ A股价量因子登记表（治理项②）：把"宽池无 alpha"沉淀成可追溯证据链。
--   与 strategy_registry 不同，这里登记的是单因子（非策略），记录跨池验证结果。
CREATE TABLE IF NOT EXISTS ashare_factor_registry (
    factor_key     TEXT PRIMARY KEY,
    label          TEXT NOT NULL,
    formula        TEXT NOT NULL,
    expected_sign  INTEGER NOT NULL,   -- -1=取低分位(低值), +1=取高分位(高值)
    layer          TEXT NOT NULL DEFAULT 'price_volume',
    version        TEXT NOT NULL,
    -- 跨池验证结果（主口径 成本0.50% / H=5 / 缓冲30%；宽池为 IR均值跨持有期）
    wide_pool_ir   REAL,              -- 宽基池(996只,2018+同窗) IR均值
    wide_pool_net  REAL,              -- 净超额均值
    wide_pass_rate REAL,             -- 通过率(0~1)
    agri_pool_ir   REAL,              -- 农业窄池(2018+同窗) H5 IR
    agri_pool_net  REAL,
    verdict        TEXT NOT NULL,    -- non_robust_across_pools | ...
    evidence       TEXT,             -- 产物文件(分号分隔)
    notes          TEXT,
    validated_at   TEXT NOT NULL
);
"""


def conn_reg():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(REG_DB, timeout=30)
    c.execute("PRAGMA foreign_keys = ON")
    return c


def cmd_init():
    c = conn_reg()
    c.executescript(SCHEMA)
    c.commit()
    n = len(list(c.execute("SELECT name FROM sqlite_master WHERE type='table'")))
    print(f"建表完成: {REG_DB}  （{n} 张表）")
    c.close()


def load_agri():
    """写入农业窄池的验证结论。

    ⚠️ 所有数字都从今日产出的 JSON 里读，不硬编码 —— 硬编码会与产物脱钩，
       将来重跑就对不上了。
    """
    day = OUT_DIR / datetime.now().strftime("%Y-%m-%d")
    chosen = json.loads((day / "ashare_agri_final_chosen.json").read_text(encoding="utf-8"))
    full = json.loads((day / "ashare_agri_final_full.json").read_text(encoding="utf-8"))
    bt = json.loads((day / "ashare_agri_backtest.json").read_text(encoding="utf-8"))
    val = json.loads((day / "ashare_agri_validate.json").read_text(encoding="utf-8"))

    c = conn_reg()
    c.executescript(SCHEMA)

    # ---- 池 ----
    agri_db = OUT_DIR / "ashare_agri_hfq_xq.sqlite"
    codes = []
    if agri_db.exists():
        ac = sqlite3.connect(agri_db)
        codes = [r[0] for r in ac.execute(
            "SELECT DISTINCT code FROM daily_quotes_hfq ORDER BY code")]
        ac.close()
    c.execute("""INSERT OR REPLACE INTO pool_registry
        (pool_key,label,universe_rule,n_codes,price_db,fund_db,date_from,date_to,
         has_close,has_volume,has_amount,has_turnover,has_fundamental,codes,notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        "ashare_agri", "A股农业窄池",
        "申万农林牧渔 + 生猪养殖主线，人工核定后 QC 过滤；全程存续",
        len(codes), "outputs/ashare_agri_hfq_xq.sqlite", "outputs/ashare_fundamental.sqlite",
        "2014-01-02", "2026-09-01",
        1, 1, 1, 1, 1, json.dumps(codes, ensure_ascii=False),
        "换手率/成交额来自东财 daily_liquidity（主库）；后复权价来自雪球 kline（2026-09-06 起，腾讯源已废弃）"))

    # ---- 策略：合成·低换手+反转（候选，已过三关但归因为风格暴露）----
    mid = [x for x in full["cells"]
           if x["exclude_limit"] and x["h"] == 5
           and abs(x["sell_thr"] - 0.50) < 1e-9 and x["min_cross"] == 30
           and abs(x["cost"] - 0.005) < 1e-9]
    cell = mid[0] if mid else None
    allc = full["cells"]
    nb = {"n_cells": len(allc),
          "all_positive": bool(min(x["ir_mean"] for x in allc) > 0),
          "ir_min": round(min(x["ir_mean"] for x in allc), 4),
          "ir_mean": round(sum(x["ir_mean"] for x in allc) / len(allc), 4),
          "net_min": round(min(x["net_mean"] for x in allc), 4)}

    # 五分位单调性（来自 backtest H=5 主成本档）
    MID = "中 0.50%（含常规滑点）"
    q = bt["strategies"]["comp_turn_mom"]["by_holding"]["5"][MID]["quantile_ann_return"]
    mono = all(q[i] < q[i + 1] for i in range(4))

    # 第①②关：从价量验证里取 comp 分量的代表值（低换手 H=5）
    g1 = g2 = None
    for fk, fv in val.get("factors", {}).items():
        if fk == "turnover_level_20d":
            g1 = fv["by_holding"].get("5")
    if g1:
        g2 = g1

    # ★ 键名易错点：ashare_agri_final_chosen.json 的顶层键是 `chosen_detail`，
    #   而交付数字（毛/净超额、年换手、分年度）都在其 by_cost["0.005"] 一层里，
    #   不在 chosen_detail 顶层。首版误写成 chosen.get("chosen") → 取到空字典，
    #   导致 gate3_turnover / gate3_gross 静默变 NULL（--list 显示 0.0x）。
    chosen_detail = (chosen.get("chosen_detail")
                     or chosen.get("chosen") or chosen.get("detail") or {})
    ir_by_cost = dict(chosen_detail.get("by_cost") or {})
    MAIN_COST = "0.005"                      # 主成本档 0.50%（含常规滑点）
    cost_cell = ir_by_cost.get(MAIN_COST) or {}
    if not cost_cell:
        raise SystemExit(
            f"未在 {P_FINAL_CHOSEN.name} 的 chosen_detail.by_cost 找到成本档 "
            f"{MAIN_COST}，实际可用: {sorted(ir_by_cost)}。拒绝写入不完整记录。")
    yearly = cost_cell.get("yearly_excess_phase_avg") or {}
    pos_years_label = (f"{cost_cell.get('pos_years')}/{cost_cell.get('n_years')}"
                       if cost_cell.get("n_years") else None)

    c.execute("""INSERT OR REPLACE INTO strategy_registry
        (strategy_key,label,pool_key,components,required_fields,params,
         gate1_n_obs,gate1_sigma_true,gate1_mde,gate1_effect,gate1_ratio,gate1_pass,
         gate2_ic,gate2_t_hac,gate2_mcc_thr,gate2_n_tests,gate2_pass,
         gate3_gross,gate3_net,gate3_ir,gate3_ir_min,gate3_turnover,
         gate3_pos_years,gate3_monotonic,gate3_pass,
         phase_robust,neighborhood,cost_ceiling,
         attribution,attribution_note,verdict,boundary,version,validated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        "agri_comp_turn_mom_h5b30",
        "农业池·低换手+20日反转（周频评估 + 30%缓冲）",
        "ashare_agri",
        json.dumps([["turnover_level_20d", -1], ["momentum_20d", -1]]),
        # ★ 这是防 nanmean 静默退化的关键字段
        json.dumps(["close", "volume", "turnover_pct"]),
        json.dumps({"holding": 5, "buy_thr": 0.20, "sell_thr": 0.50,
                    "min_cross": 30, "n_quantiles": 5, "exclude_limit": True,
                    "rebalance": "non-overlapping every 5 trading days",
                    "position": "long-only Q5 equal-weight vs pool equal-weight benchmark"}),
        (g1 or {}).get("n_periods"), (g1 or {}).get("sigma_true"), (g1 or {}).get("mde"),
        (g1 or {}).get("ic_neutral"), (g1 or {}).get("ratio_ic_over_mde"),
        1 if (g1 or {}).get("ratio_ic_over_mde", 0) and g1["ratio_ic_over_mde"] >= 1 else 0,
        (g2 or {}).get("ic_neutral"), (g2 or {}).get("t_neutral_nw"),
        val.get("mcc_threshold_t"), val.get("n_tests"),
        1 if (g2 or {}).get("t_neutral_nw") and abs(g2["t_neutral_nw"]) >= val.get("mcc_threshold_t", 99) else 0,
        cost_cell.get("gross_excess_ann"),
        cell["net_mean"] if cell else None,
        cell["ir_mean"] if cell else None,
        cell["ir_min"] if cell else None,
        cost_cell.get("annual_turnover_x"),
        pos_years_label,
        1 if mono else 0,
        1 if (cell and cell["ir_mean"] >= 0.50 and cell["pass_frac"] >= 0.7 and mono) else 0,
        json.dumps({"ir_mean": cell["ir_mean"], "ir_min": cell["ir_min"],
                    "ir_max": cell["ir_max"], "pass_frac": cell["pass_frac"],
                    "n_phase": cell["n_phase"]} if cell else {}, ensure_ascii=False),
        json.dumps(nb, ensure_ascii=False),
        0.00586,   # IR = 0.828 − 56×cost 的解（三点精确线性拟合）
        "style_exposure",
        "ashare_agri_attrib.json：对 9 维价量风格正交化后，7/7 策略超额转负、"
        "五分位单调性消失；信号对风格基的 R² = 0.53~0.85。drop-1 检验显示收益"
        "弥散在整个风格空间（剔任一风格保留 9%~98%）→ 本策略是「冷门+超跌+低波」"
        "多维风格组合的低成本实现，不是残差选股能力。",
        "regime_dependent_negative_post2018",
        "【适用·全窗口(2014+)】仅 A股农业窄池（19~30 只可交易），周频评估、月均换手 5.5x；"
        "往返成本必须 ≤ 0.586%（含滑点），否则 IR 跌破 0.50。"
        "【⚠️ 回归/失效·2018+ 同窗检验（2026-09-02 新增）】本策略在 2018+ 窗口"
        "（同候选配置 H=5/缓冲30%/成本0.50%）双双转负："
        "农业池 IR=−0.18（净超额 −1.68%、4/9 年为正、五分位非单调）；"
        "宽基池(996只,派生换手率) IR=−0.664（净超额 −4.99%）。"
        "即全窗口 IR 0.548 系 2014-2017 段驱动，2018 起为亏损 regime ——"
        "属「疑似夸大/regime 衰减」，不得作为可交付 alpha 使用，样本外须重验。"
        "【其他约束】① 成本升到 0.80% → IR 0.380 不可用；"
        "② 池子扩大需重验（已完成：宽池 2018+ 同窗 IR=−0.664）；"
        "③ 不得宣称为 alpha —— 归因证明是风格暴露，应作风格轮动/beta 工具。",
        "v2", datetime.now().isoformat(timespec="seconds")))

    ev = [
        ("gate1", "outputs/2026-09-02/ashare_agri_validate.json", "价量因子功效与 MDE"),
        ("gate2", "outputs/2026-09-02/ashare_agri_validate.json", "中性化 IC + Newey-West t + MCC"),
        ("gate3", "outputs/2026-09-02/ashare_agri_backtest.json", "扣成本回测 + 五分位"),
        ("gate3", "outputs/2026-09-02/ashare_agri_final_chosen.json", "交付配置明细（分年度/换手/成本档）"),
        ("phase", "outputs/2026-09-02/ashare_agri_phase.json", "H=20 全相位枚举（揭穿 IR 0.588 是相位最大值）"),
        ("phase", "outputs/2026-09-02/ashare_agri_phase_ext.json", "H=5/10/40/60 全相位（未剔涨停口径）"),
        ("neighborhood", "outputs/2026-09-02/ashare_agri_final_full.json", "162 邻域组合全正 → 高原非尖峰"),
        ("attribution", "outputs/2026-09-02/ashare_agri_attrib.json", "9 维风格正交化 + drop-1"),
        ("regime2018", "outputs/2026-09-02/ashare_agri_backtest_w2018.log",
         "⚠️ 同窗(2018+)检验：农业池 comp_turn_mom IR=−0.18（原 0.548 系 2014-17 驱动）"),
        ("regime2018", "outputs/2026-09-02/wide_extrap_w2018b.log",
         "⚠️ 宽池(996只,派生换手率)同窗(2018+) comp_turn_mom IR=−0.664 → 跨池亦为负，非结构效应"),
    ]
    for gate, art, note in ev:
        c.execute("INSERT OR REPLACE INTO strategy_evidence VALUES (?,?,?,?)",
                  ("agri_comp_turn_mom_h5b30", gate, art, note))

    c.commit()
    print(f"已写入 1 个池 + 1 个策略 + {len(ev)} 条证据 → {REG_DB}")
    c.close()


def cmd_register_factors():
    """把 A股 5 个纯价量因子在宽池(2018+)与农业池(2018+)的回测结果登记进
    ashare_factor_registry，使「宽池无 alpha」有可追溯证据链。
    数字来源：logs/wide_extrap_w2018b.log（宽池，IR均值跨持有期）
             + logs/agri_backtest_w2018.log（农业池 H5/成本0.50%）。
    两池在 2018+ 同窗全部转负、通过率 0% → 定级 non_robust_across_pools。"""
    c = conn_reg()
    c.executescript(SCHEMA)  # 确保表存在
    EVID = ("outputs/2026-09-02/logs/wide_extrap_w2018b.log;"
            "outputs/2026-09-02/logs/agri_backtest_w2018.log")
    NOTE = "宽池(996只)+农业池(19~30只) 2018+ 同窗双负，通过率0%"
    rows = [
        # factor_key, label, formula, sign, layer, ver,
        # wide_ir, wide_net, wide_pass, agri_ir, agri_net, verdict, evidence, notes
        ("reversal_5d", "5日反转", "-sdiv(close,5)", -1, "price_volume", "v1",
         -1.447, -11.76, 0.0, -1.35, -15.21, "non_robust_across_pools", EVID, NOTE),
        ("momentum_20d", "20日反转(低动量)", "sdiv(close,20)", -1, "price_volume", "v1",
         -0.335, -2.73, 0.0, -0.30, -3.42, "non_robust_across_pools", EVID, NOTE),
        ("turnover_level_20d", "低换手(对数)", "log(20日平均换手率%)", -1, "price_volume", "v1",
         -0.665, -5.45, 0.0, -0.05, -0.44, "non_robust_across_pools", EVID, NOTE),
        ("volatility_20d", "低波动", "sqrt(20日收益方差)", -1, "price_volume", "v1",
         -0.612, -5.67, 0.0, -0.86, -8.11, "non_robust_across_pools", EVID, NOTE),
        ("price_to_ma20", "价格/MA20(低偏离)", "close/MA20-1", -1, "price_volume", "v1",
         -0.543, -4.44, 0.0, -0.84, -9.21, "non_robust_across_pools", EVID, NOTE),
    ]
    c.executemany(
        """INSERT OR REPLACE INTO ashare_factor_registry
           (factor_key,label,formula,expected_sign,layer,version,
            wide_pool_ir,wide_pool_net,wide_pass_rate,agri_pool_ir,agri_pool_net,
            verdict,evidence,notes,validated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [r + (datetime.now().isoformat(timespec="seconds"),) for r in rows])
    c.commit()
    print(f"已登记 {len(rows)} 个 A股价量因子 → {REG_DB} (表 ashare_factor_registry)")
    c.close()


def cmd_register_semi():
    """治理项①收尾：半导体池 gate1/2 复验结果登记（2026-09-02）。

    - pool_registry 增加 ashare_semi（79 只，has_amount=0：daily_liquidity.amount
      全 NULL（派生换手率路线），这是 validate 必须用 --ctrl volume 的根因）。
    - ashare_factor_registry 的 5 个纯价量因子 notes/evidence 追加半导体全窗口
      gate1/2 复验摘要（不覆盖宽池/农业池数字，verdict 保持 non_robust_across_pools：
      全窗口 IC 显著 + 2018+ 回测转负 = 与农业池同款「显著但不可交付」形态）。
    """
    day = OUT_DIR / "2026-09-02"
    val = json.loads((day / "ashare_semi_validate.json").read_text(encoding="utf-8"))

    # 从验证 JSON 统计各因子全窗口判定（防硬编码：数字来自产物）
    per_factor = {}
    for fk, fv in val["factors"].items():
        star = mcc_only = fail = 0
        for h, r in fv["by_holding"].items():
            v = r.get("verdict", "")
            if v.startswith("★"):
                star += 1
            elif v.startswith("MCC"):
                mcc_only += 1
            else:
                fail += 1
        if star + mcc_only + fail:
            per_factor[fk] = (star, mcc_only, fail)

    semi_db = OUT_DIR / "ashare_semi_hfq_xq.sqlite"
    codes = []
    d_lo = d_hi = None
    if semi_db.exists():
        sc = sqlite3.connect(semi_db)
        codes = [r[0] for r in sc.execute(
            "SELECT DISTINCT code FROM daily_quotes_hfq ORDER BY code")]
        d_lo, d_hi = sc.execute(
            "SELECT MIN(trade_date), MAX(trade_date) FROM daily_quotes_hfq").fetchone()
        sc.close()

    c = conn_reg()
    c.executescript(SCHEMA)
    c.execute("""INSERT OR REPLACE INTO pool_registry
        (pool_key,label,universe_rule,n_codes,price_db,fund_db,date_from,date_to,
         has_close,has_volume,has_amount,has_turnover,has_fundamental,codes,notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        "ashare_semi", "A股半导体池",
        "半导体产业链（设计/制造/封测/设备/材料），人工核定后 QC 过滤",
        len(codes), "outputs/ashare_semi_hfq_xq.sqlite", None,
        d_lo, d_hi,
        1, 1, 0, 1, 0, json.dumps(codes, ensure_ascii=False),
        "⚠️ has_amount=0：daily_liquidity.amount 全 NULL（换手率由 volume/free_shares 派生，"
        "无原生成交额）→ validate/回测中性化控制变量必须用 log(volume)（--ctrl volume）；"
        "后复权价来自腾讯 fqkline hfq"))

    EVID_SEMI = "outputs/2026-09-02/ashare_semi_validate.json"
    n_upd = 0
    for fk, (star, mcc_only, fail) in sorted(per_factor.items()):
        row = c.execute(
            "SELECT notes, evidence FROM ashare_factor_registry WHERE factor_key=?", (fk,)).fetchone()
        if not row:
            continue
        old_notes, old_evid = row
        add = (f"；半导体池(79只)全窗口 gate1/2 复验：★通过 {star}/9 格"
               f"（另有 MCC-only {mcc_only}、未通过 {fail}），中性IC H5 方向与农业池一致，"
               "但 2018+ 同窗回测转负（comp_turn_mom IR −0.31）→ 与农业池同款"
               "「全窗口显著但不可交付」，verdict 不变")
        new_notes = (old_notes or "") + add
        new_evid = (old_evid or "") + (";" if old_evid else "") + EVID_SEMI
        c.execute("""UPDATE ashare_factor_registry SET notes=?, evidence=?, validated_at=?
                     WHERE factor_key=?""",
                  (new_notes, new_evid, datetime.now().isoformat(timespec="seconds"), fk))
        n_upd += 1
    c.commit()
    print(f"已登记半导体池（{len(codes)} 只）+ 更新 {n_upd} 个因子的 gate1/2 复验摘要 → {REG_DB}")
    c.close()


def cmd_register_neglist():
    """把「基本面负面清单」登记为风控型策略（verdict=risk_signal_not_alpha）。

    依据（2026-09-02 基本面线终局结论）：
    - gate1/2：净利同比(缩尾) IC=−0.1110、MDE=0.0877、|IC|/MDE=1.26、t(HAC)=−3.33
      （MCC 阈值 2.638，6 因子 1 持有期）；对 9 价量风格+规模正交化后保留 59% IC、t=−3.16
      → 本项目唯一「正交化后站得住」的独立信息。
    - gate3：纯多头买最差组净超额 +7.06%/年、IR 0.51，但正年 7/13、非单调 → FAIL；
      剔最优 2 年塌到 +0.41%（2018+2021 贡献 95%）+ 幸存者偏差翻转阈值 D=13 只
      → **不可作为 alpha 交付**。
    - 可交付形态 = 负面清单（回避净利同比最高分位组），风控 overlay，不需过第③关。
    """
    day = OUT_DIR / "2026-09-02"
    bt = json.loads((day / "ashare_fund_backtest_h60.json").read_text(encoding="utf-8"))
    r = bt["results"]["np_yoy_wins(买最差)"]["主 0.50%"]
    # 方向自校验（SOP §六.3 教训）：信号=−Z(np_yoy)，收益应随信号分位递增
    q = r["quintile_returns"]
    assert q[0] < q[-1], f"方向校验失败: quintile_returns={q}"

    c = conn_reg()
    c.executescript(SCHEMA)
    c.execute("""INSERT OR REPLACE INTO strategy_registry
        (strategy_key,label,pool_key,components,required_fields,params,
         gate1_n_obs,gate1_sigma_true,gate1_mde,gate1_effect,gate1_ratio,gate1_pass,
         gate2_ic,gate2_t_hac,gate2_mcc_thr,gate2_n_tests,gate2_pass,
         gate3_gross,gate3_net,gate3_ir,gate3_ir_min,gate3_turnover,gate3_pos_years,
         gate3_monotonic,gate3_pass,
         attribution,attribution_note,verdict,boundary,version,validated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        "agri_np_yoy_neglist",
        "农业池·基本面负面清单（回避净利同比最高分位组）",
        "ashare_agri",
        '[["np_yoy_wins", -1]]',
        '["fundamental"]',
        json.dumps({
            "holding": 60,
            "rebalance": "按实际财报生效日（非机械 60 日）",
            "n_quantiles": 5,
            "usage": "risk_overlay：持仓/选股回避 raw np_yoy 最高 20%（= 信号分位 Q1），不做空",
            "not_a_strategy": "不生成多头组合，不相对基准计超额",
        }, ensure_ascii=False),
        # gate1（功效）：25 个非重叠观测，N_eff 按披露日历去重（年报/一季报仅隔 2~4 交易日）
        25, 0.1566, 0.0877, -0.1110, 1.26, 1,
        # gate2（显著性）：MCC 6 检验 → 阈值 2.638；t(HAC)=−3.33 通过
        -0.1110, -3.33, 2.638, 6, 1,
        # gate3（经济可行）：买最差组主成本档；正年不足 + 非单调 → FAIL
        r["gross_excess_ann"], r["net_excess_ann"], r["ir"], None, r["annual_turnover_x"],
        f"{r['pos_years']}/{r['n_years']}", 0, 0,
        "residual_alpha",
        "对 9 价量风格+自由流通市值正交化后保留 59% IC、t=−3.16（R²仅 22%）→ 独立信息，"
        "非风格马甲；但与规模相关仅 +0.079。这是本项目第一个正交化后站得住的信号"
        "（价量因子 7/7 正交化后转负）。",
        "risk_signal_not_alpha",
        "【定位】风控 overlay，非 alpha：不可宣称选股能力，不需过第③关。\n"
        "【用法】回避 raw 净利同比最高分位组（高成长农业股）；反向（买最差组）兑现受双重约束：\n"
        "① 农业融资券源近乎为零，Q1−Q5 价差 +17.8%/年不可做空兑现；\n"
        "② 买最差组净超额 +7.06%/年中 95% 集中在 2018(非洲猪瘟+去杠杆)/2021(猪周期见顶) 两年，"
        "剔最优 2 年后仅 +0.41%；幸存者偏差翻转阈值 D=13 只（当前成分股口径，策略恰买最易退市组）"
        "→ 不可交付。\n"
        "【稳健】缩尾不敏感(q=0.01/0.05/0.10→t −3.36/−3.33/−3.21)；逐期符号 20/25 期为负；"
        "分年度 11/12 年为负（仅 2015 泡沫年为正）。【脆弱】剔最极端 2 期 t→−2.24；"
        "时间对半拆分前段 −1.65 vs 后段 −3.96。\n"
        "【前置】仅适用于 ashare_agri 池；换池（如中证 800）须重新过①②关，"
        "前置判据=只搬正交化后站得住的独立信息。",
        "v1", datetime.now().isoformat(timespec="seconds")))
    ev = [
        ("gate1", "outputs/2026-09-02/A股农业窄池基本面验证与终局结论.md",
         "gate1/2 主表：IC −0.1110、MDE 0.0877、t(HAC) −3.33（MCC 2.638）"),
        ("gate2", "outputs/2026-09-02/A股农业窄池基本面验证与终局结论.md",
         "正交化表：净利同比保留 59% IC、t=−3.16；ROE/合成是风格马甲"),
        ("gate3", "outputs/2026-09-02/ashare_fund_backtest_h60.json",
         "5 变体全 FAIL；净超额 +7.06%/IR 0.51/正年 7/13/非单调；幸存者敏感性 D=13"),
        ("gate3", "outputs/2026-09-02/ashare_fund_robust_h60.json",
         "剔最极端 2 期 t→−2.24；对半拆分前 −1.65 / 后 −3.96；缩尾敏感性稳健"),
    ]
    c.executemany("""INSERT OR REPLACE INTO strategy_evidence
        (strategy_key,gate,artifact,note) VALUES (?,?,?,?)""",
        [("agri_np_yoy_neglist", g, a, n) for g, a, n in ev])
    c.commit()
    print(f"已登记负面清单策略 agri_np_yoy_neglist（verdict=risk_signal_not_alpha）+ {len(ev)} 条证据")
    c.close()


def cmd_register_margin():
    """把「两融拥挤标签」登记为风控型策略（verdict=risk_signal_not_alpha）。

    ★ 与 agri_np_yoy_neglist 的关键区别（登记时必须区分，勿混为一谈）：
      农业 neglist 的 gate1/2 检验的是**信号 IC**（IC=−0.111、t(HAC)=−3.33）⇒ ①②关通过；
      两融**没有 IC 检验**（单股不可算 IC，见 ashare_margin_neglist 模块说明），
      检验的是「剔除拥挤组(EXCL) vs 等权(EQ)」的超额 ⇒ 三关全挂。
      两融标签的依据是**群体特征对比**（top10 CAGR 6.00% vs 全池 13.48%），
      而该对比**本身未做显著性检验** —— 这是它证据强度弱于农业 neglist 的地方。

    依据（2026-09-04，outputs/2026-09-04/margin_overlay/margin_overlay_result.json 主检）：
      chg20 / R=20 / D10 / fee 0.30%，148 块，2014-05-06~2026-08-05
      - EQ   : CAGR 13.477%  vol 21.66%  Sharpe 0.692  最大回撤 39.45%
      - EXCL : CAGR 13.570%  vol 21.54%  Sharpe 0.698  最大回撤 38.16%
      - TOP10: CAGR  6.004%  vol 24.96%  Sharpe 0.359  最大回撤 58.72%
      - EXCL vs EQ 净费年化差 +0.0517%，stat=+0.372，p(NW,单侧)=0.3551，
        MDE=0.794%/年，|效应|/MDE=0.17，逐年正 7/13 ⇒ 关卡③ FAIL。
    """
    src = OUT_DIR / "2026-09-04" / "margin_overlay" / "margin_overlay_result.json"
    if not src.exists():
        raise SystemExit(f"[FATAL] 缺失回测产物：{src}（拒绝凭记忆登记）")
    res = json.loads(src.read_text(encoding="utf-8"))
    m = next((x for x in res["cfgs"] if x.get("name", "").startswith("★主检")), None)
    if m is None:
        raise SystemExit("[FATAL] 回测产物中找不到主检配置 → 拒绝登记")
    t = m["test"]

    c = conn_reg()
    c.executescript(SCHEMA)
    c.execute("""INSERT OR REPLACE INTO strategy_registry
        (strategy_key,label,pool_key,components,required_fields,params,
         gate1_n_obs,gate1_sigma_true,gate1_mde,gate1_effect,gate1_ratio,gate1_pass,
         gate2_ic,gate2_t_hac,gate2_mcc_thr,gate2_n_tests,gate2_pass,
         gate3_gross,gate3_net,gate3_ir,gate3_ir_min,gate3_turnover,gate3_pos_years,
         gate3_monotonic,gate3_pass,
         attribution,attribution_note,verdict,boundary,version,validated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        "csi800_margin_crowding_neglist",
        "CSI800·两融拥挤负面清单（回避 chg20 拥挤度顶十分位）",
        "ashare_csi800",
        '[["margin_chg20_crowding", -1]]',
        '["altdata_margin"]',
        json.dumps({
            "holding": 20, "rebalance": "R=20 交易日", "cutoff": 0.10,
            "fee": 0.003,
            "usage": "risk_overlay：回避融资余额 20 日变化率处于横截面顶十分位的标的，不做空",
            "not_a_strategy": "不生成多头组合；剔除后相对等权无显著超额",
            "no_ic": "单股不可算 IC —— 该标签无 gate2 IC 口径，"
                     "检验的是 EXCL vs EQ 的组间差异",
        }, ensure_ascii=False),
        # gate1（功效）：MDE 0.794%/年 vs 效应 0.135%/年 → 比值 0.17 < 1 ⇒ FAIL
        t["n"], None, t["mde_ann_pct"] / 100.0, t["mean_diff_ann_pct"] / 100.0,
        t["effect_over_mde"], 0,
        # gate2（显著性）：无 IC；EXCL vs EQ 的 NW t=0.372、p(单侧)=0.3551 ⇒ FAIL
        None, t["stat"], None, 4, 0,
        # gate3（经济可行）：净费年化差 +0.05%、逐年正 7/13 ⇒ FAIL
        m["diff_ann_pct_net"] / 100.0, m["diff_ann_pct_net"] / 100.0, None, None,
        m["turn_ann_ex"], t["year_positive"], 0, 0,
        "crowding_fragility",
        "拥挤组（顶十分位）CAGR 6.00% vs 全池 13.48%、最大回撤 58.72% vs 39.45% —— "
        "脆弱性特征明确；但因该组仅占 10% 权重，剔除它只能把等权组合从 13.48% 抬到 "
        "13.57%（净费 +0.05%/年），统计上无法与噪声区分。",
        "risk_signal_not_alpha",
        "【定位】风控标签，非 alpha：三关全挂，不可宣称选股能力。\n"
        "【依据】**群体特征对比**：top10 CAGR 6.004% vs 全池 13.477%、"
        "最大回撤 58.72% vs 39.45%。⚠ 该对比本身**未做显著性检验**，"
        "是描述性群体差异，证据强度弱于 agri_np_yoy_neglist（后者有 IC 检验且 t=−3.33）。\n"
        "【为什么剔除无效】拥挤组仅占 10% 权重 ⇒ 剔除后等权组合仅 +0.05%/年"
        "（p(NW,单侧)=0.3551、|效应|/MDE=0.17、逐年正 7/13），统计上不可分辨。\n"
        "【边界外推】75~90 分位区（ELEVATED）**未单独检验**，不得与顶十分位同等对待；"
        "回测口径恒为 D10。\n"
        "【口径】横截面 = 全市场两融池（非池内）：池只决定「扫哪些股」，不进入判据，"
        "故跨池一致（csi800 10.5% vs wide 10.8%，交集 509 只判定差异 0）。\n"
        "【数据】两融覆盖 779 只；csi800 池内 778 只（99.7%）可判定，"
        "wide 池内仅 509 只（47.7%）可判定。stale_days 中位 1 天、max 2 天。",
        "v1", datetime.now().isoformat(timespec="seconds")))
    ev = [
        ("gate1", "outputs/2026-09-04/margin_overlay/margin_overlay_result.json",
         "主检 chg20 R=20 D10 fee0.30：n=148 块，MDE 0.794%/年 vs 效应 0.135%/年，"
         "|效应|/MDE=0.17 ⇒ 功效不足"),
        ("gate2", "outputs/2026-09-04/margin_overlay/margin_overlay_result.json",
         "无 IC 口径（单股不可算 IC）；EXCL vs EQ：NW t=0.372、p(单侧)=0.3551 ⇒ 不显著"),
        ("gate3", "outputs/2026-09-04/margin_overlay.log",
         "4 配置全 FAIL：主检/次级(R=5)/次级(D20)/敏感(fee0.50) 净费年化差 "
         "+0.05%~+0.77%，p 0.12~0.57，逐年正 5~8/13"),
        ("group", "outputs/2026-09-04/margin_overlay/margin_overlay_result.json",
         "群体特征（描述性，未做显著性检验）：TOP10 CAGR 6.004%/回撤 58.72% vs "
         "EQ CAGR 13.477%/回撤 39.45%"),
        ("crosspool", "outputs/2026-09-04/crosspool.log",
         "跨池一致性：csi800 780 只 CROWDED 82(10.5%)、wide 1068 只 CROWDED 55(10.8%)；"
         "交集 509 只判定差异 0、分位数差异 max 0.00e+00"),
    ]
    c.executemany("""INSERT OR REPLACE INTO strategy_evidence
        (strategy_key,gate,artifact,note) VALUES (?,?,?,?)""",
        [("csi800_margin_crowding_neglist", g, a, n) for g, a, n in ev])
    c.commit()
    print("已登记两融拥挤标签 csi800_margin_crowding_neglist"
          f"（verdict=risk_signal_not_alpha，三关全挂）+ {len(ev)} 条证据")
    c.close()


def cmd_register_csi800():
    """中证 800 换池检验结论登记（2026-09-02）。

    - pool_registry 增加 ashare_csi800（采集 780 只；有效池 760，剔 QC 黑名单 20 只）。
    - strategy_registry 登记 csi800_np_yoy（净利同比换池检验）：
      gate1/2 双 FAIL（非重叠 IC −0.031/t −1.45；月度口径 IC +0.016/t +1.06 符号翻转）
      → verdict=pool_specific_not_transferable（信息性零结果：月度 MDE 0.030 足以
      排除农业池量级 |IC|≈0.11 的效应存在）。
    """
    day = OUT_DIR / "2026-09-02"
    nn = json.loads((day / "ashare_csi800_fund_validate_h60.json").read_text(encoding="utf-8"))
    mo = json.loads((day / "ashare_csi800_fund_validate_h60_monthly.json").read_text(encoding="utf-8"))
    agg = json.loads((day / "ashare_agri_fund_validate_h60_monthly.json").read_text(encoding="utf-8"))
    r_nn = nn["factors"]["np_yoy_wins"]
    r_mo = mo["factors"]["np_yoy_wins"]
    r_ag = agg["factors"]["np_yoy_wins"]
    # 方向一致性自校验：月度口径下农业池必须仍为负且显著，否则对照不成立
    assert r_ag["ic_mean"] < 0 and abs(r_ag["t_hac"]) >= agg["mcc_threshold"], \
        f"农业池月度对照异常: {r_ag}"

    csi_db = OUT_DIR / "ashare_csi800_hfq_xq.sqlite"
    codes = []
    d_lo = d_hi = None
    if csi_db.exists():
        sc = sqlite3.connect(csi_db)
        codes = [r[0] for r in sc.execute(
            "SELECT DISTINCT code FROM daily_quotes_hfq ORDER BY code")]
        d_lo, d_hi = sc.execute(
            "SELECT MIN(trade_date), MAX(trade_date) FROM daily_quotes_hfq").fetchone()
        sc.close()

    c = conn_reg()
    c.executescript(SCHEMA)
    c.execute("""INSERT OR REPLACE INTO pool_registry
        (pool_key,label,universe_rule,n_codes,price_db,fund_db,date_from,date_to,
         has_close,has_volume,has_amount,has_turnover,has_fundamental,codes,notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        "ashare_csi800", "中证 800 池（20260902 成分快照）",
        "中证指数公司官方成分 000906cons.xls（2026-09-02 快照，800 只，无北交所/B 股）；"
        "剔 688981（股票级 hfq 缺失）→ 799 采集 → 成功 780",
        len(codes), "outputs/ashare_csi800_hfq_xq.sqlite",
        "outputs/ashare_csi800_fund.sqlite",
        d_lo, d_hi,
        1, 1, 0, 0, 1, json.dumps(codes, ensure_ascii=False),
        "⚠️ has_amount=0：daily_liquidity 仅 107 只（农业主库来源），validate 走 "
        "load_panel(amount_fallback=True) 近似口径（volume×100×close，仅限可交易性判定）；"
        "qc bad=1 共 20 只剔除（600595 超限249天+非正价、600601 93天、000750 35天 = 系统性损坏；"
        "其余 17 只散点 1~26 天保守剔除，含借壳/恢复上市混合，未逐日仲裁）；"
        "000657 2023-12-26~2024-01-09 缺口经官方公告核实为重组停牌（柿竹园），非数据缺陷；"
        "641 开放末段截头 bug 修复后采集，缺口扫描 0/780；"
        "19 只科创板+601399 采集失败=股票级 hfq 缺失（与 688981 同签名）"))
    c.execute("""INSERT OR REPLACE INTO strategy_registry
        (strategy_key,label,pool_key,components,required_fields,params,
         gate1_n_obs,gate1_sigma_true,gate1_mde,gate1_effect,gate1_ratio,gate1_pass,
         gate2_ic,gate2_t_hac,gate2_mcc_thr,gate2_n_tests,gate2_pass,
         gate3_gross,gate3_net,gate3_ir,gate3_ir_min,gate3_turnover,gate3_pos_years,
         gate3_monotonic,gate3_pass,
         attribution,attribution_note,verdict,boundary,version,validated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        "csi800_np_yoy",
        "中证 800·净利同比负向（农业池独立信号的换池检验）",
        "ashare_csi800",
        '[["np_yoy_wins", -1]]',
        '["fundamental"]',
        json.dumps({"holding": 60, "min_cross": 20, "min_history": 60,
                    "exclude_financial": True, "剔除": "金融/类金融 66 只"},
                   ensure_ascii=False),
        # gate1（功效）：非重叠 26 期
        int(r_nn["n_periods"]), r_nn["sigma_true"], r_nn["mde"], r_nn["ic_mean"],
        r_nn["ratio_ic_mde"], 0,
        # gate2（显著性）：7 因子 MCC 阈值 2.690
        r_nn["ic_mean"], r_nn["t_hac"], nn["mcc_threshold"], 7, 0,
        None, None, None, None, None, None, None, 0,
        "informative_null",
        "月度重叠口径（N=151，MDE 0.0296）：IC +0.0157、t +1.06 —— 符号相对非重叠口径翻转，"
        "噪声特征。农业池同口径对照：IC −0.0448、t −2.93 ✓（MCC 2.690）→ 板块内效应成立、"
        "宽池不存在。功效充足：月度 MDE 0.030 可检出 |IC|≥0.03，农业池量级 0.11 更会被轻松"
        "检出 → 排除「效应存在但样本不足」。",
        "pool_specific_not_transferable",
        "【定性】信息性零结果（不显著 + 功效充足排除大效应）。\n"
        "【机制解释】农业池净利同比负 IC 由板块周期驱动（2018 非洲猪瘟+去杠杆、2021 猪周期见顶："
        "盈利峰值后板块级崩塌），属板块择时信息而非截面选股因子 → 在宽池截面上消失。\n"
        "【影响】SOP §九「唯一活路」（换池保留基本面独立因子）在 gate1/2 即告死亡，"
        "A 股基本面 alpha 线正式关闭；农业池负面清单（risk_signal_not_alpha）仍仅限板块内使用。\n"
        "【口径警示】daily_liquidity 走近似回退口径，可交易判定不受影响；"
        "金融股按毛利率全缺失剔除（66 只）；min_history=60 防次新财报陷阱。",
        "v1", datetime.now().isoformat(timespec="seconds")))
    ev = [
        ("gate1", "outputs/2026-09-02/ashare_csi800_fund_validate_h60.json",
         "非重叠口径：IC −0.0314、t(HAC) −1.45、MDE 0.0704、|IC|/MDE 0.45（26 期）"),
        ("gate1", "outputs/2026-09-02/ashare_csi800_fund_validate_h60_monthly.json",
         "月度口径：IC +0.0157、t +1.06（N=151，MDE 0.0296）—— 符号翻转"),
        ("gate2", "outputs/2026-09-02/ashare_agri_fund_validate_h60_monthly.json",
         "农业池月度对照：IC −0.0448、t −2.93 ✓ —— 板块内效应成立、宽池不存在"),
        ("gate1", "outputs/csi800_probe/csi800_pilot_report.md",
         "数据底座：官方成分/采集试点/641 截头修复/QC 分诊全记录"),
    ]
    c.executemany("""INSERT OR REPLACE INTO strategy_evidence
        (strategy_key,gate,artifact,note) VALUES (?,?,?,?)""",
        [("csi800_np_yoy", g, a, n) for g, a, n in ev])
    c.commit()
    print(f"已登记 ashare_csi800 池（{len(codes)} 只）+ csi800_np_yoy "
          f"（verdict=pool_specific_not_transferable）+ {len(ev)} 条证据")
    c.close()


def cmd_list():
    c = conn_reg()
    try:
        pools = list(c.execute("SELECT pool_key,label,n_codes,has_turnover,has_fundamental FROM pool_registry"))
    except sqlite3.OperationalError:
        print("注册表尚未初始化，请先 --init"); return
    print(f"{'=' * 100}\n股票池")
    print(f"{'pool_key':<20}{'标签':<18}{'只数':>6}{'换手率':>8}{'基本面':>8}")
    for pk, lb, n, ht, hf in pools:
        print(f"{pk:<20}{lb:<18}{n or 0:>6}{'✓' if ht else '✗':>8}{'✓' if hf else '✗':>8}")

    print(f"\n{'=' * 100}\n策略")
    rows = list(c.execute("""SELECT strategy_key,label,pool_key,gate3_ir,gate3_ir_min,
        gate3_net,gate3_turnover,attribution,verdict,cost_ceiling,required_fields
        FROM strategy_registry ORDER BY gate3_ir DESC"""))
    for sk, lb, pk, ir, irm, net, to, at, vd, cc, rf in rows:
        print(f"\n  [{sk}]  {lb}")
        if ir is None:
            # 风控型策略（risk_signal_not_alpha）：无第③关组合指标
            print(f"    池={pk}   IR=—（风控型策略，无组合第③关）   定级={vd}")
        else:
            irm_s = f"{irm:.3f}" if irm is not None else "—"
            net_s = f"{net:.2%}" if net is not None else "—"
            cc_s = f"{cc:.3%}" if cc is not None else "—"
            print(f"    池={pk}   IR={ir:.3f}（相位最小 {irm_s}）   净超额={net_s}   年换手={to or 0:.1f}x")
            print(f"    归因={at}   定级={vd}   成本上限={cc_s}")
        print(f"    必需字段={json.loads(rf)}")
    c.close()


def cmd_match(code: str):
    """按股票代码匹配可用策略 —— 这是蓝图第 1~2 步的落地实现。

    匹配链：代码 → 所属池 → 池的数据能力 → 过滤掉字段不满足的策略 → 按定级排序
    """
    c = conn_reg()
    print(f"输入代码: {code}\n{'=' * 96}")
    hit = []
    for pk, lb, codes_j, ht, ha, hf in c.execute(
            "SELECT pool_key,label,codes,has_turnover,has_amount,has_fundamental FROM pool_registry"):
        codes = json.loads(codes_j or "[]")
        if code in codes:
            hit.append((pk, lb, ht, ha, hf, len(codes)))
    if not hit:
        print("✗ 该代码不属于任何已验证的股票池。")
        print("  → 按项目纪律，不得把窄池策略外推到未验证的标的（用户明确要求）。")
        print("  → 可选动作：把该标的所属类目建成新池并跑完三关，或明确告知无适用策略。")
        c.close(); return

    for pk, lb, ht, ha, hf, n in hit:
        print(f"✓ 命中池: {pk}（{lb}，{n} 只）")
        avail = {"close", "volume"}
        if ha: avail.add("amount")
        if ht: avail.add("turnover_pct")
        if hf: avail.add("fundamental")
        print(f"  池数据能力: {sorted(avail)}")
        rows = list(c.execute("""SELECT strategy_key,label,required_fields,params,
            gate3_ir,gate3_ir_min,gate3_net,attribution,verdict,cost_ceiling,boundary
            FROM strategy_registry WHERE pool_key=? ORDER BY gate3_ir DESC""", (pk,)))
        for sk, slb, rf, pr, ir, irm, net, at, vd, cc, bd in rows:
            need = set(json.loads(rf))
            miss = need - avail
            if miss:
                # ★ 这正是 nanmean 陷阱的拦截点
                print(f"\n  ✗ {slb}\n      缺字段 {sorted(miss)} → 拒绝匹配。"
                      f"\n      （若强行运行，build_signal 的 nanmean 会静默丢弃该分量，"
                      f"退化成子集策略且不报错）")
                continue
            print(f"\n  ✓ {slb}   [{sk}]")
            print(f"      定级 {vd}   归因 {at}")
            if ir is None:
                print("      IR —（风控型策略：回避清单，无组合第③关指标）")
            else:
                irm_s = f"{irm:.3f}" if irm is not None else "—"
                net_s = f"{net:.2%}" if net is not None else "—"
                cc_s = f"{cc:.3%}" if cc is not None else "—"
                print(f"      IR {ir:.3f}（相位最小 {irm_s}）  净超额 {net_s}  成本上限 {cc_s}")
            print(f"      参数 {json.loads(pr)}")
            print(f"      边界 {bd[:150]}...")
    c.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--load-agri", action="store_true")
    ap.add_argument("--register-factors", action="store_true",
                    help="登记 A股 5 个纯价量因子跨池(宽池+农业)验证结果到 ashare_factor_registry")
    ap.add_argument("--register-neglist", action="store_true",
                    help="登记基本面负面清单（净利同比回避清单，verdict=risk_signal_not_alpha）")
    ap.add_argument("--register-semi", action="store_true",
                    help="登记半导体池 gate1/2 复验结果（pool_registry + 因子 notes 追加）")
    ap.add_argument("--register-csi800", action="store_true",
                    help="登记中证 800 池与净利同比换池检验结论（pool_specific_not_transferable）")
    ap.add_argument("--register-margin", action="store_true",
                    help="登记两融拥挤负面清单（verdict=risk_signal_not_alpha，三关全挂）")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--match", default="")
    args = ap.parse_args()
    if args.init: cmd_init()
    if args.load_agri: load_agri()
    if args.register_factors: cmd_register_factors()
    if args.register_semi: cmd_register_semi()
    if args.register_neglist: cmd_register_neglist()
    if args.register_csi800: cmd_register_csi800()
    if args.register_margin: cmd_register_margin()
    if args.list: cmd_list()
    if args.match: cmd_match(args.match)
    if not any([args.init, args.load_agri, args.register_factors, args.register_semi,
                args.register_neglist, args.register_csi800, args.register_margin,
                args.list, args.match]):
        ap.print_help()


if __name__ == "__main__":
    main()
