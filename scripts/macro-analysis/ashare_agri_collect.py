#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ashare_agri_collect.py — A 股农业池（农林牧渔 BK0433）日线采集

设计原则（对齐项目既定纪律）
----------------------------
1. **数据层只增不修**：每次采集写入新 `collected_at` 快照，不覆盖历史。
2. **vintage 存档**：同时采集**不复权(fqt=0)**、**前复权(fqt=1)**、**后复权(fqt=2)** 三份。
   - 不复权 = 当时真实成交价，永久稳定，是 vintage 事实基准
   - 前复权 = 以「最新价」为锚向前扣，**会随后续分红送转而整体重算**，且长周期下
     可被高分红扣成**负数**（实测牧原 002714 前复权最低 -3.60 元 → 除零、日收益
     ±833%、99 天 |r|>50%）。→ **禁止用于收益率计算**
   - 后复权 = 以「最早价」为锚向后累加，价格恒为正，日收益必然落在涨跌停限内
     → **收益率/回测一律用后复权**，并逐只做超涨停限校验（qc_backadj）
3. **换手率直接用官方字段**（东财 f61），不自算 —— 需要流通股本，而流通股本历史序列
   本身难获取且随解禁/增发变动，自算会引入偏差。港股 amount 全 NULL 的教训不重演。
4. **成分股数量必须实测**，不靠估：从 ashare_pool_probe.json 读 BK0433 的实际成分。

数据源
------
东财 push2his 历史K线：
  https://push2his.eastmoney.com/api/qt/stock/kline/get
  secid: 沪市 '1.6xxxxx'，深市/北交所 '0.xxxxxx'
  klt=101 日线 | fqt=0 不复权 / 1 前复权 / 2 后复权
  fields2 字段序：日期,开,收,高,低,成交量,成交额,振幅,涨跌幅,涨跌额,换手率

用法
----
    python ashare_agri_collect.py                 # 全量采集（三份复权）
    python ashare_agri_collect.py --limit 10      # 只采前 N 只（冒烟）
    python ashare_agri_collect.py --resume        # 跳过已成功采集的
    python ashare_agri_collect.py --only-badj     # 只补采后复权（给已有库补数据）
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    raise SystemExit("需要 requests")
import numpy as np

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]
OUT_DIR = ROOT / "outputs" / "2026-09-02"
DB_PATH = ROOT / "outputs" / "ashare_agri.sqlite"
PROBE_JSON = OUT_DIR / "ashare_pool_probe.json"

KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
BEG_DATE = "20140101"          # 约 12 年，留足缓冲
MIN_INTERVAL = 1.0             # 东财限流：最小间隔

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                      "AppleWebKit/537.36"})
_last_call = [0.0]


def em_get(params: dict, retries: int = 12, timeout: int = 25):
    """
    节流 + 代理抖动重试。

    踩坑：代理 127.0.0.1:64119 会**间歇性** RemoteDisconnected，实测 6 次重试
    仍会出现单只失败（97 只里约 1~2 只）。默认提到 12 次并加长退避，
    配合 `--resume` 可补齐漏采。
    """
    last = None
    for i in range(retries):
        wait = MIN_INTERVAL - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait + random.uniform(0.1, 0.4))
        try:
            r = SESSION.get(KLINE_URL, params=params, timeout=timeout)
            _last_call[0] = time.time()
            return r
        except Exception as e:              # noqa: BLE001
            last = e
            _last_call[0] = time.time()
            time.sleep(1.0 + 1.5 * i + random.uniform(0, 0.6))
    raise last


def secid_of(code: str) -> str:
    """东财 secid：沪市 1.，深市/北交所 0."""
    if code.startswith("6"):
        return f"1.{code}"
    return f"0.{code}"


def fetch_kline(code: str, fqt: int) -> tuple:
    """返回 (rows, meta)。rows = [(date, open, close, high, low, vol, amount,
    amplitude, pct_chg, chg, turnover), ...]"""
    params = {
        "secid": secid_of(code),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101", "fqt": str(fqt),
        "beg": BEG_DATE, "end": "20500101", "lmt": "100000",
    }
    r = em_get(params)
    d = r.json()
    data = d.get("data") or {}
    klines = data.get("klines") or []
    rows = []
    for line in klines:
        p = line.split(",")
        if len(p) < 11:
            continue
        try:
            rows.append((
                p[0],                                   # 日期
                float(p[1]), float(p[2]), float(p[3]), float(p[4]),   # 开 收 高 低
                float(p[5]),                            # 成交量(手)
                float(p[6]) if p[6] not in ("", "-") else None,   # 成交额
                float(p[7]) if p[7] not in ("", "-") else None,   # 振幅%
                float(p[8]) if p[8] not in ("", "-") else None,   # 涨跌幅%
                float(p[9]) if p[9] not in ("", "-") else None,   # 涨跌额
                float(p[10]) if p[10] not in ("", "-") else None,  # 换手率%
            ))
        except (ValueError, IndexError):
            continue
    meta = {"name": data.get("name"), "code": data.get("code"), "bars": len(rows)}
    return rows, meta


