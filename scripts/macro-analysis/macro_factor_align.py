#!/usr/bin/env python3
"""Macro factor alignment: deterministic bridge from macro MCP to equity quant DB.

路线 B 的确定性桥梁——不用 agent，纯代码：
  宏观指标（月度） → 对齐规则 → 个股因子面板（日度）

设计原则：
  - 宏观指标是"一类因子"，与估值/质量因子平级；
  - 对齐规则是确定性代码（发布时点对齐，防前视），不是 LLM 判断；
  - 直接读 macro_clean.sqlite（不经过 MCP 调用，避免 agent 开销）；
  - 写入 equity_fundamental.sqlite 的 derived_factors 表（source='macro_aligned'）。

CLI:
  align                           对齐所有可用宏观指标
  align --indicator PPI_CHN       对齐单个指标
  list                            列出可对齐的宏观指标
  query --indicator PPI_CHN       查询对齐后的因子值
  self-test                       已知答案自测
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import sqlite3
from pathlib import Path

EQUITY_DB = Path(__file__).resolve().parents[4] / "outputs" / "equity_fundamental.sqlite"
MACRO_DB = Path(__file__).resolve().parents[4] / "outputs" / "macro_clean.sqlite"
TRADING_DAYS = 252

# 宏观指标 → equity factor 映射
# 对齐规则：宏观月度值在次月第一个交易日起生效（发布时点对齐，防前视）
# indicator/country 须与 macro_clean.sqlite 中的名称完全一致
MACRO_FACTOR_MAP = {
    # (indicator, country, factor_key, description, unit, transform)
    ("ppi_yoy", "CN", "ppi_yoy", "中国PPI同比(%)", "pct", "raw"),
    ("manufacturing_pmi", "CN", "pmi_mfg", "中国制造业PMI", "index", "raw"),
    ("nonmanufacturing_pmi", "CN", "pmi_non_mfg", "中国非制造业PMI", "index", "raw"),
    ("cpi_yoy", "CN", "cpi_yoy", "中国CPI同比(%)", "pct", "raw"),
    ("nonfarm_payroll_change", "US", "nfp_change", "美国非农就业变化(千人)", "k", "raw"),
    ("unemployment_rate", "US", "unemployment_rate", "美国失业率(%)", "pct", "raw"),
    ("gdp_qoq", "CN", "gdp_qoq", "中国GDP环比(%)", "pct", "raw"),
    ("composite_pmi", "CN", "composite_pmi", "中国综合PMI", "index", "raw"),
    ("m1_yoy", "CN", "m1_yoy", "中国M1同比(%)", "pct", "raw"),
    ("m2_yoy", "CN", "m2_yoy", "中国M2同比(%)", "pct", "raw"),
}


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def list_available(macro_db: Path) -> list[dict]:
    """列出 macro_clean.sqlite 中可对齐的宏观指标。"""
    if not macro_db.exists():
        return []
    con = sqlite3.connect(macro_db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT indicator, country, label, unit, frequency, last_period, n_obs "
        "FROM indicators ORDER BY country, indicator").fetchall()
    con.close()
    # 只返回有映射的指标
    mapped = {(m[0], m[1]) for m in MACRO_FACTOR_MAP}
    return [dict(r) for r in rows if (r["indicator"], r["country"]) in mapped]


def align_macro_factors(equity_db: Path, macro_db: Path,
                        indicator: str | None = None) -> int:
    """从 macro_clean.sqlite 读取宏观时序，对齐写入 equity DB 的 derived_factors。

    对齐规则：
    - 宏观月度值在次月1日起生效（假设月中发布）
    - 例：2026-06 PPI → 生效日 2026-07-01
    - 对所有标的统一写入（宏观因子是系统性因子，不因个股而异）

    返回写入的因子数。
    """
    if not macro_db.exists():
        print(f"[macro-align] macro DB not found: {macro_db}")
        return 0

    equity_con = sqlite3.connect(equity_db)
    equity_con.executescript("""
    CREATE TABLE IF NOT EXISTS derived_factors (
        ticker TEXT NOT NULL, factor_key TEXT NOT NULL, period TEXT NOT NULL,
        value REAL NOT NULL, unit TEXT NOT NULL, transform TEXT NOT NULL,
        transform_version TEXT NOT NULL DEFAULT 'v1',
        source TEXT NOT NULL DEFAULT 'derived', collected_at TEXT NOT NULL,
        PRIMARY KEY (ticker, factor_key, period, collected_at));
    """)

    # 获取所有标的
    tickers = [r[0] for r in equity_con.execute(
        "SELECT DISTINCT ticker FROM daily_quotes ORDER BY ticker").fetchall()]
    if not tickers:
        print("[macro-align] no tickers in equity DB")
        equity_con.close()
        return 0

    macro_con = sqlite3.connect(macro_db)
    macro_con.row_factory = sqlite3.Row
    # batch 用天级日期（与 ingest/fundamental 一致）：同日重跑幂等。
    # 不能用 _now() 时间戳——否则每次重跑 collected_at 都不同，INSERT OR REPLACE
    # 失效，累积冗余批次（实测已产生 3 批次、6500 重复组合）。
    batch = dt.date.today().isoformat()
    total = 0

    for m_ind, m_country, factor_key, desc, unit, transform in MACRO_FACTOR_MAP:
        if indicator and m_ind != indicator:
            continue

        # 读取宏观时序
        rows = macro_con.execute(
            "SELECT period, value FROM clean_series "
            "WHERE indicator=? AND country=? AND value IS NOT NULL "
            "ORDER BY period", (m_ind, m_country)).fetchall()
        if not rows:
            continue

        inserts = []
        for row in rows:
            period = row["period"]
            value = row["value"]
            if value is None:
                continue
            # 月度值 → 次月1日生效
            try:
                parts = period.split("-")
                yr, mo = int(parts[0]), int(parts[1])
                if mo == 12:
                    eff_date = f"{yr + 1}-01-01"
                else:
                    eff_date = f"{yr}-{mo + 1:02d}-01"
            except (ValueError, IndexError):
                continue  # 非月度格式跳过

            # 写入所有标的（系统性因子）
            for tk in tickers:
                inserts.append((
                    tk, factor_key, eff_date, round(value, 6), unit,
                    transform, "v1", "macro_aligned", batch
                ))

        if inserts:
            equity_con.executemany(
                "INSERT OR REPLACE INTO derived_factors VALUES (?,?,?,?,?,?,?,?,?)",
                inserts)
            total += len(inserts)
            print(f"[macro-align] {m_ind}/{m_country}: {len(rows)} periods × "
                  f"{len(tickers)} tickers = {len(inserts)} factor rows")

    equity_con.commit()
    equity_con.close()
    macro_con.close()
    print(f"[macro-align] total: {total} factor rows written")
    return total


def query_aligned(equity_db: Path, indicator: str | None = None,
                  ticker: str | None = None) -> list[dict]:
    """查询对齐后的宏观因子值。"""
    con = sqlite3.connect(equity_db)
    con.row_factory = sqlite3.Row
    sql = ("SELECT ticker, factor_key, period, value, unit FROM derived_factors "
           "WHERE source='macro_aligned'")
    params: list = []
    if indicator:
        # 找到对应的 factor_key
        fk = None
        for m in MACRO_FACTOR_MAP:
            if m[0] == indicator:
                fk = m[2]
                break
        if fk:
            sql += " AND factor_key=?"
            params.append(fk)
    if ticker:
        sql += " AND ticker=?"
        params.append(ticker)
    sql += " ORDER BY ticker, factor_key, period"
    rows = con.execute(sql, params).fetchall()
    con.close()
    return [dict(r) for r in rows]


# ============================================================ Self Test ===


def self_test() -> bool:
    """已知答案校验。"""
    import tempfile
    results: list[tuple[str, bool, str]] = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        eq_tdb = Path(f.name)
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        mc_tdb = Path(f.name)

    try:
        # 创建模拟 macro_clean.sqlite
        con = sqlite3.connect(mc_tdb)
        con.executescript("""
        CREATE TABLE IF NOT EXISTS indicators (
            indicator TEXT, country TEXT, label TEXT, unit TEXT,
            frequency TEXT, sa_method TEXT, first_period TEXT,
            last_period TEXT, n_obs INTEGER, n_imputed INTEGER,
            last_updated TEXT, PRIMARY KEY (indicator, country));
        CREATE TABLE IF NOT EXISTS clean_series (
            indicator TEXT, country TEXT, period TEXT, value REAL,
            value_sa REAL, value_imputed REAL, is_imputed INTEGER,
            source TEXT, release_date TEXT, collected_at TEXT,
            layer TEXT, derived_from TEXT, transform TEXT,
            transform_version TEXT, computed_at TEXT,
            PRIMARY KEY (indicator, country, period));
        """)
        con.execute("INSERT INTO indicators VALUES ('ppi_yoy','CN','PPI','%','monthly','', '2025-01','2026-06',18,0,'2026-07')")
        # 18 个月的数据
        for m in range(1, 19):
            yr = 2025 if m <= 12 else 2026
            mo = m if m <= 12 else m - 12
            period = f"{yr}-{mo:02d}"
            con.execute("INSERT INTO clean_series VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("ppi_yoy", "CN", period, -2.0 + m * 0.3, None, None, 0, "nbs", None, _now(), "observed", None, None, None, None))
        con.commit()
        con.close()

        # 创建模拟 equity DB（含 daily_quotes 和1只股票）
        con = sqlite3.connect(eq_tdb)
        con.executescript("""
        CREATE TABLE IF NOT EXISTS daily_quotes (
            ticker TEXT, quote_date TEXT, open REAL, high REAL, low REAL,
            close REAL, volume REAL, amount REAL, currency TEXT,
            source TEXT, collected_at TEXT,
            PRIMARY KEY (ticker, quote_date, collected_at, source));
        CREATE TABLE IF NOT EXISTS derived_factors (
            ticker TEXT, factor_key TEXT, period TEXT, value REAL,
            unit TEXT, transform TEXT, transform_version TEXT,
            source TEXT, collected_at TEXT,
            PRIMARY KEY (ticker, factor_key, period, collected_at));
        """)
        con.execute("INSERT INTO daily_quotes VALUES ('hk00001','2026-01-02',100,101,99,100,1e6,5e7,'HKD','test','2026-01-01')")
        con.execute("INSERT INTO daily_quotes VALUES ('hk00001','2026-07-02',110,111,109,110,1e6,5e7,'HKD','test','2026-07-01')")
        con.commit()
        con.close()

        # 1) 对齐
        n = align_macro_factors(eq_tdb, mc_tdb, "ppi_yoy")
        check("aligned ppi_yoy for 1 ticker", n > 0, f"n={n}")

        # 2) 验证生效日：2025-01 PPI → 生效 2025-02-01
        con = sqlite3.connect(eq_tdb)
        r = con.execute(
            "SELECT value FROM derived_factors WHERE ticker='hk00001' "
            "AND factor_key='ppi_yoy' AND period='2025-02-01'"
        ).fetchone()
        check("PPI 2025-01 aligned to 2025-02-01", r is not None and abs(r[0] - (-2.0 + 1 * 0.3)) < 0.01,
              f"value={r[0] if r else 'None'} expected={-2.0 + 0.3}")

        # 3) 幂等
        n2 = align_macro_factors(eq_tdb, mc_tdb, "ppi_yoy")
        cnt = con.execute("SELECT COUNT(*) FROM derived_factors WHERE source='macro_aligned'").fetchone()[0]
        con.close()
        check("idempotent", cnt == 18, f"count={cnt}")  # 18 periods × 1 ticker

    finally:
        try:
            eq_tdb.unlink(missing_ok=True)
        except PermissionError:
            pass
        try:
            mc_tdb.unlink(missing_ok=True)
        except PermissionError:
            pass

    ok = all(c for _, c, _ in results)
    for name, passed, detail in results:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}  {detail}")
    print(f"[self-test] {'ALL PASS' if ok else 'FAILED'} ({sum(c for _, c, _ in results)}/{len(results)})")
    return ok


# ============================================================ CLI ===


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--equity-db", default=str(EQUITY_DB))
    ap.add_argument("--macro-db", default=str(MACRO_DB))
    ap.add_argument("--indicator", default=None)
    ap.add_argument("--ticker", default=None)
    ap.add_argument("cmd", choices=["align", "list", "query", "self-test"])
    args = ap.parse_args()

    eq_db = Path(args.equity_db)
    mc_db = Path(args.macro_db)

    if args.cmd == "align":
        align_macro_factors(eq_db, mc_db, args.indicator)
    elif args.cmd == "list":
        for r in list_available(mc_db):
            print(f"  {r['indicator']:20s} {r['country']:4s} {r['label']:30s} "
                  f"freq={r['frequency']} last={r['last_period']} n={r['n_obs']}")
    elif args.cmd == "query":
        for r in query_aligned(eq_db, args.indicator, args.ticker):
            print(f"  {r['ticker']:10s} {r['factor_key']:20s} {r['period']:12s} "
                  f"{r['value']:10.4f} {r['unit']}")
    elif args.cmd == "self-test":
        raise SystemExit(0 if self_test() else 1)


if __name__ == "__main__":
    main()
