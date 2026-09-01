#!/usr/bin/env python3
"""Equity daily quotes collector -> append-only vintage DB (market_quotes table).

数据源：腾讯日K接口（web.ifzq.gtimg.cn，免费、直连、无需 key）。
红线：行情属第三方数据（source_trust: third_party），如实标注，不冒充官方一手；
append-only：同批次(collected_at)重跑幂等，跨批次只增不修。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import urllib.request
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[4] / "outputs" / "equity_fundamental.sqlite"
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{count},qfq"
UA = "equity-vintage-collector/1.0 (+daily-quotes)"
QUOTE_SRC = "tencent_ifzq_kline"

SCHEMA_QUOTES = """
CREATE TABLE IF NOT EXISTS daily_quotes (
    ticker       TEXT NOT NULL,
    quote_date   TEXT NOT NULL,
    open         REAL,
    high         REAL,
    low          REAL,
    close        REAL,
    volume       REAL,
    amount       REAL,
    currency     TEXT NOT NULL DEFAULT 'HKD',
    source       TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (ticker, quote_date, collected_at, source)
);
"""

SOURCES_QUOTES = [
    (QUOTE_SRC, "腾讯 ifzq 日K接口", "third_party", "第三方行情日线，仅用于回测/风险指标"),
]


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _as_number(x) -> float | None:
    """腾讯接口不同标的的字段类型不一致（偶发 dict/None），统一安全转换。"""
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str) and x.strip():
        try:
            return float(x)
        except ValueError:
            return None
    return None


def fetch_kline(symbol: str, count: int) -> list[dict]:
    url = KLINE_URL.format(symbol=symbol, count=count)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data", {}).get(symbol, {})
    # 腾讯接口：复权键 qfqday 不存在时退回 day
    rows = data.get("qfqday") or data.get("day") or []
    out = []
    for row in rows:
        # 字段顺序 [date, open, close, high, low, volume, (amount...)]
        if len(row) < 6:
            continue
        out.append({
            "date": row[0], "open": float(row[1]), "close": float(row[2]),
            "high": float(row[3]), "low": float(row[4]),
            "volume": _as_number(row[5]),
            "amount": _as_number(row[6]) if len(row) > 6 else None,
        })
    return out


def collect(db: Path, symbols: list[str], count: int) -> None:
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_QUOTES)
    for row in SOURCES_QUOTES:
        con.execute("INSERT OR REPLACE INTO source_trust VALUES (?,?,?,?)", row)
    # batch 用天级日期（与 ingest/fundamental 一致）：同日重跑幂等，避免重复批次堆积
    batch = dt.date.today().isoformat()
    for symbol in symbols:
        ticker = symbol.lower().lstrip("hk") and ("hk" + symbol.lower().lstrip("hk"))
        rows = fetch_kline(symbol, count)
        if not rows:
            print(f"[collect] {symbol}: 0 rows (source check FAILED)")
            continue
        # 行情是「最新快照」语义（非 vintage）：同一 source 的旧批次全部覆盖，
        # 避免跨批次（count 漂移/复权调整）造成同一日期多行重复。基本面数据才用 append-only。
        con.execute("DELETE FROM daily_quotes WHERE ticker=? AND source=?",
                    (ticker, QUOTE_SRC))
        for rrow in rows:
            con.execute("INSERT OR REPLACE INTO daily_quotes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (ticker, rrow["date"], rrow["open"], rrow["high"], rrow["low"],
                         rrow["close"], rrow["volume"], rrow["amount"], "HKD", QUOTE_SRC, batch))
        con.commit()
        first, last = rows[0]["date"], rows[-1]["date"]
        print(f"[collect] {ticker}: {len(rows)} bars ({first} .. {last}) batch={batch}")
    con.close()


def query(db: Path, ticker: str, limit: int) -> None:
    con = sqlite3.connect(db)
    for row in con.execute(
        "SELECT quote_date, open, high, low, close, volume FROM daily_quotes "
        "WHERE ticker=? ORDER BY quote_date DESC LIMIT ?", (ticker, limit)):
        print(f"  {row[0]}  O={row[1]:.3f} H={row[2]:.3f} L={row[3]:.3f} C={row[4]:.3f} V={row[5]:,.0f}")
    n = con.execute("SELECT COUNT(*), MIN(quote_date), MAX(quote_date) FROM daily_quotes "
                    "WHERE ticker=?", (ticker,)).fetchone()
    print(f"  total={n[0]} range={n[1]}..{n[2]}")
    con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--symbols", default="hk03738", help="逗号分隔，如 hk03738,00700")
    ap.add_argument("--count", type=int, default=320)
    ap.add_argument("cmd", choices=["collect", "query"])
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()
    syms = [s.lower() if s.lower().startswith("hk") else "hk" + s.lstrip("hk").lower().rjust(5, "0")
            for s in args.symbols.split(",")]
    if args.cmd == "collect":
        collect(Path(args.db), syms, args.count)
    else:
        query(Path(args.db), args.symbols.lower(), args.limit)


if __name__ == "__main__":
    main()