def limit_of(code: str) -> float:
    """涨跌停幅度（QC 用宽松阈值：名义上限 + 1 个百分点容差）。

    创业板 300/301、科创板 688 为 20%（创业板自 2020-08-24 起由 10% 改为 20%），
    其余主板/中小板为 10%。ST 为 5%，但本池已剔除 ST。
    """
    if code.startswith(("300", "301", "688")):
        return 0.21
    return 0.11


def qc_backadj(code: str, rows: list) -> dict:
    """后复权序列质量校验。

    为什么必须校验：前复权（fqt=1）在长周期下会被高分红扣成**负数**
    （实测牧原股份 002714 前复权最低 -3.60 元，出现除零、日收益 ±833% 的
    荒谬值，99 个交易日 |r|>50%）。后复权以最早价为锚向后累加，价格恒为正，
    日收益必然落在涨跌停限内 —— 超限即说明数据源异常，必须拦截。
    """
    if len(rows) < 2:
        return {"bad": True, "reason": "样本不足", "n": len(rows)}
    px = []
    for r in rows:
        c = r[2]
        if c is None:
            continue
        px.append(c)
    if len(px) < 2:
        return {"bad": True, "reason": "有效收盘价不足", "n": len(px)}
    a = np.asarray(px, dtype=float)
    nonpos = int((a <= 0).sum())
    with np.errstate(divide="ignore", invalid="ignore"):
        r = a[1:] / a[:-1] - 1.0
    lim = limit_of(code)
    over = int((np.abs(r) > lim).sum())
    nonfinite = int((~np.isfinite(r)).sum())
    return {
        "bad": bool(nonpos > 0 or over > 0 or nonfinite > 0),
        "n": int(len(a)),
        "nonpositive": nonpos,
        "over_limit_days": over,
        "nonfinite_days": nonfinite,
        "ret_min": float(np.nanmin(r)) if len(r) else None,
        "ret_max": float(np.nanmax(r)) if len(r) else None,
        "limit_used": lim,
    }


# ------------------------------------------------------------------ 数据库

SCHEMA = """
CREATE TABLE IF NOT EXISTS universe_agri (
    code          TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    secid         TEXT NOT NULL,
    board         TEXT NOT NULL,
    is_st         INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_quotes_raw (
    code           TEXT NOT NULL,
    trade_date     TEXT NOT NULL,
    open REAL, close REAL, high REAL, low REAL,
    volume REAL, amount REAL,
    amplitude_pct REAL, pct_chg REAL, chg REAL, turnover_pct REAL,
    source         TEXT NOT NULL DEFAULT 'eastmoney_push2his_fqt0',
    collected_at   TEXT NOT NULL,
    PRIMARY KEY (code, trade_date, collected_at)
);
CREATE TABLE IF NOT EXISTS daily_quotes_adj (
    code           TEXT NOT NULL,
    trade_date     TEXT NOT NULL,
    open REAL, close REAL, high REAL, low REAL,
    volume REAL, amount REAL,
    amplitude_pct REAL, pct_chg REAL, chg REAL, turnover_pct REAL,
    source         TEXT NOT NULL DEFAULT 'eastmoney_push2his_fqt1',
    collected_at   TEXT NOT NULL,
    PRIMARY KEY (code, trade_date, collected_at)
);
CREATE TABLE IF NOT EXISTS daily_quotes_badj (
    code           TEXT NOT NULL,
    trade_date     TEXT NOT NULL,
    open REAL, close REAL, high REAL, low REAL,
    volume REAL, amount REAL,
    amplitude_pct REAL, pct_chg REAL, chg REAL, turnover_pct REAL,
    source         TEXT NOT NULL DEFAULT 'eastmoney_push2his_fqt2',
    collected_at   TEXT NOT NULL,
    PRIMARY KEY (code, trade_date, collected_at)
);
CREATE TABLE IF NOT EXISTS collect_log (
    code TEXT NOT NULL, collected_at TEXT NOT NULL,
    status TEXT NOT NULL, bars INTEGER,
    date_start TEXT, date_end TEXT, error TEXT,
    PRIMARY KEY (code, collected_at)
);
CREATE TABLE IF NOT EXISTS badj_qc (
    code TEXT NOT NULL, collected_at TEXT NOT NULL,
    n_bars INTEGER, nonpositive INTEGER, over_limit_days INTEGER,
    nonfinite_days INTEGER, ret_min REAL, ret_max REAL,
    limit_used REAL, bad INTEGER,
    PRIMARY KEY (code, collected_at)
);
CREATE INDEX IF NOT EXISTS idx_raw_code_date ON daily_quotes_raw(code, trade_date);
CREATE INDEX IF NOT EXISTS idx_adj_code_date ON daily_quotes_adj(code, trade_date);
CREATE INDEX IF NOT EXISTS idx_badj_code_date ON daily_quotes_badj(code, trade_date);
"""


