#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A股农业池：【换手抑制】能否把 IR 抬过 0.50 门槛。

## 为什么做这个

第三关 0/28 通过，唯一卡点是 IR。归因诊断（`ashare_agri_attrib.py`）已证明：
7/7 策略的超额 100% 来自风格倾斜，正交化后全部转负 → **换任何价量因子都无意义**，
因为所有价量因子都落在同一个 9 维风格空间里（信号 R² = 0.53~0.85）。

既然换因子无路，就只剩**改组合构建**这一条路。而 IR = 净超额 / 跟踪误差，
分子端有一个直接杠杆：**成本**。

实测成本拖累（中成本 0.50% 往返）：

    合成·低换手+反转 H=5   毛超额 11.50%  年换手 16.4x  成本吃掉 8.21%  净只剩 3.29%
    合成·低换手+反转 H=20  毛超额  7.90%  年换手  8.0x  成本吃掉 4.01%  净只剩 3.89%

**成本吃掉的比剩下的还多。** 若能把换手减半且保住毛超额：
H=20 → 净 +5.89%、IR 0.59；H=5 → 净 +7.40%、IR 0.75。双双越过 0.50。

⚠️ 这是「毛超额不变」的**乐观上界**，降换手必然伴随信号衰减。
   本脚本用真实缓冲带实现来测实际值，不做算术外推。

## 缓冲带（buffer band）机制

标准做法是在 Q5 边界外留一圈缓冲，避免边缘股票反复进出：

- **买入阈值** `buy_thr`：只有信号排名进入前 `buy_thr` 才买入
- **卖出阈值** `sell_thr`：已持有的股票要跌出前 `sell_thr` 才卖出（`sell_thr > buy_thr`）
- 缓冲宽度 = `sell_thr − buy_thr`

`sell_thr = buy_thr` 即退化为无缓冲的每期全换（等价原回测）。

## 用法
    python ashare_agri_buffer.py --min-cross 30
    python ashare_agri_buffer.py --min-cross 30 --start 2018-01-01
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]
sys.path.insert(0, str(_HERE))

from ashare_agri_backtest import (  # noqa: E402
    DB, STRATEGIES, N_QUANTILES, COST_SCENARIOS,
    load_panel, compute_factors, build_signal, board_of, fwd, _roll_mean,
)
from ashare_agri_backtest import newey_west_t  # noqa: E402

OUT_DIR = ROOT / "outputs" / datetime.now().strftime("%Y-%m-%d")

# 缓冲带扫描：(买入阈值, 卖出阈值, 标签)
BUFFERS = [
    (0.20, 0.20, "无缓冲(基线)"),
    (0.20, 0.30, "缓冲10%"),
    (0.20, 0.35, "缓冲15%"),
    (0.20, 0.40, "缓冲20%"),
    (0.20, 0.50, "缓冲30%"),
    (0.20, 0.65, "缓冲45%"),
]


