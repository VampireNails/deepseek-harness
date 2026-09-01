#!/usr/bin/env python3
"""Equity fundamental vintage DB: company facts (observed) -> derived factors -> report.

红线对齐宏观三飞轮：
1. 数据层只增不修：append-only，跨 collected_at 批次不可变（同批次重跑幂等）；
2. observed / derived 分层：derived_factors.source='derived'，绝不覆盖官方原始字段；
3. 源可信度：source_trust 登记来源属性，第三方行情如实标注，不冒充官方一手。

G1（2026-08-28 修复）：行情/估值数据从 company_facts(observed=官方一手) 拆出，
独立 market_quotes 表（third_party 如实标注）。
G2（2026-08-28 修复）：现金流新增残差口径（ocf/fcf_proxy_residual，与资产负债表
反推一致），upper 口径保留并标注为"上限参考"。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[4] / "outputs" / "equity_fundamental.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS company_facts (
    ticker       TEXT NOT NULL,
    fact_key     TEXT NOT NULL,
    period       TEXT NOT NULL,
    freq         TEXT NOT NULL DEFAULT 'H',
    value        REAL,
    unit         TEXT NOT NULL,
    source       TEXT NOT NULL,
    source_url   TEXT,
    release_date TEXT,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (ticker, fact_key, period, freq, collected_at, source)
);
CREATE TABLE IF NOT EXISTS market_quotes (
    ticker       TEXT NOT NULL,
    metric       TEXT NOT NULL,
    quote_date   TEXT NOT NULL,
    value        REAL,
    unit         TEXT NOT NULL,
    source       TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (ticker, metric, quote_date, collected_at, source)
);
CREATE TABLE IF NOT EXISTS derived_factors (
    ticker            TEXT NOT NULL,
    factor_key        TEXT NOT NULL,
    period            TEXT NOT NULL,
    value             REAL NOT NULL,
    unit              TEXT NOT NULL,
    transform         TEXT NOT NULL,
    transform_version TEXT NOT NULL DEFAULT 'v1',
    source            TEXT NOT NULL DEFAULT 'derived',
    collected_at      TEXT NOT NULL,
    PRIMARY KEY (ticker, factor_key, period, collected_at)
);
CREATE TABLE IF NOT EXISTS factor_registry (
    factor_key  TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    formula     TEXT NOT NULL,
    unit        TEXT,
    layer       TEXT NOT NULL,
    version     TEXT NOT NULL,
    notes       TEXT
);
CREATE TABLE IF NOT EXISTS source_trust (
    source      TEXT PRIMARY KEY,
    authority   TEXT NOT NULL,
    trust_level TEXT NOT NULL,
    attribution TEXT NOT NULL
);
"""

TICKER = "hk03738"
HKEX_URL = "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0828/2026082800086_c.pdf"
HKEX_SRC = "hkex:2026082800086"
QUOTE_SRC = "tencent_quotes:aastocks"

