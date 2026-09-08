# -*- coding: utf-8 -*-
"""另类数据事件研究（方向 2 工序 4）：龙虎榜 / 大宗 / 解禁 的风险标签价值判定。

判据（与两融 neglist 同源，改「风险标签价值」而非篮子超额）：
    事件发生后（T+1 收盘买入），持有 h 日的【超额收益】= 个股前向收益 − 同期池中位数前向收益。
    若某信号把「脆弱群体」（高解禁 / 深度折价大宗 / 龙虎榜净卖出）与「正常群体」系统区分开
    （脆弱组超额收益显著为负、且逐年稳定），即构成风险标签。

信号（预注册，防数据挖掘）：
    解禁 lift_stage：free_ratio ≥ 0.20（高解禁，≈top25%）vs ≤ 0.05（低解禁）
    大宗 block_trade：premium_ratio ≤ −0.08（深度折价抛售）vs ≥ −0.01（平价/溢价）
    龙虎榜 lhb_daily：net_amt < 0（净卖出）vs > 0（净买入）；另测高换手（游资炒作）

口径纪律：
    - 事件日信息在收盘后已知 ⇒ 前向收益从 T+1 收盘起算（完全避开前视）。
    - 超额收益基准 = 同期 CSI800 池中位数前向收益（剥离市场/风格共同波动）。
    - 逐年稳定性：逐年超额均值 >0 的占比，<60% 即不稳。
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]

ALTDB = ROOT / "outputs" / "ashare_altdata.sqlite"
PXDB = ROOT / "outputs" / "ashare_csi800_hfq_xq.sqlite"
OUT_DIR = ROOT / "outputs" / "2026-09-04"


def load_close_matrix(pxdb: str, start: str = "2014-01-01"):
    c = sqlite3.connect(pxdb)
    dates = [r[0] for r in c.execute(
        "SELECT DISTINCT trade_date FROM daily_quotes_hfq WHERE trade_date>=? ORDER BY trade_date",
        (start,))]
    pos = {d: i for i, d in enumerate(dates)}
    codes = [r[0] for r in c.execute(
        "SELECT DISTINCT code FROM daily_quotes_hfq WHERE trade_date>=? ORDER BY code", (start,))]
    ci = {cd: j for j, cd in enumerate(codes)}
    T, M = len(dates), len(codes)
    close = np.full((T, M), np.nan)
    for code, d, px in c.execute(
            "SELECT code, trade_date, close FROM daily_quotes_hfq WHERE trade_date>=? "
            "ORDER BY code, trade_date", (start,)):
        if px is None or not np.isfinite(px):
            continue
        close[pos[d], ci[code]] = float(px)
    c.close()
    return dates, pos, codes, ci, close


def excess_return(close, code_idx, event_idx, h, pool_med):
    if event_idx + 1 + h >= close.shape[0]:
        return None
    p0 = close[event_idx + 1, code_idx]
    ph = close[event_idx + 1 + h, code_idx]
    if not np.isfinite(p0) or not np.isfinite(ph) or p0 <= 0:
        return None
    fwd = ph / p0 - 1.0
    bm = pool_med[event_idx]
    if not np.isfinite(bm):
        return None
    return fwd - bm


def newey_west_t(x, lags):
    x = np.asarray([v for v in x if v is not None and np.isfinite(v)], float)
    n = len(x)
    if n < 8:
        return float("nan"), n
    d = x - x.mean()
    g0 = float(d @ d) / n
    v = g0
    for L in range(1, min(lags, n - 1) + 1):
        gl = float(d[L:] @ d[:-L]) / n
        v += 2.0 * (1.0 - L / (lags + 1.0)) * gl
    return float(x.mean() / math.sqrt(max(v, 1e-18) / n)), n


def yearly(events_dates, xs):
    yr = {}
    for d, v in zip(events_dates, xs):
        if v is None or not np.isfinite(v):
            continue
        yr.setdefault(str(d)[:4], []).append(v)
    out = {y: float(np.mean(v)) for y, v in sorted(yr.items())}
    pos = sum(1 for v in out.values() if v > 0)
    return out, (pos / len(out) if out else float("nan"))


def summarize(group_xs, group_dates, h, label):
    xs = [v for v in group_xs if v is not None and np.isfinite(v)]
    if len(xs) < 30:
        return {"label": label, "n": len(xs), "note": "insufficient"}
    t, n = newey_west_t(xs, lags=max(h // 21, 2))
    yr, yr_pos = yearly(group_dates, group_xs)
    return {
        "label": label, "n": n,
        "mean_excess": round(float(np.mean(xs)), 5),
        "median_excess": round(float(np.median(xs)), 5),
        "t_nw": round(t, 3),
        "win_rate": round(float(np.mean(np.array(xs) > 0)), 4),
        "yearly_pos_share": round(yr_pos, 3),
        "yearly": yr,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", default="20,60")
    ap.add_argument("--out", default="ashare_altdata_eventstudy.json")
    args = ap.parse_args()
    horizons = [int(x) for x in args.horizons.split(",")]

    dates, pos, codes, ci, close = load_close_matrix(str(PXDB))
    T, M = close.shape
    print(f"池 {len(codes)} 只  日历 {dates[0]}~{dates[-1]}  {T} 日")

    alt = sqlite3.connect(str(ALTDB))

    # 预计算池中位数前向收益（每 h）
    pool_med = {}
    for h in horizons:
        fwd = np.full((T, M), np.nan)
        for i in range(T - 1 - h):
            p0 = close[i + 1]
            ph = close[i + 1 + h]
            with np.errstate(invalid="ignore", divide="ignore"):
                r = ph / p0 - 1.0
            fwd[i] = r
        pool_med[h] = np.nanmedian(fwd, axis=1)
    print("池中位数前向收益已预计算")

    report = {"generated_at": datetime.now().isoformat(timespec="seconds"),
              "universe": "csi800", "horizons": horizons, "sources": {}}

    def study(events, src, signal_name, h):
        """events: list[(code, event_date, is_high_flag)]"""
        high_xs, high_ds, low_xs, low_ds = [], [], [], []
        n_dropped = 0
        for code, d, flag in events:
            if code not in ci or d not in pos:
                n_dropped += 1
                continue
            idx = pos[d]
            if idx + 1 + h >= T:
                continue
            ex = excess_return(close, ci[code], idx, h, pool_med[h])
            if ex is None:
                continue
            if flag:
                high_xs.append(ex)
                high_ds.append(d)
            else:
                low_xs.append(ex)
                low_ds.append(d)
        hi = summarize(high_xs, high_ds, h, f"{signal_name}·高危组")
        lo = summarize(low_xs, low_ds, h, f"{signal_name}·对照组")
        return {"high": hi, "low": lo, "n_dropped": n_dropped}

    # ---- 1. 解禁 ----
    lift_rows = alt.execute(
        "SELECT code, free_date, free_ratio FROM lift_stage "
        "WHERE free_date<=? AND free_ratio IS NOT NULL", (dates[-1],)).fetchall()
    lift_events = [(c, d, fr >= 0.20) for c, d, fr in lift_rows
                   if 0 < fr <= 1.0]  # 丢弃 free_ratio>1 的数据错误行
    print(f"\n[解禁] 有效事件 {len(lift_events)} 条（free_ratio∈(0,1]）")

    # ---- 2. 大宗 ----
    bt_rows = alt.execute(
        "SELECT code, trade_date, premium_ratio FROM block_trade "
        "WHERE premium_ratio IS NOT NULL").fetchall()
    bt_events = [(c, d, pr <= -0.08) for c, d, pr in bt_rows]
    print(f"[大宗] 有效事件 {len(bt_events)} 条")

    # ---- 3. 龙虎榜 ----
    lhb_rows = alt.execute(
        "SELECT code, trade_date, net_amt, turnover_rate FROM lhb_daily "
        "WHERE net_amt IS NOT NULL").fetchall()
    lhb_events = [(c, d, na < 0) for c, d, na, tr in lhb_rows]
    print(f"[龙虎榜] 有效事件 {len(lhb_events)} 条（净卖出 vs 净买入）")

    for h in horizons:
        print(f"\n{'='*70}\n持有期 h={h} 日\n{'='*70}")
        for src_name, events, sig in [
            ("解禁", lift_events, "free_ratio"),
            ("大宗", bt_events, "premium_ratio"),
            ("龙虎榜", lhb_events, "net_amt"),
        ]:
            r = study(events, src_name, sig, h)
            report["sources"].setdefault(src_name, {})[str(h)] = r
            hi, lo = r["high"], r["low"]
            print(f"\n[{src_name} {sig}] h={h}  （丢弃 {r['n_dropped']} 条不在池内/无价格）")
            for tag, s in [("高危", hi), ("对照", lo)]:
                if "note" in s:
                    print(f"  {tag}: {s['note']} (n={s['n']})")
                else:
                    print(f"  {tag}: n={s['n']:>5} 均值超额={s['mean_excess']:+.4f} "
                          f"中位={s['median_excess']:+.4f} t(NW)={s['t_nw']:+.2f} "
                          f"胜率={s['win_rate']:.1%} 年正={s['yearly_pos_share']:.0%}")

    alt.close()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    op = OUT_DIR / args.out
    op.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果 → {op}")


if __name__ == "__main__":
    main()
