#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
宽基池流通股本采集 + 换手率反算（东财 datacenter 估值明细）
================================================================================

问题背景
--------------------------------------------------------------------------------
农业窄池候选策略 `comp_turn_mom`（低换手 + 20 日反转）过了三关，但归因显示超额
100% 来自风格暴露。要判定它是**结构效应**还是**窄池特异**，必须在独立截面
（宽基池 996 只）上重跑同一配置。

首轮外推被硬闸门拒绝：

    单因子·低换手      turnover_level_20d=0.0%   ✗ 拒绝
    合成·低换手+反转    turnover_level_20d=0.0%   ✗ 拒绝

根因：宽池价格来自腾讯 fqkline（无换手率字段），库里那张 daily_liquidity 是从
农业池整表复制来的，实测只有 107 只（= 农业池成员），单元命中率 2.3%。

为什么走「股本反算」而不是直接采换手率
--------------------------------------------------------------------------------
2026-09-02 实测的数据源连通性（沙箱代理 127.0.0.1:64119）：

    腾讯 fqkline                    ✓ 200
    东财 datacenter-web             ✓ 200
    东财 push2his.eastmoney.com     ✗ ProxyError（8/8 全败，域名级阻断）
    东财 push2.eastmoney.com        ✗ ProxyError
    东财 push2delay.eastmoney.com   △ 200 但 klines 为空（只返回元数据）
    网易 quotes.money.163.com       ✗ 502 Bad Gateway
    新浪 CN_MarketData              ✓ 200 但只有 OHLCV，无换手率

东财原生换手率（push2his 的 f61）所在域名被阻断，直连也被沙箱禁止。而
datacenter-web 是通的，其 `RPT_VALUEANALYSIS_DET` 提供**日频流通股本**：

    FREE_SHARES_A   流通A股股数
    TOTAL_SHARES    总股本
    CLOSE_PRICE     收盘价（不复权）
    TRADE_DATE      交易日

于是：换手率(%) = 成交量(股) / 流通股本(股) × 100

★ 复权无关性：成交量与股本都是**不受复权影响**的原始市场事实，因此用不复权
  口径的股本与雪球后复权价格（`ashare_wide_hfq_xq.sqlite`）并用，不存在口径
  混用问题；标的全集与覆盖率分母同样取自该新库。

★ 时间覆盖的诚实边界：估值明细**只回溯到 2018-01-02**（实测 2104 个交易日），
  而价格面板从 2014-01-02 起。因此反算换手率覆盖约 68% 的时间跨度。
  → 用它做跨池外推时，**农业池必须限制到同一时间窗（2018+）做对照**，
    否则会把「跨期差异」误读成「跨池差异」。

★ 交叉校验（本脚本的核心防错设计）
  库里那 107 只有东财**原生** turnover_pct。反算值必须与原生值高度一致
  （目标相关系数 > 0.99、中位相对误差 < 2%），否则说明单位或公式错了
  （最典型：腾讯 volume 单位是「手」还是「股」）。校验不过就不许写入。

用法
--------------------------------------------------------------------------------
    python ashare_wide_shares.py --probe                 # 只探连通性与字段
    python ashare_wide_shares.py --collect --workers 4   # 采集全部 996 只
    python ashare_wide_shares.py --crosscheck            # 反算 vs 原生 一致性
    python ashare_wide_shares.py --derive                # 写入派生换手率
    python ashare_wide_shares.py --verify                # 覆盖率核查
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]                       # D:\tmp\deepseek-harness
OUT_DIR = ROOT / "outputs" / "2026-09-02"
DB = ROOT / "outputs" / "ashare_wide_hfq.sqlite"
# 写库目标仍是旧库：daily_shares 是**非行情表**，存量数据只在旧库，
# 改指新库会把同一张表分裂在两处。旧库被废弃的只有 daily_quotes_hfq。
# 但「标的全集 / 覆盖率分母」必须从雪球新库取 —— ATTACH 后以 xq. 前缀访问。
POOL_DB = ROOT / "outputs" / "ashare_wide_hfq_xq.sqlite"