# (factor_key, 描述, formula, unit, layer, version, notes)
REGISTRY = [
    ("revenue_yoy", "收入同比增速", "revenue[t]/revenue[t-1]-1", "ratio", "growth", "v1", None),
    ("gross_margin", "毛利率", "gross_profit[t]/revenue[t]", "ratio", "quality", "v1", None),
    ("gross_margin_chg", "毛利率同比变化(pct)", "(gm[t]-gm[t-1])*100", "pct", "quality", "v1", None),
    ("gross_profit_check", "毛利勾稽:毛利-(收入-服务成本)", "gross_profit[t]-(revenue[t]-cost_of_services[t])", "kHKD", "quality", "v1", "应为0，非0即勾稽失败"),
    ("net_profit_yoy", "期内溢利同比增速", "net_profit[t]/net_profit[t-1]-1", "ratio", "growth", "v1", None),
    ("adj_net_profit_yoy", "经调整纯利同比增速", "adj_net_profit[t]/adj_net_profit[t-1]-1", "ratio", "growth", "v1", "更干净的盈利增速锚"),
    ("earnings_quality_gap", "盈利去伪因子：报表-经调整增速差", "net_profit_yoy-adj_net_profit_yoy", "ratio", "quality", "v1", "差值越大，净利增速中一次性成分越高"),
    ("adj_ebitda_yoy", "经调整EBITDA同比增速", "adj_ebitda[t]/adj_ebitda[t-1]-1", "ratio", "growth", "v1", None),
    ("receivables_to_revenue", "应收净额/当期收入", "trade_receivables_net[t]/revenue[t]", "ratio", "asset_quality", "v1", None),
    ("dso_days", "应收周转天数(期末口径)", "trade_receivables_net[t]/revenue[t]*182", "days", "asset_quality", "v1", "贸易条款10-180天"),
    ("ecl_ratio", "预期信贷损失率", "ecl_allowance[t]/trade_receivables_gross[t]", "ratio", "asset_quality", "v1", None),
    ("ocf_proxy_upper", "经营现金流上限估算", "net_profit[t]+da[t]+sbc[t]", "kHKD", "quality", "v1", "粗口径：忽略营运资金变动，仅上限参考"),
    ("fcf_proxy_upper", "自由现金流上限估算", "ocf_proxy_upper[t]-capex[t]", "kHKD", "quality", "v1", "粗口径，见 ocf_proxy_upper"),
    ("ocf_proxy_residual", "经营现金流残差反推", "delta_cash+capex+cb_repurchase_cash-borrowings_net_inc+interest_paid-exercise_cash", "kHKD", "quality", "v1", "利息已付按租赁460近似(下限口径)；若借款利息实付，上限约1.69亿。噪声≈4,054万"),
    ("fcf_proxy_residual", "自由现金流残差反推", "ocf_proxy_residual[t]-capex[t]", "kHKD", "quality", "v1", "与 ocf_proxy_residual 同噪声带"),
    ("cb_principal_outstanding_est", "CB未偿本金估算", "cb_principal_issued-converted-repurchased", "kHKD", "credit", "v1", "推算值：observed 层只存一手披露，本因子入 derived 层"),
    ("cb_gap_to_cash", "CB到期未偿本金(est)-现金", "cb_principal_outstanding_est[t]-cash_and_equivalents[t]", "kHKD", "credit", "v1", "est=发行-转换-回购推算，observed 只存一手披露"),
    ("cb_gap_incl_unused_proceeds", "CB缺口(含未动用所得款)", "cb_gap_to_cash-unused_cb_proceeds[t]", "kHKD", "credit", "v1", "动用未动用所得款口径，涉及变更募资用途"),
    ("cb_gap_to_mktcap", "CB缺口/市值", "cb_gap_to_cash/market_cap[t]", "ratio", "credit", "v1", None),
    ("cb_conversion_moneyness", "CB转股价内含价值比", "share_price_close[t]/cb_conversion_price[t]", "ratio", "credit", "v1", "<1 深度价外，理性持有人不转股"),
]

