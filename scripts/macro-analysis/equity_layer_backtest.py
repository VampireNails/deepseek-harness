#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
equity_layer_backtest.py — 分层多空回测 + 交易成本敏感性（经济可行性判定）

目的
----
IC 显著 ≠ 能赚钱。日频调仓的高换手会把毛收益吃光。本脚本回答：
  「扣掉真实交易成本后，还剩多少？」

设计
----
- 非重叠再平衡：每 H 个交易日调仓一次，持有 H 日（避免重叠收益导致的虚假 Sharpe）
- 五分位分层：按因子截面排名分 5 组，等权；多头 Q5 / 空头 Q1，多空组合 Q5-Q1
- 换手率：逐期跟踪权重变化，turnover = 0.5 × Σ|w_t − w_{t-1}|（单边口径）
- 成本：每期成本 = turnover × 往返成本率，测 3 档情景
    港股往返成本参考（单边）：印花税 0.13% + 交易费 0.00565% + 结算费 0.002%
                            + 交易系统使用费 + 券商佣金 0.05%~0.1%
    → 往返合计约 0.26%~0.36%，再叠加滑点。故取 0.30% / 0.50% / 0.80% 三档，
      0.80% 为保守压力情景（含流动性差的中小市值冲击成本）。
- 报告：年化收益、波动、Sharpe、最大回撤、分年度、t 统计

数据质量提示
------------
daily_quotes 无复权价 → 除权除息日会产生虚假负收益，会**低估**多头端收益。
港股蓝筹股息率 3%~6%，因此对实际结果应视为偏保守的下界。

用法
----
    python equity_layer_backtest.py
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.stats import rankdata

from equity_narrow_pool_power import (
    DB, OUT_DIR, HOLDING_PERIODS, load_panel, compute_factors, forward_returns,
    newey_west_t,
)

# 往返交易成本情景 (标签, 比率)
COST_SCENARIOS = [
    ("低 0.30%（大盘蓝筹/低滑点）", 0.0030),
    ("中 0.50%（含佣金与常规滑点）", 0.0050),
    ("高 0.80%（含冲击成本，保守压力）", 0.0080),
]

STRATEGIES = [
    ("volume_ratio_20d", [("volume_ratio_20d", +1)], "单因子·20日量比"),
    ("composite", [("volume_ratio_20d", +1), ("reversal_5d", +1), ("volatility_20d", -1)],
     "合成·量比+反转-波动"),
    ("volatility_20d", [("volatility_20d", -1)], "单因子·低波动"),
]

N_QUANTILES = 5
MIN_CROSS = 20


def build_signal(close, volume, factors, comps, liq_ok):
    """把多个因子按符号合成为截面 z-score 信号。"""
    T, M = close.shape
    zs = []
    for fk, sign in comps:
        fm = factors[fk]
        z = np.full_like(fm, np.nan)
        for t in range(T):
            row = fm[t]
            m = np.isfinite(row) & liq_ok[t]
            if m.sum() >= MIN_CROSS:
                r = rankdata(row[m]).astype(np.float64)
                r = (r - r.mean()) / (r.std() if r.std() > 0 else 1.0)
                z[t, m] = r * sign
        zs.append(z)
    return np.nanmean(np.stack(zs, axis=0), axis=0)


