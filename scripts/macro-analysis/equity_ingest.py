#!/usr/bin/env python3
"""Generic equity ingester: facts.json -> company_facts + derived_factors (append-only).

与 equity_fundamental.py 的区别：后者硬编码单只股票（hk03738）及其专属因子（CB 相关）；
本脚本为**通用入库器**，支持任意 ticker、任意币种/单位，只计算通用因子集，
公司专属因子仍由专用脚本承担。

红线：observed 层只存公告一手披露；推算值一律进 derived（formula 记录推算链）；
append-only，同批次(collected_at)重跑幂等，跨批次只增不修。

用法: equity_ingest.py ingest --facts <path/to/facts.json>
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[4] / "outputs" / "equity_fundamental.sqlite"

# 通用因子：依赖字段齐全才算，缺字段静默跳过（不编造）
# ⚠️ 口径异质（第 9 次归因纠错连带发现，2026-08-31）：H2 期存年报**全年数**
# （实测腾讯 2024H2 revenue=660,257mCNY=FY2024）。因此：
#   - revenue_yoy：H1 行=半年同比、H2 行=全年同比，期级 IC 混用两种窗口；
#   - inventory_to_revenue / fcf_proxy 等「流量做分母/水平」因子：
#     H1 分母=半年流量、H2 分母=全年流量，跨期差 2 倍，不可直接比较。
GENERIC_FACTORS = [
    ("revenue_yoy", "收入同比增速", "revenue[t]/revenue[t-1]-1", "ratio", "growth",
     ("revenue", "revenue")),
    ("gross_margin", "毛利率", "gross_profit/revenue", "ratio", "quality",
     ("gross_profit", "revenue")),
    ("gross_margin_chg", "毛利率同比变化(pct)", "(gm[t]-gm[t-1])*100", "pct", "quality",
     ("gross_profit", "revenue")),
    ("net_margin_proxy", "归母净利率(代理)", "net_profit_attributable/revenue", "ratio", "quality",
     ("net_profit_attributable", "revenue")),
    ("non_recurring_ratio", "非经常性损益/归母净利（盈利去伪）", "non_recurring_total/net_profit_attributable", "ratio", "quality",
     ("non_recurring_total", "net_profit_attributable")),
    ("recurring_profit_yoy", "经常性盈利同比(扣非经常性)", "(np-nonrecur)/(np_prev-nonrecur_prev)-1", "ratio", "growth",
     ("net_profit_attributable", "non_recurring_total")),
    ("net_margin_chg", "归母净利率同比变化(pct)", "(nm[t]-nm[t-1])*100", "pct", "quality",
     ("net_profit_attributable", "revenue")),
    ("ebitda_margin", "EBITDA 利润率", "ebitda/revenue", "ratio", "quality",
     ("ebitda", "revenue")),
    ("rd_intensity", "研发费用率", "rd_expense/revenue", "ratio", "quality",
     ("rd_expense", "revenue")),
    ("ocf_to_revenue", "经营现金流/收入", "ocf/revenue", "ratio", "quality",
     ("ocf", "revenue")),
    ("ocf_to_net_profit", "经营现金流/归母净利(现金含量)", "ocf/net_profit_attributable", "ratio", "quality",
     ("ocf", "net_profit_attributable")),
    ("fcf_proxy", "自由现金流代理(OCF-capex)", "ocf-capex", "kCUR", "quality",
     ("ocf", "capex")),
    # 口径纪律：官方披露的净债务优先；borrowings-cash 只是保守口径，两者不可混用
    ("net_debt_official", "净债务（公告官方口径）", "net_debt_official", "kCUR", "credit",
     ("net_debt_official", "net_debt_official")),
    ("net_debt_official_to_equity", "官方净债务/权益", "net_debt_official/total_equity", "ratio", "credit",
     ("net_debt_official", "total_equity")),
    ("debt_minus_cash", "有息负债-现金（保守口径，非官方净债务）", "borrowings_current+borrowings_non_current-cash", "kCUR", "credit",
     ("borrowings_current", "cash_and_equivalents")),
    ("debt_to_equity", "有息负债/权益", "(borrowings_current+borrowings_non_current)/total_equity", "ratio", "credit",
     ("borrowings_current", "total_equity")),
    ("cash_to_current_borrowings", "现金/短期有息负债", "cash/borrowings_current", "ratio", "credit",
     ("cash_and_equivalents", "borrowings_current")),
    ("inventory_to_revenue", "存货/当期收入", "inventories/revenue", "ratio", "asset_quality",
     ("inventories", "revenue")),
    ("asset_liability_ratio", "总负债/总资产", "total_liabilities/total_assets", "ratio", "credit",
     ("total_liabilities", "total_assets")),
]


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# H 期 -> (本期期末, 上期期末)
def _bs_of(flow_period: str) -> tuple[str, str]:
    """flow 期 -> (本期资产负债表日, 上期资产负债表日)。

    2025H1 -> ('2025-06-30', '2024-12-31')
    2025H2 -> ('2025-12-31', '2024-12-31')   # 年报：期末 vs 上年末
    """
    yr = int(flow_period[:4])
    if flow_period.endswith("H1"):
        return f"{yr}-06-30", f"{yr - 1}-12-31"
    return f"{yr}-12-31", f"{yr - 1}-12-31"


def recompute_derived(db: Path, ticker: str | None = None) -> int:
    """从 company_facts **回溯计算所有报告期**的 derived 因子。

    背景：LLM/规则抽取时同时给出 current 与 prior 两期数据，故 company_facts
    往往已存有多期（如 2025H1 + 2026H1）；但 ingest 每次只计算当期的 derived
    因子，历史期的因子缺失，导致基本面因子只有单一时点、无法做时序 IC。

    本函数对 company_facts 中每个已有的 flow 报告期补齐因子，使基本面因子
    具备多时点（这是基本面因子 IC 分析的前置条件）。
    """
    con = sqlite3.connect(db)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS derived_factors (
        ticker TEXT NOT NULL, factor_key TEXT NOT NULL, period TEXT NOT NULL,
        value REAL NOT NULL, unit TEXT NOT NULL, transform TEXT NOT NULL,
        transform_version TEXT NOT NULL DEFAULT 'v1',
        source TEXT NOT NULL DEFAULT 'derived', collected_at TEXT NOT NULL,
        PRIMARY KEY (ticker, factor_key, period, collected_at));
    CREATE TABLE IF NOT EXISTS factor_registry (
        factor_key TEXT PRIMARY KEY, description TEXT NOT NULL, formula TEXT NOT NULL,
        unit TEXT, layer TEXT NOT NULL, version TEXT NOT NULL, notes TEXT);
    """)
    batch = dt.date.today().isoformat()
    tickers = [ticker] if ticker else [r[0] for r in con.execute(
        "SELECT DISTINCT ticker FROM company_facts ORDER BY ticker")]
    total = 0
    for tk in tickers:
        f = {k: v for k, v in con.execute(
            "SELECT fact_key || '@' || period, value FROM company_facts WHERE ticker=?", (tk,))}
        flows = sorted({r[0].split("@")[1] for r in con.execute(
            "SELECT DISTINCT fact_key || '@' || period FROM company_facts "
            "WHERE ticker=? AND (period LIKE '%H1' OR period LIKE '%H2')", (tk,))})
        for cur in flows:
            yr = int(cur[:4])
            prv = f"{yr - 1}{cur[4:]}"
            cur_bs, prv_bs = _bs_of(cur)

            def val(key, period):
                return f.get(f"{key}@{period}")

            specs = [
                ("revenue_yoy", "营收同比", "rev(t)/rev(t-1)-1", "ratio",
                 lambda: val("revenue", cur) / val("revenue", prv) - 1),
                ("gross_margin", "毛利率", "gross_profit/revenue", "ratio",
                 lambda: val("gross_profit", cur) / val("revenue", cur)),
                ("net_margin_proxy", "归母净利率", "np_attr/revenue", "ratio",
                 lambda: val("net_profit_attributable", cur) / val("revenue", cur)),
                ("net_margin_chg", "净利率变动", "margin(t)-margin(t-1)", "pct",
                 lambda: (val("net_profit_attributable", cur) / val("revenue", cur)
                          - val("net_profit_attributable", prv) / val("revenue", prv)) * 100),
                ("asset_liability_ratio", "资产负债率", "liab/assets", "ratio",
                 lambda: val("total_liabilities", cur_bs) / val("total_assets", cur_bs)),
                ("inventory_to_revenue", "存货/营收", "inv/revenue", "ratio",
                 lambda: val("inventories", cur_bs) / val("revenue", cur)),
                ("ocf_to_revenue", "经营现金流/营收", "ocf/revenue", "ratio",
                 lambda: val("ocf", cur) / val("revenue", cur)),
                ("rd_intensity", "研发强度", "rd/revenue", "ratio",
                 lambda: val("rd_expense", cur) / val("revenue", cur)),
                # ---- 质量/金融通用因子（2026-09-01 类型化补位）----
                # 行业异质性审查发现：金融股（银行/保险/券商/交易所）无毛利、
                # 无经营现金流语义，net_margin/asset_liability 概念失效。
                # ROE/ROA/权益比率 对金融股才是核心因子（巴塞尔口径下的
                # 资本回报与资本充足代理），对非金融股同样有意义。
                # 分母用**期初期末平均**（避免用期末单时点夸大/低估回报率）。
                ("roe", "净资产收益率", "np_attr/avg(total_equity)", "ratio",
                 lambda: (2 * val("net_profit_attributable", cur)
                          / (val("total_equity", cur_bs) + val("total_equity", prv_bs)))),
                ("roa", "总资产收益率", "np_attr/avg(total_assets)", "ratio",
                 lambda: (2 * val("net_profit_attributable", cur)
                          / (val("total_assets", cur_bs) + val("total_assets", prv_bs)))),
                ("equity_ratio", "权益/资产", "equity/assets", "ratio",
                 lambda: val("total_equity", cur_bs) / val("total_assets", cur_bs)),
                ("net_profit_yoy", "归母净利润同比", "np_attr(t)/np_attr(t-1)-1", "ratio",
                 lambda: val("net_profit_attributable", cur)
                          / val("net_profit_attributable", prv) - 1),
            ]
            for fkey, desc, formula, unit, calc in specs:
                try:
                    v = calc()
                except (TypeError, ZeroDivisionError):
                    continue
                if v is None or v != v or v in (float("inf"), float("-inf")):
                    continue  # 缺字段/非法值跳过，不编造
                con.execute("INSERT OR REPLACE INTO factor_registry VALUES (?,?,?,?,?,?,?)",
                            (fkey, desc, formula, unit, "derived", "v1", None))
                con.execute("INSERT OR REPLACE INTO derived_factors VALUES (?,?,?,?,?,?,'v1','derived',?)",
                            (tk, fkey, cur, round(v, 6), unit, formula, batch))
                total += 1
    con.commit()
    con.close()
    print(f"[recompute] 写入 {total} 条跨期 derived 因子（{len(tickers)} 只标的）")
    return total