# 阜博集团 2026 中报 observed 事实（千港元，除非另注），全部摘自官方公告比较列。
# (fact_key, period, value[, unit])——period 以 H1 结尾=损益期，否则=资产负债表时点。
FACTS = [
    # ---- 损益类 2026H1 / 2025H1（公告 P1/P11/P13-14/P16/P21-25）----
    ("revenue", "2026H1", 1805455), ("revenue", "2025H1", 1456315),
    ("cost_of_services", "2026H1", 995416), ("cost_of_services", "2025H1", 813585),
    ("gross_profit", "2026H1", 810039), ("gross_profit", "2025H1", 642730),
    ("pretax_profit", "2026H1", 234481), ("pretax_profit", "2025H1", 126872),
    ("net_profit", "2026H1", 195047), ("net_profit", "2025H1", 101242),
    ("net_profit_attributable", "2026H1", 191020), ("net_profit_attributable", "2025H1", 102344),
    ("adj_net_profit", "2026H1", 193660), ("adj_net_profit", "2025H1", 120850),
    ("adj_ebitda", "2026H1", 344367), ("adj_ebitda", "2025H1", 272401),
    ("segment_revenue_subscription", "2026H1", 717878), ("segment_revenue_subscription", "2025H1", 609902),
    ("segment_revenue_value_added", "2026H1", 1087577), ("segment_revenue_value_added", "2025H1", 846413),
    ("geo_revenue_china", "2026H1", 878149), ("geo_revenue_china", "2025H1", 726565),
    ("geo_revenue_us", "2026H1", 862786), ("geo_revenue_us", "2025H1", 725042),
    ("geo_revenue_others", "2026H1", 64520), ("geo_revenue_others", "2025H1", 4708),
    ("sm_expense", "2026H1", 240432), ("sm_expense", "2025H1", 193005),
    ("admin_expense", "2026H1", 127899), ("admin_expense", "2025H1", 112716),
    ("rd_expense", "2026H1", 188645), ("rd_expense", "2025H1", 163441),
    ("finance_cost", "2026H1", 41007), ("finance_cost", "2025H1", 35491),
    ("tax_expense", "2026H1", 39434), ("tax_expense", "2025H1", 25630),
    ("sbc_expense", "2026H1", 4842), ("sbc_expense", "2025H1", 13491),
    ("da_expense", "2026H1", 84250), ("da_expense", "2025H1", 88159),
    ("capex", "2026H1", 371000),
    ("eps_basic", "2026H1", 0.0741, "HKD"), ("eps_basic", "2025H1", 0.0442, "HKD"),
    # ---- 存量类 2026-06-30 / 2025-12-31（公告 P1/P15/P18-19/P26/P29）----
    ("total_assets", "2026-06-30", 6490692), ("total_assets", "2025-12-31", 6262715),
    ("total_liabilities", "2026-06-30", 2626103), ("total_liabilities", "2025-12-31", 2815599),
    ("net_assets", "2026-06-30", 3864589), ("net_assets", "2025-12-31", 3447116),
    ("cash_and_equivalents", "2026-06-30", 896477), ("cash_and_equivalents", "2025-12-31", 1157048),
    ("restricted_deposits", "2026-06-30", 10240),
    ("trade_receivables_net", "2026-06-30", 1914563), ("trade_receivables_net", "2025-12-31", 1753741),
    ("trade_receivables_gross", "2026-06-30", 1962363), ("trade_receivables_gross", "2025-12-31", 1800071),
    ("ecl_allowance", "2026-06-30", 47800), ("ecl_allowance", "2025-12-31", 46330),
    ("goodwill", "2026-06-30", 1342972), ("goodwill", "2025-12-31", 1315908),
    ("intangible_assets", "2026-06-30", 998425), ("intangible_assets", "2025-12-31", 847378),
    ("borrowings_current", "2026-06-30", 401468), ("borrowings_current", "2025-12-31", 342257),
    ("borrowings_non_current", "2026-06-30", 62350), ("borrowings_non_current", "2025-12-31", 58749),
    ("cb_liability_portion", "2026-06-30", 1438427), ("cb_liability_portion", "2025-12-31", 1608554),
    # CB 本金：observed 只存公告一手披露的发行/转换/回购额，未偿本金由 derived 推算（P0-1 修复）
    ("cb_principal_issued", "2025H2", 1600000),
    ("cb_principal_converted", "2025H2", 18000),
    ("cb_principal_converted", "2026H1", 44000),
    ("cb_principal_repurchased", "2026H1", 80000),
    ("cb_repurchase_cash_paid", "2026H1", 80297),
    ("cb_conversion_price", "2026-06-30", 5.87, "HKD"),
    ("unused_cb_proceeds", "2026-06-30", 388000),
    ("ppe", "2026-06-30", 261522), ("ppe", "2025-12-31", 58218),
    ("shares_outstanding", "2026-06-30", 2594310836, "shares"),
    ("exercise_cash_proceeds", "2026H1", 404),
]

# 行情/估值（第三方，G1：独立 market_quotes 表，不进 observed 层）
# (metric, quote_date, value[, unit])
QUOTES = [
    ("share_price_close", "2026-08-28", 2.80, "HKD"),
    ("market_cap", "2026-08-28", 7260000, "kHKD"),
    ("pe_ttm", "2026-08-28", 24.77, "ratio"),
    ("pb_lf", "2026-08-28", 2.27, "ratio"),
]