def init_db(conn: sqlite3.Connection):
    conn.executescript(SCHEMA)
    conn.commit()


def load_pool() -> list[dict]:
    """从探测结果读取 BK0433 农林牧渔的实际成分（数量实测，不估算）。"""
    if not PROBE_JSON.exists():
        raise SystemExit(f"缺少 {PROBE_JSON}，请先运行 ashare_pool_probe.py")
    d = json.loads(PROBE_JSON.read_text(encoding="utf-8"))
    for b in d.get("matched", []):
        if b.get("code") == "BK0433":
            return b.get("constituents", [])
    raise SystemExit("探测结果中未找到 BK0433 农林牧渔")


def is_st(name: str) -> bool:
    n = (name or "").replace(" ", "")
    return n.startswith("ST") or n.startswith("*ST") or "退" in n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只采前 N 只（冒烟用）")
    ap.add_argument("--resume", action="store_true", help="跳过已成功采集的")
    ap.add_argument("--include-st", action="store_true", help="包含 ST 股（默认剔除）")
    ap.add_argument("--only-badj", action="store_true",
                    help="只补采后复权(fqt=2)序列，用于给已有库补数据")
    args = ap.parse_args()

    pool = load_pool()
    print(f"BK0433 农林牧渔成分股: {len(pool)} 只")

    st_codes, keep = [], []
    for r in pool:
        if is_st(r["name"]):
            st_codes.append((r["code"], r["name"]))
            if args.include_st:
                keep.append(r)
        else:
            keep.append(r)
    print(f"剔除 ST/退市: {len(st_codes)} 只 -> {[c for c, _ in st_codes]}")
    print(f"入库标的: {len(keep)} 只")

    if args.limit:
        keep = keep[:args.limit]
        print(f"（--limit {args.limit}）实际采集 {len(keep)} 只")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    init_db(conn)
    collected_at = datetime.now().isoformat(timespec="seconds")

    done = set()
    if args.resume or args.only_badj:
        # --only-badj 下，已完成 = daily_quotes_badj 里已有数据；
        # 不能用 collect_log，否则会把「只采过 fqt0/fqt1」的标的误判为已完成。
        src = ("SELECT DISTINCT code FROM daily_quotes_badj" if args.only_badj
               else "SELECT code FROM collect_log WHERE status='ok'")
        done = {r[0] for r in conn.execute(src)}
        print(f"{'--only-badj' if args.only_badj else '--resume'}: 跳过已有 {len(done)} 只")

    ok = fail = skip = qc_bad = 0
    t0 = time.time()
    for i, r in enumerate(keep, 1):
        code, name = r["code"], r["name"]
        if code in done:
            skip += 1
            continue

        conn.execute(
            "INSERT OR IGNORE INTO universe_agri(code,name,secid,board,is_st,first_seen_at)"
            " VALUES(?,?,?,?,?,?)",
            (code, name, secid_of(code), "BK0433", int(is_st(name)), collected_at))

        try:
            if args.only_badj:
                raw_rows, meta = fetch_kline(code, 2)   # 后复权，用于收益率
                adj_rows = None
                badj_rows = raw_rows
            else:
                raw_rows, meta = fetch_kline(code, 0)
                adj_rows, _ = fetch_kline(code, 1)
                badj_rows, _ = fetch_kline(code, 2)
        except Exception as e:                       # noqa: BLE001
            conn.execute("INSERT OR REPLACE INTO collect_log VALUES(?,?,?,?,?,?,?)",
                         (code, collected_at, "fail", 0, None, None, str(e)[:300]))
            conn.commit()
            print(f"  [{i}/{len(keep)}] {code} {name:<8} FAIL {type(e).__name__}")
            fail += 1
            continue

        if not raw_rows:
            conn.execute("INSERT OR REPLACE INTO collect_log VALUES(?,?,?,?,?,?,?)",
                         (code, collected_at, "empty", 0, None, None, "klines 为空"))
            conn.commit()
            print(f"  [{i}/{len(keep)}] {code} {name:<8} EMPTY")
            fail += 1
            continue

        targets = []
        if args.only_badj:
            targets = [("daily_quotes_badj", badj_rows, "eastmoney_push2his_fqt2")]
        else:
            targets = [("daily_quotes_raw", raw_rows, "eastmoney_push2his_fqt0"),
                       ("daily_quotes_adj", adj_rows, "eastmoney_push2his_fqt1"),
                       ("daily_quotes_badj", badj_rows, "eastmoney_push2his_fqt2")]

        # 后复权质量校验：价格必须为正、日收益必须落在涨跌停限内
        qc = qc_backadj(code, badj_rows or [])
        conn.execute(
            "INSERT OR REPLACE INTO badj_qc(code,collected_at,n_bars,nonpositive,"
            "over_limit_days,nonfinite_days,ret_min,ret_max,limit_used,bad)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (code, collected_at, qc.get("n"), qc.get("nonpositive"),
             qc.get("over_limit_days"), qc.get("nonfinite_days"),
             qc.get("ret_min"), qc.get("ret_max"), qc.get("limit_used"),
             int(bool(qc.get("bad")))))
        if qc.get("bad"):
            qc_bad += 1
            print(f"  ⚠ [{i}/{len(keep)}] {code} {name:<8} QC异常 "
                  f"非正价={qc.get('nonpositive')} 超涨停限={qc.get('over_limit_days')}天 "
                  f"非有限={qc.get('nonfinite_days')}天 "
                  f"收益区间=[{qc.get('ret_min'):.2%},{qc.get('ret_max'):.2%}]")

        for table, rows, src in targets:
            if not rows:
                continue
            conn.executemany(
                f"INSERT OR REPLACE INTO {table}("
                "code,trade_date,open,close,high,low,volume,amount,"
                "amplitude_pct,pct_chg,chg,turnover_pct,source,collected_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(code, d, o, c, h, l, v, a, amp, pc, ch, to, src, collected_at)
                 for (d, o, c, h, l, v, a, amp, pc, ch, to) in rows])

        ds = [x[0] for x in raw_rows]
        conn.execute("INSERT OR REPLACE INTO collect_log VALUES(?,?,?,?,?,?,?)",
                     (code, collected_at, "ok", len(raw_rows), ds[0], ds[-1], None))
        conn.commit()
        ok += 1
        if i % 10 == 0 or i == len(keep):
            el = time.time() - t0
            print(f"  [{i}/{len(keep)}] {code} {name:<8} {len(raw_rows):>5} bars "
                  f"{ds[0]}~{ds[-1]}  累计 ok={ok} fail={fail} {el:.0f}s")
        else:
            print(f"  [{i}/{len(keep)}] {code} {name:<8} {len(raw_rows):>5} bars {ds[0]}~{ds[-1]}")

    conn.commit()

    # 汇总
    print("\n" + "=" * 70)
    print(f"采集完成: ok={ok} fail={fail} skip={skip} QC异常={qc_bad}  用时 {time.time()-t0:.0f}s")
    for t in ("daily_quotes_raw", "daily_quotes_adj", "daily_quotes_badj"):
        r = conn.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT code), MIN(trade_date), MAX(trade_date) FROM {t}"
        ).fetchone()
        print(f"  {t:<20} rows={r[0]:<8} codes={r[1]:<5} {r[2]} ~ {r[3]}")
    tr = conn.execute(
        "SELECT COUNT(*), COUNT(turnover_pct), COUNT(amount) FROM daily_quotes_badj").fetchone()
    print(f"  后复权表：换手率非空 {tr[1]}/{tr[0]}   成交额非空 {tr[2]}/{tr[0]}")
    qb = conn.execute("SELECT COUNT(*) FROM badj_qc WHERE bad=1").fetchone()[0]
    qt = conn.execute("SELECT COUNT(*) FROM badj_qc").fetchone()[0]
    print(f"  后复权 QC：异常 {qb}/{qt} 只"
          + ("  ← 需人工核查（上市首日无涨跌幅限制属正常）" if qb else "  ✓ 全部通过"))

    summary = {
        "generated_at": collected_at,
        "board": "BK0433 农林牧渔",
        "pool_total": len(pool), "st_excluded": [c for c, _ in st_codes],
        "collected": ok, "failed": fail, "skipped": skip, "qc_bad": qc_bad,
        "db": str(DB_PATH),
    }
    out = OUT_DIR / "ashare_agri_collect_summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n汇总已写入: {out}")
    conn.close()


if __name__ == "__main__":
    main()