def compute_ttm_factors(db: Path, ticker: str | None = None) -> int:
    """TTM 滚动因子：trailing-twelve-months（半年度数据的行业标准用法）。

    ⚠️ 口径语义（第 9 次归因纠错，2026-08-31 修复）：company_facts 的 H2 期
    存的是**年报全年数（12 个月）**，不是下半年单季数（实测腾讯 2024H2
    revenue=660,257 mCNY = FY2024 全年；安踏 70,826 = FY2024）。故正确构造：

      TTM@H1(y) = H1(y) + [FY(y-1) - H1(y-1)]    # H1 + 上年下半年单季
      TTM@H2(y) = FY(y)                           # 全年本身就是 TTM
      TTM_yoy(P) = TTM(P) / TTM(去年同半) - 1

    首版实现误用 value(P)+value(前一期)，实为 18 个月混合（H1 行混入上年 FY、
    H2 行混入本年 H1），已废弃重算。任一成分缺失即跳过（红线：不编造）。
    """
    con = sqlite3.connect(db)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS derived_factors (
        ticker TEXT NOT NULL, factor_key TEXT NOT NULL, period TEXT NOT NULL,
        value REAL NOT NULL, unit TEXT NOT NULL, transform TEXT NOT NULL,
        transform_version TEXT NOT NULL DEFAULT 'v1', source TEXT NOT NULL DEFAULT 'derived',
        collected_at TEXT NOT NULL, PRIMARY KEY (ticker, factor_key, period, collected_at));
    CREATE TABLE IF NOT EXISTS factor_registry (
        factor_key TEXT PRIMARY KEY, description TEXT NOT NULL, formula TEXT NOT NULL,
        unit TEXT, layer TEXT NOT NULL, version TEXT NOT NULL, notes TEXT);
    """)
    batch = dt.date.today().isoformat()
    # 先清掉首版错误 TTM 行（18 个月混合口径）：derived/学习产物可安全重算，
    # observed 层（company_facts）不受影响。
    con.execute("DELETE FROM derived_factors WHERE factor_key LIKE 'ttm_%'")
    con.execute("DELETE FROM factor_registry WHERE factor_key LIKE 'ttm_%'")

    tickers = [ticker] if ticker else [r[0] for r in con.execute(
        "SELECT DISTINCT ticker FROM company_facts ORDER BY ticker")]

    specs = {
        "ttm_revenue_yoy": ("TTM 营收同比", "TTM_rev(t)/TTM_rev(t-1)-1"),
        "ttm_net_profit_yoy": ("TTM 归母净利同比", "TTM_np(t)/TTM_np(t-1)-1"),
        "ttm_ocf_yoy": ("TTM 经营现金流同比", "TTM_ocf(t)/TTM_ocf(t-1)-1"),
        "ttm_gross_margin": ("TTM 毛利率", "TTM_gp/TTM_rev"),
        "ttm_net_margin": ("TTM 归母净利率", "TTM_np/TTM_rev"),
        "ttm_ocf_to_revenue": ("TTM 经营现金流/营收", "TTM_ocf/TTM_rev"),
    }
    reg_note = ("TTM 正确口径：H1=H1+FY(上年)-H1(上年)；H2=FY 全年。"
                "第 9 次归因纠错：H2 期存全年数，首版 value(P)+value(前一期) 为 18 个月混合，已废弃")

    total = 0
    for tk_ in tickers:
        rows = con.execute(
            "SELECT period, fact_key, value FROM company_facts WHERE ticker=? "
            "AND (period LIKE '%H1' OR period LIKE '%H2')", (tk_,)).fetchall()
        pmap: dict[str, dict[str, float]] = {}
        for period, fk, val in rows:
            pmap.setdefault(period, {})[fk] = val

        def f(m, p):
            return pmap.get(p, {}).get(m) if p else None

        def ttm_of(m, period):
            """正确 TTM：H2=FY 全年；H1=H1 + FY(上年) - H1(上年)。缺成分返回 None。"""
            v_cur = f(m, period)
            if v_cur is None:
                return None
            if period.endswith("H2"):
                return v_cur
            y = int(period[:4])
            fy = f(m, f"{y - 1}H2")
            h1p = f(m, f"{y - 1}H1")
            if fy is None or h1p is None:
                return None
            return v_cur + fy - h1p

        def prev_same(period):
            y = int(period[:4])
            return f"{y - 1}H1" if period.endswith("H1") else f"{y - 1}H2"

        def write(fkey, period, v):
            con.execute("INSERT OR REPLACE INTO factor_registry VALUES (?,?,?,?,?,?,?)",
                        (fkey, specs[fkey][0], specs[fkey][1], "ratio", "derived", "v1", reg_note))
            con.execute("INSERT OR REPLACE INTO derived_factors VALUES (?,?,?,?,?,?,'v1','derived',?)",
                        (tk_, fkey, period, round(v, 6), "ratio", specs[fkey][1], batch))

        for period in sorted(pmap.keys()):
            # 水平比率（分子分母同一 TTM 窗口）
            for fkey, (num_m, den_m) in {
                "ttm_gross_margin": ("gross_profit", "revenue"),
                "ttm_net_margin": ("net_profit_attributable", "revenue"),
                "ttm_ocf_to_revenue": ("ocf", "revenue"),
            }.items():
                num, den = ttm_of(num_m, period), ttm_of(den_m, period)
                if num is None or den in (None, 0):
                    continue
                v = num / den
                if v != v or v in (float("inf"), float("-inf")):
                    continue
                write(fkey, period, v)
                total += 1
            # 同比（去年同期 TTM）
            for fkey, m in {"ttm_revenue_yoy": "revenue",
                            "ttm_net_profit_yoy": "net_profit_attributable",
                            "ttm_ocf_yoy": "ocf"}.items():
                cur_t, prev_t = ttm_of(m, period), ttm_of(m, prev_same(period))
                if cur_t is None or prev_t in (None, 0):
                    continue
                v = cur_t / prev_t - 1
                if v != v or v in (float("inf"), float("-inf")):
                    continue
                write(fkey, period, v)
                total += 1
    con.commit()
    con.close()
    print(f"[ttm] 写入 {total} 条 TTM 跨期 derived 因子（{len(tickers)} 只标的，正确口径）")
    return total


def self_test() -> bool:
    """已知答案校验：H2=FY 语义下的 TTM 构造（第 9 次归因纠错的防回归护栏）。"""
    import tempfile
    results: list[tuple[str, bool, str]] = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as fh:
        tdb = Path(fh.name)
    try:
        con = sqlite3.connect(tdb)
        con.executescript("""
        CREATE TABLE company_facts (
            ticker TEXT NOT NULL, fact_key TEXT NOT NULL, period TEXT NOT NULL,
            freq TEXT NOT NULL DEFAULT 'H', value REAL, unit TEXT NOT NULL,
            source TEXT NOT NULL, source_url TEXT, release_date TEXT,
            collected_at TEXT NOT NULL,
            PRIMARY KEY (ticker, fact_key, period, freq, collected_at, source));
        """)
        con.close()
        # 合成公司：H2 存全年数（真实语义）
        # revenue: 2022H1=350 2022H2=900(FY22) 2023H1=400 2023H2=1000(FY23)
        #          2024H1=500 2024H2=1200(FY24)
        # gross_profit: 2023H1=120 2023H2=300(FY23) 2024H1=150 2024H2=360(FY24)
        facts = [
            ("revenue", "2022H1", 350), ("revenue", "2022H2", 900),
            ("revenue", "2023H1", 400), ("revenue", "2023H2", 1000),
            ("revenue", "2024H1", 500), ("revenue", "2024H2", 1200),
            ("gross_profit", "2023H1", 120), ("gross_profit", "2023H2", 300),
            ("gross_profit", "2024H1", 150), ("gross_profit", "2024H2", 360),
        ]
        con = sqlite3.connect(tdb)
        for fk, p, v in facts:
            con.execute("INSERT INTO company_facts VALUES "
                        "('hkT', ?, ?, 'H', ?, 'kCNY', 'test', NULL, NULL, '2026-08-31')",
                        (fk, p, v))
        con.commit()  # ⚠️ 不 commit 则 close 时回滚，compute_ttm_factors 读到空库
        con.close()
        n = compute_ttm_factors(tdb, "hkT")
        check("ttm rows written", n > 0, f"n={n}")

        con = sqlite3.connect(tdb)

        def get(fk, p):
            r = con.execute(
                "SELECT value FROM derived_factors WHERE ticker='hkT' AND factor_key=? AND period=?",
                (fk, p)).fetchone()
            return r[0] if r else None

        # TTM@2024H1 = 500 + 1000 - 400 = 1100；TTM@2023H1 = 400 + 900 - 350 = 950
        yoy = get("ttm_revenue_yoy", "2024H1")
        check("ttm_revenue_yoy@2024H1 = 1100/950-1 ≈ 0.1579",
              yoy is not None and abs(yoy - 0.157894) < 1e-4, f"v={yoy}")
        # TTM@H2 = FY 本身：1200/1000 - 1 = 0.2
        yoy2 = get("ttm_revenue_yoy", "2024H2")
        check("ttm_revenue_yoy@2024H2 = 0.2 (FY/FY)",
              yoy2 is not None and abs(yoy2 - 0.2) < 1e-9, f"v={yoy2}")
        # ttm_gross_margin@2024H2 = 360/1200 = 0.3（FY 毛利率）
        gm = get("ttm_gross_margin", "2024H2")
        check("ttm_gross_margin@2024H2 = 0.3", gm is not None and abs(gm - 0.3) < 1e-9, f"v={gm}")
        # ttm_gross_margin@2024H1 = TTM_gp/TTM_rev = (150+300-120)/(500+1000-400) = 330/1100 = 0.3
        gm1 = get("ttm_gross_margin", "2024H1")
        check("ttm_gross_margin@2024H1 = 330/1100 = 0.3",
              gm1 is not None and abs(gm1 - 0.3) < 1e-9, f"v={gm1}")
        # 缺成分的期（2020-2022 相关）不得有 TTM 行（红线：不编造）
        check("no ttm rows for periods lacking components",
              get("ttm_revenue_yoy", "2021H1") is None
              and get("ttm_gross_margin", "2021H2") is None
              and get("ttm_revenue_yoy", "2022H1") is None)
        # 幂等：重跑行数不变
        n2 = compute_ttm_factors(tdb, "hkT")
        cnt = con.execute(
            "SELECT COUNT(*) FROM derived_factors WHERE ticker='hkT'").fetchone()[0]
        con.close()
        check("idempotent (same count)", n2 == n and cnt == n,
              f"n={n} n2={n2} cnt={cnt}")

        # ---- 类型化因子护栏（2026-09-01）：金融股靠 ROE/ROA/权益比率，
        # 分母须用期初期末平均，避免用期末单时点夸大回报率。----
        con = sqlite3.connect(tdb)
        fin = [
            ("net_profit_attributable", "2024H1", 50),
            ("net_profit_attributable", "2024H2", 120),
            ("net_profit_attributable", "2023H1", 40),
            ("total_equity", "2023-12-31", 900),   # 2024H1 期初
            ("total_equity", "2024-06-30", 1000),  # 2024H1 期末
            ("total_equity", "2024-12-31", 1100),  # 2024H2 期末
            ("total_assets", "2023-12-31", 9000),
            ("total_assets", "2024-06-30", 10000),
            ("total_assets", "2024-12-31", 11000),
        ]
        for fk, p, v in fin:
            con.execute("INSERT OR REPLACE INTO company_facts VALUES "
                        "('hkF', ?, ?, 'H', ?, 'mCNY', 'test', NULL, NULL, '2026-09-01')",
                        (fk, p, v))
        con.commit()
        con.close()
        recompute_derived(tdb, "hkF")
        con = sqlite3.connect(tdb)

        def g2(fk, p):
            r = con.execute("SELECT value FROM derived_factors WHERE ticker='hkF' "
                            "AND factor_key=? AND period=?", (fk, p)).fetchone()
            return r[0] if r else None

        # roe@2024H1 = 50 / ((1000+900)/2) = 50/950 = 0.052632
        roe = g2("roe", "2024H1")
        check("roe@2024H1 = 50/avg(1000,900) ≈ 0.05263",
              roe is not None and abs(roe - 0.052632) < 1e-4, f"v={roe}")
        # roa@2024H1 = 50 / ((10000+9000)/2) = 50/9500 = 0.005263
        roa = g2("roa", "2024H1")
        check("roa@2024H1 = 50/avg(10000,9000) ≈ 0.005263",
              roa is not None and abs(roa - 0.005263) < 1e-4, f"v={roa}")
        # equity_ratio@2024H1 = 1000/10000 = 0.1
        er = g2("equity_ratio", "2024H1")
        check("equity_ratio@2024H1 = 0.1", er is not None and abs(er - 0.1) < 1e-9, f"v={er}")
        # net_profit_yoy@2024H1 = 50/40 - 1 = 0.25
        npy = g2("net_profit_yoy", "2024H1")
        check("net_profit_yoy@2024H1 = 0.25", npy is not None and abs(npy - 0.25) < 1e-9,
              f"v={npy}")
        con.close()
    finally:
        try:
            tdb.unlink(missing_ok=True)
        except PermissionError:
            pass

    ok = all(c for _, c, _ in results)
    for name, passed, detail in results:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}  {detail}")
    print(f"[self-test] {'ALL PASS' if ok else 'FAILED'} "
          f"({sum(c for _, c, _ in results)}/{len(results)})")
    return ok


def ingest(db: Path, facts_path: Path) -> None:
    spec = json.loads(facts_path.read_text(encoding="utf-8"))
    ticker = spec["ticker"]
    src = spec["source"]
    url = spec.get("url")
    release = spec.get("release_date")
    unit_default = spec.get("unit_default", "kCUR")
    con = sqlite3.connect(db)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS company_facts (
        ticker TEXT NOT NULL, fact_key TEXT NOT NULL, period TEXT NOT NULL,
        freq TEXT NOT NULL DEFAULT 'H', value REAL, unit TEXT NOT NULL,
        source TEXT NOT NULL, source_url TEXT, release_date TEXT, collected_at TEXT NOT NULL,
        PRIMARY KEY (ticker, fact_key, period, freq, collected_at, source));
    CREATE TABLE IF NOT EXISTS market_quotes (
        ticker TEXT NOT NULL, metric TEXT NOT NULL, quote_date TEXT NOT NULL,
        value REAL, unit TEXT NOT NULL, source TEXT NOT NULL, collected_at TEXT NOT NULL,
        PRIMARY KEY (ticker, metric, quote_date, collected_at, source));
    CREATE TABLE IF NOT EXISTS derived_factors (
        ticker TEXT NOT NULL, factor_key TEXT NOT NULL, period TEXT NOT NULL,
        value REAL NOT NULL, unit TEXT NOT NULL, transform TEXT NOT NULL,
        transform_version TEXT NOT NULL DEFAULT 'v1', source TEXT NOT NULL DEFAULT 'derived',
        collected_at TEXT NOT NULL, PRIMARY KEY (ticker, factor_key, period, collected_at));
    CREATE TABLE IF NOT EXISTS factor_registry (
        factor_key TEXT PRIMARY KEY, description TEXT NOT NULL, formula TEXT NOT NULL,
        unit TEXT, layer TEXT NOT NULL, version TEXT NOT NULL, notes TEXT);
    CREATE TABLE IF NOT EXISTS source_trust (
        source TEXT PRIMARY KEY, authority TEXT NOT NULL, trust_level TEXT NOT NULL,
        attribution TEXT NOT NULL);
    """)
    # batch 用天级日期（与 equity_fundamental.py 一致）：同一天重跑幂等，跨天才是新 vintage 批次。
    # 绝不用秒级时间戳——否则每次重跑都生成新批次，造成无修订数据的冗余累积。
    batch = dt.date.today().isoformat()
    # market_quotes 无多期语义，按 batch 幂等即可
    con.execute("DELETE FROM market_quotes WHERE ticker=? AND collected_at=?", (ticker, batch))
    # ⚠️ company_facts / derived_factors 有**多报告期共存**语义（2025H1 与 2026H1 并存），
    # 绝不可按 batch 删除：同一天回溯历史期会连带删掉当天写入的当期数据
    # （实测安踏：回溯 2025H1 后，2026H1 的原始 facts 被删空，仅剩 derived 残留）。
    # 幂等单位是 (ticker, period)：同报告期重抽覆盖，跨报告期共存。
    # 同报告期覆盖（不区分 source）：否则规则抽取旧批次（单位不同）会与 LLM 新数据混合
    # （实测中芯：kUSD 千美元旧数据与 mUSD 百万美元新数据混算致勾稽失败）。
    for p in {row[1] for row in spec["facts"]}:
        con.execute("DELETE FROM company_facts WHERE ticker=? AND period=?", (ticker, p))
    # derived 因子：仅覆盖本次计算的报告期，不触碰其他报告期
    con.execute("DELETE FROM derived_factors WHERE ticker=? AND period=? AND source='derived'",
                (ticker, spec.get("periods", {}).get("current", "")))

    cur_period = spec.get("periods", {}).get("current", "")
    for row in spec["facts"]:
        key, period, value = row[0], row[1], row[2]
        unit = row[3] if len(row) > 3 else unit_default
        # ⚠️ release_date 只属于 current 期：prior 期若沿用 current 的公告日，
        # 会把发布日记成一年后（实测 2024H2 作为 2025H2 的 prior，发布日被记成
        # 2026-03，真实是 2025-03）——PEAD 事件研究的事件窗将完全错位。
        # prior 期置 NULL，让生效日逻辑回落到标准发布日（保守、无前视）。
        rel = release if period == cur_period else None
        con.execute("INSERT OR IGNORE INTO company_facts VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (ticker, key, period, "H", value, unit, src, url, rel, batch))
    for row in spec.get("quotes", []):
        metric, qdate, value = row[0], row[1], row[2]
        unit = row[3] if len(row) > 3 else "HKD"
        qsrc = spec.get("quote_source", "third_party_quote")
        con.execute("INSERT OR IGNORE INTO market_quotes VALUES (?,?,?,?,?,?,?)",
                    (ticker, metric, qdate, value, unit, qsrc, batch))
    con.execute("INSERT OR REPLACE INTO source_trust VALUES (?,?,?,?)",
                (src, spec.get("source_label", "公告来源"), "official",
                 spec.get("source_note", "官方一手公告")))
    if spec.get("quotes"):
        con.execute("INSERT OR REPLACE INTO source_trust VALUES (?,?,?,?)",
                    (spec.get("quote_source", "third_party_quote"), spec.get("quote_label", "行情源"),
                     "third_party", "第三方行情快照"))
    con.commit()

    f = {k: v for k, v in con.execute(
        "SELECT fact_key || '@' || period, value FROM company_facts WHERE ticker=?", (ticker,))}

    def r(key, period):
        return f.get(f"{key}@{period}")

    def rd(key, period):
        return r(key, f"{period}-06-30")

    periods = spec.get("periods", {})
    cur, prv, cur_bs, prv_bs = (
        periods.get("current", "2026H1"), periods.get("prior", "2025H1"),
        periods.get("current_bs", "2026-06-30"), periods.get("prior_bs", "2025-12-31"))

    def val(key, period):
        return r(key, period if period.endswith("H1") else period)

    out = []

    def calc(fkey, desc, formula, unit, layer, deps):
        try:
            if fkey == "revenue_yoy":
                a, b = val("revenue", cur), val("revenue", prv)
                v = a / b - 1
            elif fkey == "gross_margin":
                v = val("gross_profit", cur) / val("revenue", cur)
            elif fkey == "gross_margin_chg":
                v = (val("gross_profit", cur) / val("revenue", cur)
                     - val("gross_profit", prv) / val("revenue", prv)) * 100
            elif fkey == "net_margin_proxy":
                v = val("net_profit_attributable", cur) / val("revenue", cur)
            elif fkey == "non_recurring_ratio":
                v = val("non_recurring_total", cur) / val("net_profit_attributable", cur)
            elif fkey == "recurring_profit_yoy":
                # 缺任一期的非经常性数据即跳过：不得用 0 填补（红线：不编造）
                nr_cur = val("non_recurring_total", cur)
                nr_prv = val("non_recurring_total", prv)
                if nr_cur is None or nr_prv is None:
                    return
                a = val("net_profit_attributable", cur) - nr_cur
                b = val("net_profit_attributable", prv) - nr_prv
                v = a / b - 1
            elif fkey == "net_margin_chg":
                v = (val("net_profit_attributable", cur) / val("revenue", cur)
                     - val("net_profit_attributable", prv) / val("revenue", prv)) * 100
            elif fkey == "ebitda_margin":
                v = val("ebitda", cur) / val("revenue", cur)
            elif fkey == "rd_intensity":
                v = val("rd_expense", cur) / val("revenue", cur)
            elif fkey == "ocf_to_revenue":
                v = val("ocf", cur) / val("revenue", cur)
            elif fkey == "ocf_to_net_profit":
                v = val("ocf", cur) / val("net_profit_attributable", cur)
            elif fkey == "fcf_proxy":
                v = val("ocf", cur) - val("capex", cur)
            elif fkey == "net_debt_official":
                v = val("net_debt_official", cur_bs)
            elif fkey == "net_debt_official_to_equity":
                v = val("net_debt_official", cur_bs) / val("total_equity", cur_bs)
            elif fkey == "debt_minus_cash":
                v = (val("borrowings_current", cur_bs) + val("borrowings_non_current", cur_bs)
                     - val("cash_and_equivalents", cur_bs))
            elif fkey == "debt_to_equity":
                v = ((val("borrowings_current", cur_bs) + val("borrowings_non_current", cur_bs))
                     / val("total_equity", cur_bs))
            elif fkey == "cash_to_current_borrowings":
                v = val("cash_and_equivalents", cur_bs) / val("borrowings_current", cur_bs)
            elif fkey == "inventory_to_revenue":
                v = val("inventories", cur_bs) / val("revenue", cur)
            elif fkey == "asset_liability_ratio":
                v = val("total_liabilities", cur_bs) / val("total_assets", cur_bs)
            else:
                return
        except (TypeError, ZeroDivisionError, KeyError):
            return  # 缺字段静默跳过，不编造
        # 缺字段或算出 None/NaN 一律跳过（红线：不编造、不入库空值）
        if v is None or (isinstance(v, float) and (v != v or v in (float("inf"), float("-inf")))):
            return
        unit_final = unit.replace("CUR", unit_default.lstrip("k")) if unit == "kCUR" else unit
        con.execute("INSERT OR REPLACE INTO factor_registry VALUES (?,?,?,?,?,?,?)",
                    (fkey, desc, formula, unit_final, layer, "v1", None))
        con.execute("INSERT OR IGNORE INTO derived_factors VALUES (?,?,?,?,?,?,'v1','derived',?)",
                    (ticker, fkey, cur, round(v, 6), unit_final, formula, batch))
        out.append((fkey, round(v, 6)))
        return v

    for fkey, desc, formula, unit, layer, _deps in GENERIC_FACTORS:
        calc(fkey, desc, formula, unit, layer, _deps)

    # ---- 估值因子（依赖行情 + 股本，汇率假设 7.8 标注在 formula 与 note）----
    try:
        price = float(con.execute(
            "SELECT value FROM market_quotes WHERE ticker=? AND metric='share_price_close' "
            "ORDER BY quote_date DESC LIMIT 1", (ticker,)).fetchone()[0])
        shares = val("shares_outstanding", cur_bs)
        np_attr = val("net_profit_attributable", cur)
        te = val("total_equity", cur_bs)
        nci = val("non_controlling_interests", cur_bs)
        if price and shares and np_attr and te and nci:
            usd_hkd = 7.8
            mcap_hkd = shares * price
            pe_ann = mcap_hkd / (np_attr * 2 * 1000 * usd_hkd)
            equity_attr_hkd = (te - nci) * 1000 * usd_hkd
            pb_att = mcap_hkd / equity_attr_hkd
            val_factors = [
                ("market_cap_hkd", "市值(港元,股本×价)", f"{mcap_hkd:,.0f}", "HKD",
                 "shares_outstanding × share_price_close"),
                ("pe_annualized", "年化PE(归母)", f"{pe_ann:.4f}", "ratio",
                 "market_cap_hkd / (net_profit_attributable×2×1000×7.8)"),
                ("pb_attributable", "市净率(归母)", f"{pb_att:.4f}", "ratio",
                 "market_cap_hkd / ((total_equity-nci)×1000×7.8)"),
            ]
            for key, desc, disp, unit, formula in val_factors:
                con.execute("INSERT OR REPLACE INTO factor_registry VALUES (?,?,?,?,?,?,?)",
                            (key, desc, formula, unit, "valuation", "v1",
                             "汇率假设 USD/HKD=7.8；推算值，估值类因子依赖行情快照"))
                con.execute("INSERT OR IGNORE INTO derived_factors VALUES (?,?,?,?,?,?,'v1','derived',?)",
                            (ticker, key, cur_bs, round(float(disp.replace(',', '')), 4) if key == "pe_annualized"
                             or key == "pb_attributable" else float(disp.replace(',', '')),
                             unit, formula, batch))
                out.append((key, round(float(disp.replace(',', '')), 4)))
    except Exception:
        pass  # 缺股本/价格/权益即跳过估值因子，不编造

    con.commit()
    print(f"[ingest] {ticker} batch={batch} facts={len(spec['facts'])} "
          f"quotes={len(spec.get('quotes', []))} derived={len(out)}")
    for k, v in out:
        print(f"  {k:30s} {v:14,.4f}")
    con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--facts", default="")
    ap.add_argument("--ticker", default=None)
    ap.add_argument("cmd", nargs="?", default="ingest",
                    choices=["ingest", "recompute", "ttm", "self-test"])
    args = ap.parse_args()
    if args.cmd == "ingest":
        if not args.facts:
            ap.error("ingest 需要 --facts")
        ingest(Path(args.db), Path(args.facts))
    elif args.cmd == "recompute":
        recompute_derived(Path(args.db), args.ticker)
    elif args.cmd == "ttm":
        compute_ttm_factors(Path(args.db), args.ticker)
    elif args.cmd == "self-test":
        raise SystemExit(0 if self_test() else 1)


if __name__ == "__main__":
    main()