API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT = "RPT_VALUEANALYSIS_DET"
COLUMNS = "SECURITY_CODE,TRADE_DATE,FREE_SHARES_A,TOTAL_SHARES,CLOSE_PRICE"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SOURCE = "eastmoney_datacenter_valueanalysis"
DERIVED_SRC = "derived_volume_div_freeshares"

_gate = threading.Lock()
_last = [0.0]


def throttled_get(params: dict, rate: float, retries: int, timeout: int = 40):
    """全局节流 + 指数退避。并发只用于重叠网络等待，不放大瞬时压力。"""
    err = None
    for i in range(retries):
        with _gate:
            wait = rate - (time.time() - _last[0])
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()
        try:
            r = requests.get(API, params=params,
                             headers={"User-Agent": UA}, timeout=timeout)
            if r.status_code == 200:
                return r
            err = RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:                                   # noqa: BLE001
            err = e
        time.sleep(0.7 + 1.3 * i + random.uniform(0, 0.5))
    raise err if err else RuntimeError("unknown")


def fetch_shares(code: str, rate: float, retries: int) -> list:
    """取一只股票的日频股本序列。返回 [(date, free_shares, total_shares, close_raw)]

    pageSize=3000 实测可一次返回全部 2104 行（pages=1），故正常情况 1 请求/只。
    仍保留翻页兜底，防止将来行数超过 3000。
    """
    out, page = [], 1
    while True:
        p = {"reportName": REPORT, "columns": COLUMNS,
             "pageSize": "3000", "pageNumber": str(page),
             "filter": f'(SECURITY_CODE="{code}")',
             "sortColumns": "TRADE_DATE", "sortTypes": "1"}
        j = throttled_get(p, rate, retries).json() or {}
        res = j.get("result") or {}
        rows = res.get("data") or []
        for r in rows:
            d = (r.get("TRADE_DATE") or "")[:10]
            if not d:
                continue
            out.append((d, r.get("FREE_SHARES_A"),
                        r.get("TOTAL_SHARES"), r.get("CLOSE_PRICE")))
        if page >= int(res.get("pages") or 1) or not rows:
            break
        page += 1
    return out


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS daily_shares (
        code TEXT NOT NULL, trade_date TEXT NOT NULL,
        free_shares REAL, total_shares REAL, close_raw REAL,
        source TEXT, collected_at TEXT,
        PRIMARY KEY (code, trade_date)
    );
    CREATE INDEX IF NOT EXISTS ix_shares_code ON daily_shares(code);
    CREATE INDEX IF NOT EXISTS ix_shares_date ON daily_shares(trade_date);
    CREATE TABLE IF NOT EXISTS collect_log_shares (
        code TEXT PRIMARY KEY, ok INTEGER, n_bars INTEGER,
        date_from TEXT, date_to TEXT, err TEXT, collected_at TEXT
    );
    CREATE TABLE IF NOT EXISTS daily_liquidity (
        code TEXT NOT NULL, trade_date TEXT NOT NULL,
        volume REAL, amount REAL, turnover_pct REAL,
        source TEXT, collected_at TEXT,
        PRIMARY KEY (code, trade_date)
    );
    """)
    conn.commit()


# ------------------------------------------------------------------ 采集

def cmd_collect(conn, a) -> None:
    allc = [r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM xq.daily_quotes_hfq ORDER BY code")]
    if a.codes:
        want = {c.strip() for c in a.codes.split(",") if c.strip()}
        allc = [c for c in allc if c in want]
    if not a.redo:
        have = {r[0] for r in conn.execute(
            "SELECT code FROM daily_shares GROUP BY code HAVING COUNT(free_shares)>0")}
        allc = [c for c in allc if c not in have]
    if a.limit:
        allc = allc[: a.limit]

    print("=" * 78)
    print("宽基池流通股本采集（东财 datacenter 估值明细）")
    print("=" * 78)
    print(f"待采 {len(allc)} 只   并发 {a.workers}   节流 {a.rate}s/请求")
    if not allc:
        print("无待采代码。")
        return
    print("-" * 78)

    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    t0 = time.time()
    done = ok = fail = nrow = 0
    fails = []
    lock = threading.Lock()

    def job(code):
        try:
            return code, fetch_shares(code, a.rate, a.retries), None
        except Exception as e:                                   # noqa: BLE001
            return code, None, f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = [ex.submit(job, c) for c in allc]
        for f in as_completed(futs):
            code, rows, err = f.result()
            with lock:
                done += 1
                if rows:
                    conn.executemany(
                        "INSERT OR REPLACE INTO daily_shares"
                        "(code,trade_date,free_shares,total_shares,close_raw,"
                        "source,collected_at) VALUES (?,?,?,?,?,?,?)",
                        [(code, d, fs, ts, cp, SOURCE, now)
                         for d, fs, ts, cp in rows])
                    conn.execute(
                        "INSERT OR REPLACE INTO collect_log_shares"
                        "(code,ok,n_bars,date_from,date_to,err,collected_at)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (code, 1, len(rows), rows[0][0], rows[-1][0], None, now))
                    ok += 1
                    nrow += len(rows)
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO collect_log_shares"
                        "(code,ok,n_bars,date_from,date_to,err,collected_at)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (code, 0, 0, None, None, err or "空", now))
                    fail += 1
                    fails.append((code, err or "空"))
                if done % 25 == 0 or done == len(allc):
                    conn.commit()
                    el = time.time() - t0
                    print(f"  [{done}/{len(allc)}] ok={ok} fail={fail} "
                          f"rows={nrow}  用时 {el/60:.1f}m  "
                          f"ETA {el/done*(len(allc)-done)/60:.1f}m")
    conn.commit()
    print("-" * 78)
    print(f"完成：成功 {ok} / 失败 {fail} / 行数 {nrow} / "
          f"耗时 {(time.time()-t0)/60:.1f} 分钟")
    for c, e in fails[:15]:
        print(f"    {c}  {e}")


# ------------------------------------------------------------------ 交叉校验

def cmd_crosscheck(conn, a) -> dict:
    """反算换手率 vs 东财原生 turnover_pct（用库里已有的 107 只重叠样本）。

    这是**防止公式/单位错误**的关键闸门。腾讯 volume 单位若是「手」，
    则 换手率% = volume × 100 / free_shares × 100。若单位是「股」，
    则不该乘 100。两者差 100 倍，靠本校验区分。
    """
    print("=" * 78)
    print("交叉校验：反算换手率 vs 东财原生 turnover_pct")
    print("=" * 78)
    rows = conn.execute("""
        SELECT q.code, q.trade_date, q.volume, s.free_shares, l.turnover_pct
        FROM xq.daily_quotes_hfq q
        JOIN daily_shares    s ON s.code=q.code AND s.trade_date=q.trade_date
        JOIN daily_liquidity l ON l.code=q.code AND l.trade_date=q.trade_date
        WHERE l.turnover_pct IS NOT NULL AND l.source LIKE 'eastmoney_push2his%'
          AND s.free_shares > 0 AND q.volume > 0
    """).fetchall()
    if not rows:
        print("✗ 无重叠样本，无法校验（需先采集股本，且库中要有原生换手率）")
        return {"ok": False, "reason": "no_overlap"}

    vol = np.array([r[2] for r in rows], float)
    fs = np.array([r[3] for r in rows], float)
    native = np.array([r[4] for r in rows], float)
    n_codes = len({r[0] for r in rows})

    cands = {
        "volume 视为手（×100）": vol * 100.0 / fs * 100.0,
        "volume 视为股（×1）": vol / fs * 100.0,
    }
    print(f"重叠样本: {len(rows)} 个单元 / {n_codes} 只\n")
    best, best_key = None, None
    for key, derived in cands.items():
        m = np.isfinite(derived) & np.isfinite(native) & (native > 0)
        if m.sum() < 100:
            print(f"  {key:22s} 有效样本不足")
            continue
        corr = float(np.corrcoef(derived[m], native[m])[0, 1])
        relerr = np.abs(derived[m] - native[m]) / native[m]
        med = float(np.median(relerr))
        p90 = float(np.percentile(relerr, 90))
        ratio = float(np.median(derived[m] / native[m]))
        print(f"  {key:22s} 相关={corr:.6f}  中位相对误差={med:.4%}  "
              f"P90={p90:.4%}  中位比值={ratio:.4f}")
        if best is None or (corr > best["corr"] and med < best["median_relerr"]):
            best = {"corr": corr, "median_relerr": med,
                    "p90_relerr": p90, "median_ratio": ratio, "n": int(m.sum())}
            best_key = key

    ok = bool(best and best["corr"] > 0.99 and best["median_relerr"] < 0.02)
    print()
    print(f"胜出口径: {best_key}")
    print(f"判定: {'✓ 通过（相关>0.99 且中位相对误差<2%）→ 允许写入派生换手率' if ok else '✗ 未通过 → 禁止写入，需先排查单位/公式'}")
    return {"ok": ok, "winner": best_key, **(best or {}),
            "n_units": len(rows), "n_codes": n_codes}


# ------------------------------------------------------------------ 派生写入

def cmd_derive(conn, a) -> dict:
    """把反算换手率写入 daily_liquidity（source 标记为派生，可与原生区分）。

    只写 daily_liquidity 中**尚无原生 turnover_pct** 的单元 —— 原生数据优先，
    遵循「数据层只增不修」。
    """
    cc = cmd_crosscheck(conn, a)
    if not cc.get("ok") and not a.force:
        print("\n交叉校验未通过，拒绝写入。确需强写请加 --force（不推荐）。")
        return {"written": 0, "crosscheck": cc}
    mult = 100.0 if "手" in str(cc.get("winner")) else 1.0
    print(f"\n采用换算系数: volume × {mult:g} / free_shares × 100")

    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    cur = conn.execute(f"""
        SELECT q.code, q.trade_date, q.volume, s.free_shares
        FROM xq.daily_quotes_hfq q
        JOIN daily_shares s ON s.code=q.code AND s.trade_date=q.trade_date
        LEFT JOIN daily_liquidity l
               ON l.code=q.code AND l.trade_date=q.trade_date
              AND l.turnover_pct IS NOT NULL
        WHERE l.code IS NULL AND s.free_shares > 0 AND q.volume >= 0
    """)
    batch, n = [], 0
    for code, d, vol, fs in cur:
        tp = float(vol) * mult / float(fs) * 100.0
        batch.append((code, d, float(vol), None, tp, DERIVED_SRC, now))
        if len(batch) >= 20000:
            conn.executemany(
                "INSERT OR REPLACE INTO daily_liquidity"
                "(code,trade_date,volume,amount,turnover_pct,source,collected_at)"
                " VALUES (?,?,?,?,?,?,?)", batch)
            n += len(batch)
            batch.clear()
    if batch:
        conn.executemany(
            "INSERT OR REPLACE INTO daily_liquidity"
            "(code,trade_date,volume,amount,turnover_pct,source,collected_at)"
            " VALUES (?,?,?,?,?,?,?)", batch)
        n += len(batch)
    conn.commit()
    print(f"写入派生换手率 {n} 行")
    return {"written": n, "multiplier": mult, "crosscheck": cc}


# ------------------------------------------------------------------ 核查

def cmd_verify(conn) -> dict:
    n_code_q, n_cell_q = conn.execute(
        "SELECT COUNT(DISTINCT code), COUNT(*) FROM xq.daily_quotes_hfq").fetchone()
    n_join = conn.execute("""
        SELECT COUNT(*) FROM xq.daily_quotes_hfq q
        JOIN daily_liquidity l ON l.code=q.code AND l.trade_date=q.trade_date
        WHERE l.turnover_pct IS NOT NULL
    """).fetchone()[0]
    n_code_l = conn.execute(
        "SELECT COUNT(DISTINCT code) FROM daily_liquidity "
        "WHERE turnover_pct IS NOT NULL").fetchone()[0]
    by_src = list(conn.execute(
        "SELECT source, COUNT(*), COUNT(DISTINCT code) FROM daily_liquidity "
        "WHERE turnover_pct IS NOT NULL GROUP BY source"))
    rng = conn.execute(
        "SELECT MIN(trade_date), MAX(trade_date) FROM daily_liquidity "
        "WHERE turnover_pct IS NOT NULL").fetchone()
    # 2018 起的单元命中率（估值表覆盖窗口内的真实覆盖度）
    n_cell_q18 = conn.execute(
        "SELECT COUNT(*) FROM xq.daily_quotes_hfq WHERE trade_date>='2018-01-02'").fetchone()[0]
    n_join18 = conn.execute("""
        SELECT COUNT(*) FROM xq.daily_quotes_hfq q
        JOIN daily_liquidity l ON l.code=q.code AND l.trade_date=q.trade_date
        WHERE l.turnover_pct IS NOT NULL AND q.trade_date>='2018-01-02'
    """).fetchone()[0]
    v = {
        "codes_in_price": n_code_q, "codes_with_turnover": n_code_l,
        "code_hit_rate": round(n_code_l / n_code_q, 4) if n_code_q else 0,
        "cells_in_price": n_cell_q, "cells_joined": n_join,
        "cell_hit_rate": round(n_join / n_cell_q, 4) if n_cell_q else 0,
        "cells_in_price_2018plus": n_cell_q18, "cells_joined_2018plus": n_join18,
        "cell_hit_rate_2018plus": round(n_join18 / n_cell_q18, 4) if n_cell_q18 else 0,
        "turnover_date_range": list(rng) if rng else None,
        "by_source": [{"source": s, "cells": c, "codes": k} for s, c, k in by_src],
    }
    print("=" * 78)
    print("宽基池换手率覆盖率核查")
    print("=" * 78)
    for k, val in v.items():
        if k == "by_source":
            print("  by_source:")
            for it in val:
                print(f"      {it['source']:38s} {it['cells']:>9,} 单元 / "
                      f"{it['codes']:>4} 只")
        else:
            print(f"  {k:26s} {val}")
    g_all, g18 = v["cell_hit_rate"], v["cell_hit_rate_2018plus"]
    print(f"\n分量覆盖率闸门（阈值 30%）:")
    print(f"    全窗口 2014+ : {g_all:.1%}  "
          f"{'✓' if g_all >= 0.30 else '✗（估值表只到 2018，全窗口天然受限）'}")
    print(f"    2018+ 窗口   : {g18:.1%}  "
          f"{'✓ comp_turn_mom 可在宽池验证（须限窗 2018+，农业池同窗对照）' if g18 >= 0.30 else '✗ 仍不可用'}")
    return v


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--crosscheck", action="store_true")
    ap.add_argument("--derive", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--codes", default="")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rate", type=float, default=0.25)
    ap.add_argument("--retries", type=int, default=6)
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(a.db, timeout=180)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if not POOL_DB.exists():
        raise SystemExit(f"雪球行情库缺失：{POOL_DB}\n标的全集必须由新库定义（旧库 hfq 已废弃）")
    conn.execute("ATTACH DATABASE ? AS xq", (str(POOL_DB),))
    ensure_schema(conn)

    rep = {"generated_at": datetime.now(timezone.utc).astimezone()
           .isoformat(timespec="seconds")}
    if a.probe:
        rows = fetch_shares("000001", a.rate, a.retries)
        print(f"探测 000001: {len(rows)} 行  {rows[0] if rows else None} .. "
              f"{rows[-1] if rows else None}")
        rep["probe"] = {"n": len(rows),
                        "first": rows[0] if rows else None,
                        "last": rows[-1] if rows else None}
    if a.collect:
        cmd_collect(conn, a)
    if a.crosscheck:
        rep["crosscheck"] = cmd_crosscheck(conn, a)
    if a.derive:
        rep["derive"] = cmd_derive(conn, a)
    if a.verify:
        rep["verify"] = cmd_verify(conn)

    if len(rep) > 1:
        p = OUT_DIR / "ashare_wide_shares.json"
        p.write_text(json.dumps(rep, ensure_ascii=False, indent=2,
                                default=str), encoding="utf-8")
        print(f"\n报告已写入: {p}")
    conn.close()


if __name__ == "__main__":
    main()