def run_buffer(signal, close, dates, h, cost_rate, tradable, min_cross,
               buy_thr, sell_thr, t0=0, lim_per_col=None):
    """带缓冲带的非重叠再平衡回测。

    与 ashare_agri_backtest.run_backtest 的口径完全一致（纯多头 Q5 相对池内等权基准、
    成本 = 单边换手 × 往返费率），唯一差别是持仓选择加了缓冲带。

    ★★ lim_per_col 是【已废弃的死参数】—— 历史上它被接收但函数体内从未使用，
       导致 2026-09-02 之前所有缓冲回测都**允许在涨停日买入**，收益账面虚高
       （A 股涨停板实际买不进）。发现时 `ashare_agri_final.py` 已通过在
       `tradable` 掩码里剔除涨停修正了结论（实测影响很小：IR 0.523 → 0.511）。

       为避免再次静默失效，这里改为**显式拒绝**：剔涨停必须在传入前做进
       `tradable`，口径与 ashare_agri_backtest.py 一致：

           chg = np.full_like(close, np.nan); chg[1:] = close[1:] / close[:-1] - 1.0
           tradable &= ~(np.isfinite(chg) & (chg >= lim_per_col * 0.95))

       ⚠️ 涨停判定必须【分板块】：主板 10% / 创业板科创板 20% / 北交所 30%，
          否则会把创业板 +15% 的正常上涨误判为涨停（board_of 已实现）。
    """
    if lim_per_col is not None:
        raise ValueError(
            "run_buffer 不再接受 lim_per_col（历史死参数，从未生效）。"
            "请在调用前把涨停剔除写进 tradable 掩码，见本函数 docstring。")
    T, M = close.shape
    ret_h = fwd(close, h)
    reb = list(range(t0, T - h, h))

    w_prev = np.zeros(M)
    hold = np.zeros(M, bool)
    long_r, bench_r, turns, dts = [], [], [], []

    for t in reb:
        s_row, r_row, m0 = signal[t], ret_h[t], tradable[t]
        m = np.isfinite(s_row) & np.isfinite(r_row) & m0
        n = int(m.sum())
        if n < min_cross:
            continue
        idx = np.where(m)[0]
        sv = s_row[idx]

        # 排名分位 p：0 = 信号最高
        order = np.argsort(-sv)
        p = np.empty(n)
        p[order] = np.arange(n) / max(n - 1, 1)

        target = max(int(round(n / N_QUANTILES)), 3)

        keep = hold[idx] & (p <= sell_thr)
        cand = np.where(~hold[idx] & (p <= buy_thr))[0]
        cand = cand[np.argsort(p[cand])]

        new_hold = np.zeros(n, bool)
        new_hold[keep] = True
        room = target - int(new_hold.sum())
        if room > 0 and len(cand):
            new_hold[cand[:room]] = True
        elif room < 0:
            # 保留过多：按信号从低到高裁掉多余的（但不裁到 target 以下）
            hi = np.where(new_hold)[0]
            hi = hi[np.argsort(-p[hi])]
            new_hold[hi[:-room]] = False

        if not new_hold.any():
            continue

        w = np.zeros(M)
        w[idx[new_hold]] = 1.0 / int(new_hold.sum())
        hold = np.zeros(M, bool)
        hold[idx[new_hold]] = True

        turn = 0.5 * float(np.abs(w - w_prev).sum())
        w_prev = w

        long_r.append(float(r_row[idx][new_hold].mean()))
        bench_r.append(float(r_row[idx].mean()))
        turns.append(turn)
        dts.append(str(dates[t]))

    if len(long_r) < 30:
        return None

    L = np.asarray(long_r)
    B = np.asarray(bench_r)
    tv = np.asarray(turns)
    ppy = 243.0 / h

    net_long = L - tv * cost_rate
    exc = net_long - B
    te = float(exc.std(ddof=1)) * np.sqrt(ppy)
    ir = float(exc.mean() * ppy / te) if te > 0 else float("nan")

    yearly = {}
    for v, d in zip(exc, dts):
        yearly.setdefault(d[:4], []).append(v)
    yl = [{"year": y, "n": len(v), "excess_ann": round(float(np.mean(v)) * ppy, 4)}
          for y, v in sorted(yearly.items())]

    return {
        "gross_excess_ann": round(float((L - B).mean() * ppy), 4),
        "net_excess_ann": round(float(exc.mean() * ppy), 4),
        "ir": round(ir, 3),
        "te_ann": round(te, 4),
        "annual_turnover_x": round(float(tv.mean()) * ppy, 1),
        "cost_drag_ann": round(float(tv.mean() * cost_rate * ppy), 4),
        "avg_holdings": round(float(np.mean([
            int((w_prev > 0).sum()) for _ in [0]]) or 0), 1),
        "pos_years": sum(1 for y in yl if y["excess_ann"] > 0),
        "n_years": len(yl),
        "yearly_excess": yl,
        "n_periods": int(len(exc)),
        "start": dts[0], "end": dts[-1],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--min-cross", type=int, default=30)
    ap.add_argument("--start", default=None)
    ap.add_argument("--holding", type=int, default=0, help="0=1/5/10/20 全部")
    ap.add_argument("--tag", default="", help="输出文件名后缀（区分全样本/子样本，避免互相覆盖）")
    args = ap.parse_args()

    db = Path(args.db)
    conn = sqlite3.connect(db, timeout=30)
    try:
        qc_bad = {r[0] for r in conn.execute("SELECT code FROM hfq_qc WHERE bad = 1")}
    except sqlite3.OperationalError:
        qc_bad = set()
    dates, codes, open_, close, volume, amount, turn = load_panel(conn)
    conn.close()

    T, M = close.shape
    print(f"面板: {T} 日 × {M} 只   {dates[0]} ~ {dates[-1]}")

    lim_per_col = np.array([board_of(c)[1] for c in codes])
    tradable = (volume > 0) & np.isfinite(close)
    is_b = np.array([c.startswith(("200", "900")) for c in codes])
    tradable[:, is_b] = False
    a20 = _roll_mean(amount, 20)
    tradable &= np.isfinite(a20) & (a20 > 0)
    if qc_bad:
        tradable[:, np.array([c in qc_bad for c in codes])] = False
    # ★ 剔涨停必须在这里做（run_buffer 的 lim_per_col 已废弃为显式报错）
    #   分板块阈值 × 0.95 容差，与 ashare_agri_backtest.py / ashare_agri_final.py 一致
    chg = np.full_like(close, np.nan)
    chg[1:] = close[1:] / close[:-1] - 1.0
    tradable &= ~(np.isfinite(chg) & (chg >= lim_per_col * 0.95))
    print(f"可交易单元占比: {tradable.mean():.1%}（已剔涨停日）")

    factors = compute_factors(close, volume, amount, turn)

    t0 = 0
    if args.start:
        hit = np.where(dates >= args.start)[0]
        t0 = int(hit[0])
        print(f"起始日 {args.start} → {dates[t0]}（子样本）")

    holdings = [args.holding] if args.holding else [1, 5, 10, 20]
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": f"{dates[t0]} ~ {dates[-1]}" + (f"（--start {args.start}）" if args.start else "（全样本）"),
        "method": {
            "buffer": "买入阈值 0.20；卖出阈值 sell_thr；缓冲宽度 = sell_thr − 0.20",
            "cost": "成本 = 单边换手 × 往返费率（与原回测口径一致）",
            "why": "归因已证明换价量因子无路（正交化后 7/7 转负），只剩改组合构建；"
                   "成本吃掉的比净超额还多，是最直接的杠杆",
        },
        "strategies": {},
    }

    for skey, comps, slabel in STRATEGIES:
        signal = build_signal(factors, comps, tradable, args.min_cross)
        print(f"\n{'=' * 112}")
        print(f"【{slabel}】")
        report["strategies"][skey] = {"label": slabel, "by_holding": {}}

        for h in holdings:
            print(f"\n  --- H={h} ---")
            print(f"  {'缓冲':<12}{'年换手':>8}{'毛超额':>9}{'成本拖累':>9}"
                  f"{'净超额':>9}{'IR':>7}{'为正年':>9}{'达标':>6}")
            blk = {}
            for buy_thr, sell_thr, blabel in BUFFERS:
                r = run_buffer(signal, close, dates, h, 0.005, tradable,
                               args.min_cross, buy_thr, sell_thr, t0=t0)
                if not r:
                    continue
                ok = (r["net_excess_ann"] > 0 and r["ir"] >= 0.50
                      and r["pos_years"] / max(r["n_years"], 1) >= 2 / 3)
                blk[blabel] = {**r, "pass": bool(ok)}
                print(f"  {blabel:<12}{r['annual_turnover_x']:>7.1f}x"
                      f"{r['gross_excess_ann']:>+8.2%}{r['cost_drag_ann']:>8.2%}"
                      f"{r['net_excess_ann']:>+8.2%}{r['ir']:>7.2f}"
                      f"{r['pos_years']:>6}/{r['n_years']:<3}{'  ✓' if ok else ''}")
            report["strategies"][skey]["by_holding"][str(h)] = blk

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # ⚠️ 全样本与子样本必须带不同 --tag，否则后者会静默覆盖前者
    out = OUT_DIR / (f"ashare_agri_buffer{('_' + args.tag) if args.tag else ''}.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n报告已写入: {out}")


if __name__ == "__main__":
    main()