SOURCES = [
    (HKEX_SRC, "HKEX 联交所公告", "official", "官方一手：中期业绩公告全文 PDF"),
    (QUOTE_SRC, "腾讯行情/AASTOCKS", "third_party", "第三方行情快照，仅用于估值类因子分母"),
]


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def backfill(db: Path, batch: str) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    now = _now()

    # 同一批次(collected_at)重跑先清除，保证幂等；跨批次只增不修
    for t in ("company_facts", "market_quotes", "derived_factors"):
        con.execute(f"DELETE FROM {t} WHERE collected_at=?", (batch,))

    for row in FACTS:
        key, period, value = row[0], row[1], row[2]
        unit = row[3] if len(row) > 3 else "kHKD"
        is_flow = period.endswith("H1") or period.endswith("H2")
        freq = "H" if is_flow else "H"  # 损益期与报告快照同属半年度报告；时点类以 period 日期区分
        con.execute(
            "INSERT OR IGNORE INTO company_facts VALUES (?,?,?,?,?,?,?,?,?,?)",
            (TICKER, key, period, freq, value, unit, HKEX_SRC, HKEX_URL, "2026-08-28", batch),
        )

    for row in QUOTES:
        metric, qdate, value = row[0], row[1], row[2]
        unit = row[3] if len(row) > 3 else "kHKD"
        con.execute("INSERT OR IGNORE INTO market_quotes VALUES (?,?,?,?,?,?,?)",
                    (TICKER, metric, qdate, value, unit, QUOTE_SRC, batch))

    for key, desc, formula, unit, layer, ver, note in REGISTRY:
        con.execute("INSERT OR REPLACE INTO factor_registry VALUES (?,?,?,?,?,?,?)",
                    (key, desc, formula, unit, layer, ver, note))
    for row in SOURCES:
        con.execute("INSERT OR REPLACE INTO source_trust VALUES (?,?,?,?)", row)

    f = {k: v for k, v in con.execute(
        "SELECT fact_key || '@' || period, value FROM company_facts WHERE ticker=?", (TICKER,))}
    q = {k: v for k, v in con.execute(
        "SELECT metric || '@' || quote_date, value FROM market_quotes WHERE ticker=?", (TICKER,))}

    def r(key, period):
        return f[f"{key}@{period}"]

    def rq(metric, qdate):
        return q[f"{metric}@{qdate}"]

    # derived 之间的引用用局部变量（est 不在 company_facts，不能经 r() 查）
    cb_outstanding_2025 = r("cb_principal_issued", "2025H2") - r("cb_principal_converted", "2025H2")
    cb_outstanding_2026 = (cb_outstanding_2025
                           - r("cb_principal_converted", "2026H1") - r("cb_principal_repurchased", "2026H1"))
    delta_cash = r("cash_and_equivalents", "2026-06-30") - r("cash_and_equivalents", "2025-12-31")
    borrowings_net_inc = ((r("borrowings_current", "2026-06-30") + r("borrowings_non_current", "2026-06-30"))
                          - (r("borrowings_current", "2025-12-31") + r("borrowings_non_current", "2025-12-31")))
    # 残差口径：利息已付取确定的租赁名义利息 460（下限口径，note 声明噪声）
    ocf_residual = (delta_cash + r("capex", "2026H1") + r("cb_repurchase_cash_paid", "2026H1")
                    - borrowings_net_inc + 460 - r("exercise_cash_proceeds", "2026H1"))

    D = [
        ("revenue_yoy", "2026H1", r("revenue", "2026H1") / r("revenue", "2025H1") - 1),
        ("gross_margin", "2026H1", r("gross_profit", "2026H1") / r("revenue", "2026H1")),
        ("gross_margin_chg", "2026H1",
         (r("gross_profit", "2026H1") / r("revenue", "2026H1")
          - r("gross_profit", "2025H1") / r("revenue", "2025H1")) * 100),
        ("gross_profit_check", "2026H1",
         r("gross_profit", "2026H1") - (r("revenue", "2026H1") - r("cost_of_services", "2026H1"))),
        ("net_profit_yoy", "2026H1", r("net_profit", "2026H1") / r("net_profit", "2025H1") - 1),
        ("adj_net_profit_yoy", "2026H1", r("adj_net_profit", "2026H1") / r("adj_net_profit", "2025H1") - 1),
        ("adj_ebitda_yoy", "2026H1", r("adj_ebitda", "2026H1") / r("adj_ebitda", "2025H1") - 1),
        ("receivables_to_revenue", "2026H1", r("trade_receivables_net", "2026-06-30") / r("revenue", "2026H1")),
        ("dso_days", "2026H1", r("trade_receivables_net", "2026-06-30") / r("revenue", "2026H1") * 182),
        ("ecl_ratio", "2026H1", r("ecl_allowance", "2026-06-30") / r("trade_receivables_gross", "2026-06-30")),
        ("ocf_proxy_upper", "2026H1",
         r("net_profit", "2026H1") + r("da_expense", "2026H1") + r("sbc_expense", "2026H1")),
        ("fcf_proxy_upper", "2026H1",
         r("net_profit", "2026H1") + r("da_expense", "2026H1") + r("sbc_expense", "2026H1") - r("capex", "2026H1")),
        ("ocf_proxy_residual", "2026H1", ocf_residual),
        ("fcf_proxy_residual", "2026H1", ocf_residual - r("capex", "2026H1")),
        ("cb_principal_outstanding_est", "2025-12-31", cb_outstanding_2025),
        ("cb_principal_outstanding_est", "2026-06-30", cb_outstanding_2026),
        ("cb_gap_to_cash", "2026H1", cb_outstanding_2026 - r("cash_and_equivalents", "2026-06-30")),
        ("cb_gap_incl_unused_proceeds", "2026H1",
         cb_outstanding_2026 - r("cash_and_equivalents", "2026-06-30")
         - r("unused_cb_proceeds", "2026-06-30")),
        ("cb_gap_to_mktcap", "2026-08-28",
         (cb_outstanding_2026 - r("cash_and_equivalents", "2026-06-30"))
         / rq("market_cap", "2026-08-28")),
        ("cb_conversion_moneyness", "2026-08-28",
         rq("share_price_close", "2026-08-28") / r("cb_conversion_price", "2026-06-30")),
    ]
    units = {"dso_days": "days", "gross_margin_chg": "pct", "gross_profit_check": "kHKD",
             "ocf_proxy_upper": "kHKD", "fcf_proxy_upper": "kHKD", "ocf_proxy_residual": "kHKD",
             "fcf_proxy_residual": "kHKD", "cb_gap_to_cash": "kHKD",
             "cb_gap_incl_unused_proceeds": "kHKD", "cb_principal_outstanding_est": "kHKD"}
    formula_of = {k: formula for k, _, formula, *_ in REGISTRY}
    for key, period, value in D:
        con.execute(
            "INSERT OR IGNORE INTO derived_factors VALUES (?,?,?,?,?,?,'v1','derived',?)",
            (TICKER, key, period, round(value, 6), units.get(key, "ratio"), formula_of[key], batch))
    con.commit()

    n_facts = con.execute("SELECT COUNT(*) FROM company_facts WHERE collected_at=?", (batch,)).fetchone()[0]
    n_quotes = con.execute("SELECT COUNT(*) FROM market_quotes WHERE collected_at=?", (batch,)).fetchone()[0]
    n_derived = con.execute("SELECT COUNT(*) FROM derived_factors WHERE collected_at=?", (batch,)).fetchone()[0]
    print(f"[backfill] batch={batch} facts={n_facts} quotes={n_quotes} derived={n_derived} db={db}")
    for key, period, value in D:
        print(f"  {key:34s} {period:11s} {value:14,.4f}")
    con.close()


