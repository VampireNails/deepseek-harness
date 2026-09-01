#!/usr/bin/env python3
"""Equity data model: panel view, forward returns, benchmarks, sector mapping.

路线 B（真量化）的数据模型地基。在现有 equity_fundamental.sqlite 之上扩展：
  1. panel 视图：ticker × trade_date × factor（横截面分析的统一入口）
  2. forward_returns 表：未来 N 日收益标签（因子验证的前提）
  3. benchmarks 表：基准指数日线（恒指等）
  4. sector_map 表：行业分类（中性化所需）

设计约束：
- 扩展现有数据库，不新建独立库（共享 vintage 哲学）；
- forward_returns 按交易日预计算（查询时零成本）；
- 基准数据来源与个股行情同一接口（腾讯 ifzq）；
- sector_map 允许手动标注或后续 API 自动填充；
- 所有新增表遵循 append-only（同日重跑幂等）。

CLI:
  init                    建表 + 建视图（幂等）
  forward-returns         从 daily_quotes 计算前向收益
  benchmark --index HSI   下载基准指数日线
  panel --ticker hk03738  查询面板数据（近 N 日）
  universe                列出所有标的及行业
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sqlite3
import urllib.request
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[4] / "outputs" / "equity_fundamental.sqlite"
TRADING_DAYS = 252

# ============================================================ Schema Extensions ===

SCHEMA_NEW = """
-- 前向收益标签（量化因子验证的前提）
CREATE TABLE IF NOT EXISTS forward_returns (
    ticker       TEXT NOT NULL,
    trade_date   TEXT NOT NULL,
    fwd_1d       REAL,   -- next 1 trading day return
    fwd_5d       REAL,   -- next 5 trading days
    fwd_20d      REAL,   -- next 20 trading days (~1 month)
    fwd_60d      REAL,   -- next 60 trading days (~1 quarter)
    PRIMARY KEY (ticker, trade_date)
);

-- 基准指数日线
CREATE TABLE IF NOT EXISTS benchmarks (
    index_code   TEXT NOT NULL,
    trade_date   TEXT NOT NULL,
    open         REAL,
    high         REAL,
    low          REAL,
    close        REAL,
    volume       REAL,
    currency     TEXT NOT NULL DEFAULT 'HKD',
    source       TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (index_code, trade_date)
);

-- 行业分类（中性化所需）
CREATE TABLE IF NOT EXISTS sector_map (
    ticker       TEXT NOT NULL PRIMARY KEY,
    sector       TEXT NOT NULL,
    industry     TEXT,
    source       TEXT NOT NULL DEFAULT 'manual',
    collected_at TEXT NOT NULL
);

-- 币种汇率快照（标准化到基准币种）
CREATE TABLE IF NOT EXISTS fx_rates (
    pair         TEXT NOT NULL,   -- e.g. 'USDHKD', 'CNYHKD'
    rate_date    TEXT NOT NULL,
    rate         REAL NOT NULL,
    source       TEXT NOT NULL DEFAULT 'manual',
    PRIMARY KEY (pair, rate_date)
);

-- 标的池配置
CREATE TABLE IF NOT EXISTS universe (
    ticker       TEXT NOT NULL PRIMARY KEY,
    name_zh      TEXT,
    name_en      TEXT,
    sector       TEXT,
    currency     TEXT NOT NULL DEFAULT 'kHKD',
    fiscal_year_end TEXT NOT NULL DEFAULT '12',  -- 月份，如 '12'=12月, '3'=3月
    included     INTEGER NOT NULL DEFAULT 1,
    reason       TEXT,
    collected_at TEXT NOT NULL
);
"""

# 面板视图：统一的 ticker × trade_date × factor 入口
PANEL_VIEW = """
CREATE VIEW IF NOT EXISTS v_panel AS
SELECT
    q.ticker,
    q.trade_date,
    q.close,
    q.open,
    q.high,
    q.low,
    q.volume,
    COALESCE(s.sector, 'unknown') AS sector,
    -- 市值代理（若 close × shares 可得）
    q.close AS _close_for_mcap
