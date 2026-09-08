# -*- coding: utf-8 -*-
"""A股 CSI800 原始（未复权）收盘价采集 —— 腾讯 fqkline，fq=day。

用途：估值 PB = 原始收盘价 / 每股净资产(bps)。后复权价会污染截面排序
（复权因子与分红相关 → 高分红低PB股被系统性抬高），故 PB 必须用未复权价。

与 ashare_badj_collect.py 的区别：
  - fq 参数：空（不复权）→ 响应键 `day`（不是 `hfqday`）
  - 只存 close（PB 只需收盘价）
  - 不做涨跌停 QC（原始价在除权除息日会跳空，属正常，非数据错误）

复用 badj_collect 的：HOSTS（3 域名轮换）、build_chunks（18 个月分段防 641 截头）、
tx_symbol、board_of、UA、连接池并发。六坑同样适用。
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

import sys
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from ashare_badj_collect import (  # noqa: E402
    HOSTS, build_chunks, tx_symbol, board_of, EXCLUDE_BOARDS, UA,
)

ROOT = _HERE.parents[3]
DB = ROOT / "outputs" / "ashare_csi800_raw.sqlite"
REGISTRY = ROOT / "outputs" / "ashare_strategy_registry.sqlite"
PXDB = ROOT / "outputs" / "ashare_csi800_hfq_xq.sqlite"
SOURCE = "tencent-fqkline-raw"


def ensure_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS daily_quotes_raw (
        code TEXT NOT NULL, trade_date TEXT NOT NULL,
        close REAL, source TEXT, collected_at TEXT,
        PRIMARY KEY (code, trade_date)
    );
    CREATE INDEX IF NOT EXISTS ix_raw_date ON daily_quotes_raw(trade_date);
    CREATE INDEX IF NOT EXISTS ix_raw_code ON daily_quotes_raw(code);
    """)
    conn.commit()


def _fetch_chunk(sym, a2, b, session, sleep):
    prm = f"{sym},day,{a2},{b},640,"
    last = None
    for host in HOSTS:
        for attempt in range(3):
            try:
                r = session.get(host, params={"param": prm},
                                headers={"User-Agent": UA}, timeout=30)
                j = r.json()
                d = j.get("data")
                if not isinstance(d, dict):
                    raise RuntimeError(f"param error: {str(j)[:80]}")
                rows = d.get(sym, {}).get("day")
                return (rows if rows is not None else []), None
            except Exception as e:
                last = e
                time.sleep(sleep * (2 ** attempt))
    return None, last


def fetch_one(code, since, session, sleep, workers=6):
    sym = tx_symbol(code)
    jobs = [(max(a, since), b)
            for a, b in build_chunks(since, date.today().isoformat())
            if not b < since]
    bars = {}
    last_err = None
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as ex:
        futs = {ex.submit(_fetch_chunk, sym, a2, b, session, sleep): (a2, b)
                for a2, b in jobs}
        for f in as_completed(futs):
            rows, err = f.result()
            if rows is None:
                last_err = err
                continue
            for row in rows:
                if len(row) >= 3:
                    bars[row[0]] = float(row[2])   # [date, open, close, ...]
    if not bars:
        raise RuntimeError(f"全部分段无数据：{last_err}")
    return bars


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--codes", default="", help="逗号分隔；缺省=CSI800 池全量")
    ap.add_argument("--since", default="2014-01-01")
    ap.add_argument("--sleep", type=float, default=0.35)
    ap.add_argument("--limit", type=int, default=0, help="只采前 N 只（测试用）")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    ensure_schema(conn)

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        codes = [r[0] for r in sqlite3.connect(str(PXDB)).execute(
            "SELECT DISTINCT code FROM daily_quotes_hfq ORDER BY code")]
        codes = [c for c in codes if board_of(c)[0] not in EXCLUDE_BOARDS]
    if args.limit:
        codes = codes[:args.limit]

    done = {r[0] for r in conn.execute("SELECT DISTINCT code FROM daily_quotes_raw")}
    codes = [c for c in codes if c not in done]
    print(f"待采集 {len(codes)} 只  起始 {args.since}  数据源 {SOURCE}")

    session = requests.Session()
    _ad = HTTPAdapter(pool_connections=16, pool_maxsize=16)
    session.mount("https://", _ad)
    session.mount("http://", _ad)

    ok = fail = 0
    for i, code in enumerate(codes, 1):
        now = datetime.now().isoformat(timespec="seconds")
        try:
            bars = fetch_one(code, args.since, session, args.sleep)
            rows = [(code, d, c, SOURCE, now) for d, c in bars.items()]
            conn.executemany(
                "INSERT OR REPLACE INTO daily_quotes_raw "
                "(code,trade_date,close,source,collected_at) VALUES (?,?,?,?,?)",
                rows)
            conn.commit()
            ok += 1
        except Exception as e:
            fail += 1
            print(f"  ✗ {code}: {e}")
        if i % 50 == 0:
            print(f"  进度 {i}/{len(codes)}  成功 {ok} 失败 {fail}")
    print(f"完成：成功 {ok} / 失败 {fail}")
    conn.close()


if __name__ == "__main__":
    main()