def run_backtest(signal, close, dates, h, cost_rate):
    """
    非重叠再平衡五分位多空回测。
    返回 dict（含毛/净收益序列、换手、分年度等）
    """
    T, M = close.shape
    ret_h = forward_returns(close, h)

    reb = list(range(0, T - h, h))
    w_prev_long = np.zeros(M)
    w_prev_short = np.zeros(M)

    gross, net, turns, dts = [], [], [], []
    q_rets = [[] for _ in range(N_QUANTILES)]

    for t in reb:
        s_row = signal[t]
        r_row = ret_h[t]
        m = np.isfinite(s_row) & np.isfinite(r_row)
        n = int(m.sum())
        if n < MIN_CROSS:
            continue
        idx = np.where(m)[0]
        sv = s_row[idx]
        rv = r_row[idx]

        order = np.argsort(sv)
        qsize = len(order) / N_QUANTILES
        cuts = [int(round(k * qsize)) for k in range(N_QUANTILES + 1)]

        for q in range(N_QUANTILES):
            sel = order[cuts[q]:cuts[q + 1]]
            if len(sel):
                q_rets[q].append(float(rv[sel].mean()))

        lo_idx = idx[order[cuts[0]:cuts[1]]]
        hi_idx = idx[order[cuts[-2]:cuts[-1]]]

        w_long = np.zeros(M)
        w_long[hi_idx] = 1.0 / len(hi_idx)
        w_short = np.zeros(M)
        w_short[lo_idx] = 1.0 / len(lo_idx)

        r_long = float(rv[order[cuts[-2]:cuts[-1]]].mean())
        r_short = float(rv[order[cuts[0]:cuts[1]]].mean())
        g = r_long - r_short

        # 换手：多空两侧合计，单边口径
        turn = 0.5 * (float(np.abs(w_long - w_prev_long).sum())
                      + float(np.abs(w_short - w_prev_short).sum()))
        w_prev_long, w_prev_short = w_long, w_short

        cost = turn * cost_rate
        gross.append(g)
        net.append(g - cost)
        turns.append(turn)
        dts.append(str(dates[t]))

    if len(gross) < 30:
        return None

    g = np.asarray(gross)
    nv = np.asarray(net)
    tv = np.asarray(turns)
    periods_per_year = 243.0 / h

    def _stats(x, label):
        m, se, t = newey_west_t(x, lags=0)
        ann_ret = m * periods_per_year
        ann_vol = float(x.std(ddof=1)) * np.sqrt(periods_per_year)
        sharpe = ann_ret / ann_vol if ann_vol > 0 else float("nan")
        eq = np.cumprod(1 + x)
        dd = float((eq / np.maximum.accumulate(eq) - 1).min())
        return {"label": label, "mean_per_period": round(float(m), 6),
                "ann_return": round(float(ann_ret), 4),
                "ann_vol": round(float(ann_vol), 4),
                "sharpe": round(float(sharpe), 3),
                "max_drawdown": round(dd, 4), "t": round(float(t), 2),
                "n_periods": int(len(x))}

    yearly = {}
    for v, d in zip(nv, dts):
        yearly.setdefault(d[:4], []).append(v)
    yl = [{"year": y, "n": len(v), "net_mean": round(float(np.mean(v)), 5)}
          for y, v in sorted(yearly.items())]

    out = {
        "gross": _stats(g, "毛收益"),
        "net": _stats(nv, "净收益"),
        "avg_turnover_per_rebalance": round(float(tv.mean()), 3),
        "annual_turnover_x": round(float(tv.mean()) * periods_per_year, 1),
        "cost_drag_ann": round(float((g.mean() - nv.mean()) * periods_per_year), 4),
        "quantile_ann_return": [round(float(np.mean(q)) * periods_per_year, 4) if q else None
                                for q in q_rets],
        "yearly_net": yl,
        "start": dts[0], "end": dts[-1],
    }
    return out


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB))
    dates, tickers, close, volume = load_panel(conn)
    conn.close()
    T, M = close.shape
    print(f"面板: {T} 日 × {M} 只   {dates[0]} ~ {dates[-1]}")

    factors = compute_factors(close, volume)
    from equity_narrow_pool_power import _roll_mean
    avg_vol20 = _roll_mean(volume, 20)
    liq_ok = np.full_like(close, True, dtype=bool)
    for t in range(T):
        v = avg_vol20[t]
        ok = np.isfinite(v)
        if ok.sum() >= MIN_CROSS:
            thr = np.nanpercentile(v[ok], 10)
            liq_ok[t] = ok & (v >= thr)
        else:
            liq_ok[t] = ok

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "panel": {"days": int(T), "tickers": int(M), "start": str(dates[0]), "end": str(dates[-1])},
        "design": {"rebalance": "non-overlapping every H trading days",
                   "quintiles": N_QUANTILES, "long_short": "Q5 - Q1, equal weight",
                   "cost_model": "cost_per_period = turnover(one-way) × round-trip rate"},
        "caveats": ["无复权价：除权除息造成虚假负收益，结果偏保守",
                    "未考虑港股卖空限制与借券成本，空头端收益可能高估"],
        "strategies": {},
    }

    for skey, comps, slabel in STRATEGIES:
        print(f"\n{'='*100}")
        print(f"【{slabel}】")
        signal = build_signal(close, volume, factors, comps, liq_ok)
        report["strategies"][skey] = {"label": slabel, "components": comps, "by_holding": {}}

        for h in HOLDING_PERIODS:
            print(f"\n  --- 持有 {h} 日（每 {h} 日调仓，非重叠）---")
            print(f"  {'成本情景':<32}{'年化毛收益':>11}{'年化净收益':>11}{'Sharpe':>9}"
                  f"{'最大回撤':>10}{'t':>8}{'年均换手(倍)':>13}")
            report["strategies"][skey]["by_holding"][str(h)] = {}
            for clabel, crate in COST_SCENARIOS:
                r = run_backtest(signal, close, dates, h, crate)
                if not r:
                    print(f"  {clabel:<32}样本不足")
                    continue
                print(f"  {clabel:<32}{r['gross']['ann_return']:>11.2%}{r['net']['ann_return']:>11.2%}"
                      f"{r['net']['sharpe']:>9.2f}{r['net']['max_drawdown']:>10.2%}"
                      f"{r['net']['t']:>8.2f}{r['annual_turnover_x']:>13.1f}")
                report["strategies"][skey]["by_holding"][str(h)][clabel] = {
                    "cost_rate": crate, **r}
            # 分五分位单调性（用中成本档）
            mid = run_backtest(signal, close, dates, h, COST_SCENARIOS[1][1])
            if mid:
                qs = mid["quantile_ann_return"]
                print(f"      五分位年化收益 Q1→Q5: " +
                      "  ".join(f"{v:+.2%}" if v is not None else "NA" for v in qs))
                mono = all(qs[i] <= qs[i + 1] + 1e-9 for i in range(len(qs) - 1)) if all(
                    v is not None for v in qs) else False
                print(f"      单调性: {'✓ 严格单调' if mono else '✗ 非单调'}")
                report["strategies"][skey]["by_holding"][str(h)]["_mid"] = {
                    "quantile_ann_return": qs, "monotonic": bool(mono),
                    "yearly_net": mid["yearly_net"]}

    out = OUT_DIR / "layer_backtest.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入: {out}")
    print("\n注：空头端未考虑港股卖空限制与借券成本，实际可行性需再打折扣。")


if __name__ == "__main__":
    main()