FROM (
    SELECT ticker, quote_date AS trade_date, close, open, high, low, volume
    FROM daily_quotes
) q
LEFT JOIN sector_map s ON q.ticker = s.ticker
ORDER BY q.ticker, q.trade_date;
"""


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db(db: Path) -> None:
    """建表 + 建视图（幂等）。"""
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_NEW)
    con.executescript(PANEL_VIEW)
    con.commit()
    # 验证
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('forward_returns','benchmarks','sector_map','fx_rates','universe')")]
    views = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='view' AND name='v_panel'")]
    con.close()
    print(f"[init] tables: {tables}")
    print(f"[init] views: {views}")
    missing = set(['forward_returns', 'benchmarks', 'sector_map', 'fx_rates', 'universe']) - set(tables)
    if missing or 'v_panel' not in views:
        print(f"[init] WARNING: missing {missing}, view={'OK' if 'v_panel' in views else 'MISSING'}")
    else:
        print("[init] ALL OK")


# ============================================================ Price Factors ===


def compute_price_factors(db: Path, ticker: str | None = None) -> int:
    """从 daily_quotes 计算价格因子（动量/波动率/量比），写入 derived_factors。

    不需要公告数据，所有有行情的标的均可计算。这是最快速的跨截面因子来源。
    返回写入的标的数。
    """
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_NEW)
    tickers = [ticker] if ticker else [r[0] for r in con.execute(
        "SELECT DISTINCT ticker FROM daily_quotes ORDER BY ticker")]
    # batch 用天级日期（幂等）：不能用 _now() 时间戳，否则重跑累积冗余批次
    # （与 macro_factor_align 同类 bug）。
    batch = dt.date.today().isoformat()
    total = 0

    FACTORS = [
        "momentum_20d", "momentum_60d", "volatility_20d",
        "volume_ratio_20d", "price_to_ma20",
    ]

    for tk in tickers:
        rows = con.execute(
            "SELECT quote_date, close, volume FROM daily_quotes "
            "WHERE ticker=? AND close > 0 ORDER BY quote_date", (tk,)).fetchall()
        if len(rows) < 21:
            continue
        dates = [r[0] for r in rows]
        closes = [r[1] for r in rows]
        volumes = [r[2] or 0 for r in rows]

        # 按因子分组收集 (period, value)
        by_factor: dict[str, list[tuple[str, float]]] = {f: [] for f in FACTORS}
        for i in range(20, len(dates)):
            d = dates[i]
            c = closes[i]

            # momentum_20d
            if closes[i - 20] > 0:
                by_factor["momentum_20d"].append((d, c / closes[i - 20] - 1))

            # momentum_60d
            if i >= 60 and closes[i - 60] > 0:
                by_factor["momentum_60d"].append((d, c / closes[i - 60] - 1))

            # volatility_20d
            rets = [(closes[j] / closes[j - 1] - 1)
                    for j in range(i - 19, i + 1) if closes[j - 1] > 0]
            if len(rets) >= 10:
                mean_r = sum(rets) / len(rets)
                var_r = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)
                by_factor["volatility_20d"].append((d, (var_r ** 0.5) * (252 ** 0.5)))

            # volume_ratio_20d
            if i >= 40:
                recent_vol = sum(volumes[i - 19:i + 1]) / 20
                prev_vol = sum(volumes[i - 39:i - 19]) / 20
                if prev_vol > 0:
                    by_factor["volume_ratio_20d"].append((d, recent_vol / prev_vol))

            # price_to_ma20
            ma20 = sum(closes[i - 19:i + 1]) / 20
            if ma20 > 0:
                by_factor["price_to_ma20"].append((d, c / ma20 - 1))

        # 批量写入
        rows_written = 0
        for fk, data in by_factor.items():
            if not data:
                continue
            con.executemany(
                "INSERT OR REPLACE INTO derived_factors "
                "(ticker, factor_key, period, value, unit, transform, "
                "transform_version, source, collected_at) VALUES (?,?,?,?,?,?,?,?,?)",
                [(tk, fk, d, round(v, 8), "ratio", "rolling", "v1",
                  "price_computed", batch) for d, v in data])
            rows_written += len(data)
        con.commit()
        total += 1
        print(f"[price-factors] {tk}: {rows_written} rows across {len([f for f in FACTORS if by_factor[f]])} factors")

    con.close()
    return total


# ============================================================ Forward Returns ===


def compute_forward_returns(db: Path, ticker: str | None = None) -> int:
    """从 daily_quotes 计算前向收益标签，写入 forward_returns。

    使用交易日序列的滚动查找，确保零未来信息泄露。
    返回处理的标的数。
    """
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_NEW)
    tickers = [ticker] if ticker else [r[0] for r in con.execute(
        "SELECT DISTINCT ticker FROM daily_quotes ORDER BY ticker")]
    batch = dt.date.today().isoformat()
    total_rows = 0

    for tk in tickers:
        # 获取有序交易日和收盘价
        rows = con.execute(
            "SELECT quote_date, close FROM daily_quotes WHERE ticker=? "
            "AND close > 0 ORDER BY quote_date", (tk,)).fetchall()
        if len(rows) < 2:
            continue
        dates = [r[0] for r in rows]
        closes = [r[1] for r in rows]
        date_idx = {d: i for i, d in enumerate(dates)}

        # 先删除当天批次（幂等）
        con.execute("DELETE FROM forward_returns WHERE trade_date >= ? "
                     "AND trade_date <= ? AND ticker=?",
                     (dates[0], dates[-1], tk))

        inserts = []
        for i, (d, c) in enumerate(zip(dates, closes)):
            fwd = {}
            for horizon, col in [(1, 'fwd_1d'), (5, 'fwd_5d'), (20, 'fwd_20d'), (60, 'fwd_60d')]:
                j = i + horizon
                fwd[col] = (closes[j] / c - 1) if j < len(closes) else None
            inserts.append((tk, d, fwd['fwd_1d'], fwd['fwd_5d'], fwd['fwd_20d'], fwd['fwd_60d']))

        con.executemany(
            "INSERT OR REPLACE INTO forward_returns VALUES (?,?,?,?,?,?)", inserts)
        total_rows += len(inserts)
        print(f"[fwd-ret] {tk}: {len(inserts)} rows ({dates[0]}..{dates[-1]})")

    con.commit()
    con.close()
    return len(tickers)


# ============================================================ Market Cap ===

QUOTE_URL = "https://qt.gtimg.cn/q={symbol}"


def fetch_market_cap(db: Path, ticker: str | None = None) -> int:
    """采集总市值与推总股本（腾讯 qt 接口），用于市值中性化。

    数据源：qt.gtimg.cn 实时行情，idx44 = 总市值（亿港元）。
    实测反推股本与真实股本吻合（腾讯 91.03 亿股 / 建行 2404 亿股 / 中芯 60.14 亿股）。

    股本 = 市值 / 股价，据此可算**时变市值** = 历史收盘价 × 股本
    （股本假设稳定，忽略增发/回购的影响）。

    存储：universe.shares_outstanding（股本，股）+ market_cap 表（当前时点市值快照）。
    返回成功数。
    """
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_NEW)
    con.execute("""CREATE TABLE IF NOT EXISTS market_cap (
        ticker TEXT NOT NULL, quote_date TEXT NOT NULL,
        market_cap REAL, shares_outstanding REAL, currency TEXT NOT NULL,
        source TEXT NOT NULL, collected_at TEXT NOT NULL,
        PRIMARY KEY (ticker, quote_date, collected_at, source))""")
    # universe 表加 shares_outstanding 列（向后兼容已存在的表）
    cols = {r[1] for r in con.execute("PRAGMA table_info(universe)")}
    if "shares_outstanding" not in cols:
        con.execute("ALTER TABLE universe ADD COLUMN shares_outstanding REAL")
    tickers = [ticker] if ticker else [r[0] for r in con.execute(
        "SELECT ticker FROM universe ORDER BY ticker")]
    batch = dt.date.today().isoformat()
    ok = 0
    for tk in tickers:
        try:
            req = urllib.request.Request(QUOTE_URL.format(symbol=tk),
                                         headers={"User-Agent": UA})
            raw = urllib.request.urlopen(req, timeout=20).read().decode("gbk", "ignore")
            parts = raw.split('="')[-1].rstrip('";\n').split("~")
            price = _as_number(parts[3])
            mcap_yi = _as_number(parts[44])  # 总市值（亿港元）
            if not price or not mcap_yi or price <= 0:
                continue
            market_cap = mcap_yi * 1e8          # 亿 -> 元
            shares = market_cap / price          # 推总股本（股）
            qdate = (parts[30] or "").split(" ")[0].replace("/", "-") or batch
            con.execute("DELETE FROM market_cap WHERE ticker=? AND quote_date=? AND source=?",
                        (tk, qdate, "tencent_qt"))
            con.execute("INSERT OR REPLACE INTO market_cap VALUES (?,?,?,?,?,?,?)",
                        (tk, qdate, market_cap, shares, "HKD", "tencent_qt", batch))
            con.execute("UPDATE universe SET shares_outstanding=? WHERE ticker=?", (shares, tk))
            ok += 1
            print(f"[mcap] {tk}: 市值={mcap_yi:,.2f}亿港元  股本={shares/1e8:,.2f}亿股")
        except Exception as e:
            print(f"[mcap] {tk}: FAILED ({e})")
    con.commit()
    con.close()
    return ok


# ============================================================ Benchmarks ===

# 腾讯日K接口 index 映射
INDEX_SYMBOLS = {
    "HSI":  "hkHSI",      # 恒生指数
    "HSTECH": "hkHSTECH", # 恒生科技
    "HSCEI": "hkHSCEI",   # 恒生中国企业
    "SSE": "sh000001",     # 上证指数
    "SZSE": "sz399001",    # 深证成指
}

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{count},qfq"
UA = "equity-data-model/1.0"


def _as_number(x) -> float | None:
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str) and x.strip():
        try:
            return float(x)
        except ValueError:
            return None
    return None


def download_benchmark(db: Path, index_code: str = "HSI", count: int = 500) -> int:
    """下载基准指数日线，写入 benchmarks 表。返回行数。"""
    symbol = INDEX_SYMBOLS.get(index_code)
    if not symbol:
        raise ValueError(f"Unknown index: {index_code}; known: {list(INDEX_SYMBOLS)}")

    url = KLINE_URL.format(symbol=symbol, count=count)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    data = payload.get("data", {}).get(symbol, {})
    rows = data.get("qfqday") or data.get("day") or []
    if not rows:
        print(f"[benchmark] {index_code}: 0 rows (source check FAILED)")
        return 0

    con = sqlite3.connect(db)
    con.executescript(SCHEMA_NEW)
    batch = _now()
    inserts = []
    for row in rows:
        if len(row) < 6:
            continue
        inserts.append((
            index_code, row[0],
            float(row[1]), float(row[3]), float(row[4]), float(row[2]),
            _as_number(row[5]), "HKD", f"tencent_ifzq:{symbol}", batch
        ))
    con.executemany(
        "INSERT OR REPLACE INTO benchmarks VALUES (?,?,?,?,?,?,?,?,?,?)", inserts)
    con.commit()
    con.close()
    first, last = inserts[0][1], inserts[-1][1]
    print(f"[benchmark] {index_code}: {len(inserts)} bars ({first}..{last})")
    return len(inserts)


# ============================================================ Sector Mapping ===


def set_sector(db: Path, ticker: str, sector: str, industry: str = "") -> None:
    """手动设置标的行业分类。"""
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_NEW)
    con.execute("INSERT OR REPLACE INTO sector_map VALUES (?,?,?, 'manual', ?)",
                (ticker, sector, industry, _now()))
    con.commit()
    con.close()
    print(f"[sector] {ticker} -> sector={sector} industry={industry}")


def set_universe(db: Path, ticker: str, name_zh: str = "", name_en: str = "",
                 sector: str = "", currency: str = "kHKD",
                 fiscal_year_end: str = "12", included: bool = True,
                 reason: str = "") -> None:
    """设置标的池条目（UPSERT：更新时保留 shares_outstanding，不整行覆盖）。"""
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_NEW)
    # 显式列名 + ON CONFLICT 更新：表含 shares_outstanding 列（第 10 列），
    # 整表 VALUES 插入会列数失配，且 REPLACE 会抹掉已有股本数据。
    con.execute(
        "INSERT INTO universe "
        "(ticker,name_zh,name_en,sector,currency,fiscal_year_end,included,reason,collected_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(ticker) DO UPDATE SET "
        "name_zh=excluded.name_zh, name_en=excluded.name_en, sector=excluded.sector, "
        "currency=excluded.currency, fiscal_year_end=excluded.fiscal_year_end, "
        "included=excluded.included, reason=excluded.reason, collected_at=excluded.collected_at",
        (ticker, name_zh, name_en, sector, currency, fiscal_year_end,
         1 if included else 0, reason, _now()))
    con.commit()
    con.close()
    print(f"[universe] {ticker} ({name_zh}) included={included}")


def universe_import(db: Path, csv_path: Path) -> int:
    """从 CSV 批量导入标的池。

    列（首行为表头）：ticker,name_zh,name_en,sector,currency[,fiscal_year_end[,reason]]
    缺省 currency=kHKD、fiscal_year_end='12'、included=1。
    返回导入条数。
    """
    import csv as _csv
    n = 0
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in _csv.DictReader(f):
            row = { (k or "").strip(): (v or "").strip() for k, v in row.items() }
            tk = row.get("ticker", "")
            if not tk:
                continue
            fye = row.get("fiscal_year_end") or "12"
            set_universe(db, tk, row.get("name_zh", ""), row.get("name_en", ""),
                         row.get("sector", ""), row.get("currency") or "kHKD",
                         fye, True, row.get("reason", "import"))
            n += 1
    print(f"[universe-import] 共导入 {n} 条")
    return n


# ============================================================ Query Helpers ===


def panel_query(db: Path, ticker: str, limit: int = 60) -> list[dict]:
    """查询面板数据（最近 N 个交易日）。"""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM v_panel WHERE ticker=? ORDER BY trade_date DESC LIMIT ?",
        (ticker, limit)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def forward_returns_query(db: Path, ticker: str, limit: int = 20) -> list[dict]:
    """查询前向收益标签。"""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM forward_returns WHERE ticker=? ORDER BY trade_date DESC LIMIT ?",
        (ticker, limit)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def universe_query(db: Path) -> list[dict]:
    """列出所有标的池条目。"""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM universe ORDER BY ticker").fetchall()
    con.close()
    return [dict(r) for r in rows]


# ============================================================ Self Test ===


def self_test() -> bool:
    """已知答案校验：构造数据验证 forward return 计算。"""
    import tempfile
    results: list[tuple[str, bool, str]] = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        tdb = Path(f.name)

    try:
        # 建库（包含 daily_quotes 本模块不管理的表）
        con = sqlite3.connect(tdb)
        con.executescript("""
        CREATE TABLE IF NOT EXISTS daily_quotes (
            ticker TEXT NOT NULL, quote_date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, amount REAL, currency TEXT NOT NULL DEFAULT 'HKD',
            source TEXT NOT NULL, collected_at TEXT NOT NULL,
            PRIMARY KEY (ticker, quote_date, collected_at, source));
        """)
        con.close()
        init_db(tdb)

        # 插入模拟 daily_quotes（3只股，5天，收盘价 100..104）
        con = sqlite3.connect(tdb)
        batch = dt.date.today().isoformat()
        for tk in ["hk00001", "hk00002", "hk00003"]:
            for i in range(5):
                d = f"2026-01-{i+1:02d}"
                con.execute(
                    "INSERT OR REPLACE INTO daily_quotes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (tk, d, 100+i, 101+i, 99+i, 100+i, 1000000, 50000000, "HKD",
                     "test_source", batch))
        con.commit()
        con.close()

        # 计算 forward returns
        n = compute_forward_returns(tdb)
        check("fwd-ret computed for 3 tickers", n == 3, f"n={n}")

        # 验证 fwd_1d: day1=100, day2=101 => fwd_1d = 1/100 = 0.01
        con = sqlite3.connect(tdb)
        r = con.execute(
            "SELECT fwd_1d FROM forward_returns WHERE ticker='hk00001' AND trade_date='2026-01-01'"
        ).fetchone()
        check("fwd_1d correct (0.01)", r and abs(r[0] - 0.01) < 1e-9,
              f"fwd_1d={r[0] if r else 'None'}")

        # 验证 fwd_5d: day1=100, day5=104 => fwd_5d = 4/100 = 0.04 (仅5天数据, day5无fwd_5d)
        r5 = con.execute(
            "SELECT fwd_5d FROM forward_returns WHERE ticker='hk00001' AND trade_date='2026-01-01'"
        ).fetchone()
        check("fwd_5d is None (insufficient data)", r5 and r5[0] is None,
              f"fwd_5d={r5[0] if r5 else 'None'}")

        # 验证幂等：重跑不增加行数
        n2 = compute_forward_returns(tdb)
        cnt = con.execute("SELECT COUNT(*) FROM forward_returns").fetchone()[0]
        con.close()
        check("idempotent (same row count)", cnt == 15, f"count={cnt}")  # 3 tickers × 5 days

        # 验证 panel view 存在
        con2 = sqlite3.connect(tdb)
        v = con2.execute("SELECT COUNT(*) FROM v_panel").fetchone()[0]
        con2.close()
        check("panel view queryable", v >= 0, f"panel_rows={v}")

        # set_universe UPSERT：更新已有条目不得抹掉 shares_outstanding（第 8 轮加列）
        con3 = sqlite3.connect(tdb)
        con3.execute("ALTER TABLE universe ADD COLUMN shares_outstanding REAL")
        con3.commit()
        con3.close()
        set_universe(tdb, "hk00001", name_zh="长和", sector="Industrials",
                     currency="kHKD", reason="upsert-init")
        con3 = sqlite3.connect(tdb)
        con3.execute("UPDATE universe SET shares_outstanding=91.0 WHERE ticker='hk00001'")
        con3.commit()
        con3.close()
        set_universe(tdb, "hk00001", name_zh="测试更新", sector="Financials",
                     currency="kHKD", reason="upsert-check")
        con3 = sqlite3.connect(tdb)
        row = con3.execute(
            "SELECT name_zh, shares_outstanding FROM universe WHERE ticker='hk00001'").fetchone()
        con3.close()
        check("set_universe upsert preserves shares_outstanding",
              row == ("测试更新", 91.0), f"row={row}")

        # universe_import：CSV 批量导入
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                         encoding="utf-8") as f:
            f.write("ticker,name_zh,sector,currency\n")
            f.write("hk09999,网易,Communication Services,kCNY\n")
            f.write("hk00005,汇丰控股,Financials,kUSD\n")
            fcsv = Path(f.name)
        n = universe_import(tdb, fcsv)
        fcsv.unlink(missing_ok=True)
        con3 = sqlite3.connect(tdb)
        cur = con3.execute("SELECT currency, sector FROM universe WHERE ticker='hk09999'").fetchone()
        con3.close()
        check("universe_import rows", n == 2, f"n={n}")
        check("universe_import fields", cur == ("kCNY", "Communication Services"),
              f"cur={cur}")

    finally:
        try:
            tdb.unlink(missing_ok=True)
        except PermissionError:
            pass  # Windows: file lock may persist briefly

    ok = all(c for _, c, _ in results)
    for name, passed, detail in results:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}  {detail}")
    print(f"[self-test] {'ALL PASS' if ok else 'FAILED'} ({sum(c for _, c, _ in results)}/{len(results)})")
    return ok


# ============================================================ CLI ===


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("cmd", choices=["init", "forward-returns", "benchmark",
                                     "price-factors", "market-cap", "panel", "universe",
                                     "universe-import", "self-test"])
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--index", default="HSI")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--csv", default=None, help="universe-import 的输入 CSV 路径")
    args = ap.parse_args()

    db = Path(args.db)
    if args.cmd == "init":
        init_db(db)
    elif args.cmd == "forward-returns":
        compute_forward_returns(db, args.ticker)
    elif args.cmd == "price-factors":
        compute_price_factors(db, args.ticker)
    elif args.cmd == "market-cap":
        n = fetch_market_cap(db, args.ticker)
        print(f"[market-cap] 成功采集 {n} 只标的的市值/股本")
    elif args.cmd == "benchmark":
        download_benchmark(db, args.index)
    elif args.cmd == "panel":
        for r in panel_query(db, args.ticker or "hk03738", args.limit):
            print(f"  {r['trade_date']}  C={r['close']:.3f}  sector={r['sector']}")
    elif args.cmd == "universe":
        for r in universe_query(db):
            print(f"  {r['ticker']:10s} {r['name_zh']:12s} sector={r['sector']} "
                  f"cur={r['currency']} fye={r['fiscal_year_end']} inc={r['included']}")
    elif args.cmd == "universe-import":
        if not args.csv:
            raise SystemExit("universe-import 需要 --csv <路径>")
        universe_import(db, Path(args.csv))
    elif args.cmd == "self-test":
        raise SystemExit(0 if self_test() else 1)


if __name__ == "__main__":
    main()