def query(db: Path, ticker: str) -> None:
    con = sqlite3.connect(db)
    print(f"== company_facts ({ticker}) 最新批次:")
    for row in con.execute(
        "SELECT fact_key, period, value, unit, source FROM company_facts "
        "WHERE ticker=? AND collected_at=(SELECT MAX(collected_at) FROM company_facts WHERE ticker=?) "
        "ORDER BY fact_key, period", (ticker, ticker)):
        print(f"  {row[0]:34s} {row[1]:11s} {row[2]:>16,.2f} {row[3]:6s} {row[4]}")
    print("== market_quotes:")
    for row in con.execute(
        "SELECT metric, quote_date, value, unit, source FROM market_quotes WHERE ticker=? "
        "ORDER BY metric, quote_date", (ticker,)):
        print(f"  {row[0]:34s} {row[1]:11s} {row[2]:>16,.2f} {row[3]:6s} {row[4]}")
    print("== derived_factors:")
    for row in con.execute(
        "SELECT factor_key, period, value, unit FROM derived_factors WHERE ticker=? "
        "ORDER BY period, factor_key", (ticker,)):
        print(f"  {row[0]:34s} {row[1]:11s} {row[2]:14,.4f} {row[3]}")
    con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--ticker", default=TICKER)
    ap.add_argument("cmd", choices=["backfill", "query"])
    args = ap.parse_args()
    if args.cmd == "backfill":
        backfill(Path(args.db), batch=dt.date.today().isoformat())
    else:
        query(Path(args.db), args.ticker)


if __name__ == "__main__":
    main()
