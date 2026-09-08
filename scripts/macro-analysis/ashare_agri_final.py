#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A股农业池：候选策略的【参数邻域稳健性】终检 —— 防多重检验自欺。

## 背景：为什么不能只看最优参数

`ashare_agri_phase.py` 扫了 7 策略 × 4 持有期 × 6 缓冲档 × 全相位，共 168 个配置，
唯一通过相位稳健性的是：

    合成·低换手+反转   H=5   缓冲30%    IR均值 0.553 [0.397, 0.656]  sd 0.097

**但这是在 168 个配置里挑出来的最优。** 若参数空间是噪声，最好看的那个也会好看。
唯一能证伪的做法是看**邻域**：把 H、缓冲宽度、min_cross 各向左右挪一格，
如果整片邻域都为正且量级接近，说明脚下是高原不是尖峰。

## 本脚本做的事

对候选配置的邻域做全组合扫描（每个组合都跑满 H 个相位）：

    持有期 H        ∈ {4, 5, 6}         （周频附近）
    卖出阈值        ∈ {0.45, 0.50, 0.55}（缓冲 25% / 30% / 35%）
    min_cross       ∈ {20, 30, 40}
    往返成本        ∈ {0.30%, 0.50%, 0.80%}
    剔涨停          ∈ {否, 是}          ★ 见下方"已修漏洞"

= 3×3×3×3×2 = 162 个组合，每个跑 H 个相位。

## ★ 已修漏洞：缓冲回测从未剔除涨停

`ashare_agri_buffer.run_buffer` 的签名里有 `lim_per_col`，但**函数体内从未使用**
（第 74 行接收，全文再无引用）。这意味着此前所有缓冲回测都允许在涨停日买入 ——
而 A 股涨停板是买不进去的，这部分收益是**账面虚高**。

本脚本改为在 `tradable` 掩码里按板块剔除涨停（主板 10% / 创业板科创板 20%），
口径与 `ashare_agri_backtest.py` 第 476 行一致：

    tradable &= ~(np.isfinite(chg) & (chg >= lim_per_col * 0.95))

## 判定

- **高原**（可交付）：邻域通过率 ≥ 70%，且"剔涨停 + 高成本 0.80%"这一最苛刻组合仍过
- **尖峰**（不可交付）：通过率 < 50%，或最苛刻组合转负

## 用法
    python ashare_agri_final.py
    python ashare_agri_final.py --quick    # 只跑 H=5，快速复核
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
    DB, STRATEGIES, load_panel, compute_factors, build_signal, board_of, _roll_mean,
)
from ashare_agri_buffer import run_buffer  # noqa: E402

OUT_DIR = ROOT / "outputs" / datetime.now().strftime("%Y-%m-%d")

CANDIDATE = "comp_turn_mom"     # 合成·低换手+反转
BUY_THR = 0.20                  # 买入阈值固定为 Q5 边界

HOLDINGS = [4, 5, 6]
SELL_THRS = [0.45, 0.50, 0.55]  # 缓冲 25% / 30% / 35%
MIN_CROSS = [20, 30, 40]
COSTS = [0.003, 0.005, 0.008]
EXCLUDE_LIMIT = [False, True]

PASS_IR = 0.50
PASS_FRAC = 2 / 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--quick", action="store_true", help="只跑 H=5")
    ap.add_argument("--tag", default="", help="输出文件名后缀")
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

    lim_per_col = np.array([board_of(c)[1] for c in codes], dtype=float)
    is_b = np.array([c.startswith(("200", "900")) for c in codes])
    bad_mask = np.array([c in qc_bad for c in codes]) if qc_bad else np.zeros(M, bool)

    # 日涨跌幅，用于涨停判定（后复权价算涨跌幅是安全的：hfq 相邻两日比值 = 真实收益）
    chg = np.full_like(close, np.nan)
    chg[1:] = close[1:] / close[:-1] - 1.0

    factors = compute_factors(close, volume, amount, turn)

    holdings = [5] if args.quick else HOLDINGS
    comps = next(c for k, c, _ in STRATEGIES if k == CANDIDATE)
    slabel = next(l for k, _, l in STRATEGIES if k == CANDIDATE)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": f"{dates[0]} ~ {dates[-1]}",
        "candidate": {"key": CANDIDATE, "label": slabel, "components": comps},
        "grid": {"holdings": holdings, "sell_thrs": SELL_THRS, "min_cross": MIN_CROSS,
                 "costs": COSTS, "exclude_limit": EXCLUDE_LIMIT, "buy_thr": BUY_THR},
        "method": {
            "why": "168 个配置里挑出的最优值必须靠邻域检验排除多重检验假象",
            "fixed_bug": "run_buffer 的 lim_per_col 是死参数（从未使用）→ "
                         "此前缓冲回测允许涨停日买入，收益账面虚高；本脚本在 tradable 里剔除",
            "pass": f"净超额>0 且 IR>={PASS_IR} 且 分年为正>={PASS_FRAC:.0%}",
        },
        "cells": [],
    }

    print(f"\n候选: {slabel}   组件: {comps}")
    print(f"网格: H{holdings} × 缓冲{SELL_THRS} × min_cross{MIN_CROSS} "
          f"× 成本{COSTS} × 剔涨停{EXCLUDE_LIMIT}")

    cells = []
    for exlim in EXCLUDE_LIMIT:
        # ★ 可交易掩码：与 ashare_agri_backtest.py 口径一致
        tradable = (volume > 0) & np.isfinite(close)
        tradable[:, is_b] = False
        if bad_mask.any():
            tradable[:, bad_mask] = False
        a20 = _roll_mean(amount, 20)
        tradable &= np.isfinite(a20) & (a20 > 0)
        if exlim:
            tradable &= ~(np.isfinite(chg) & (chg >= lim_per_col * 0.95))
        print(f"\n{'=' * 104}")
        print(f"剔涨停 = {exlim}   可交易单元占比 {tradable.mean():.1%}")

        for mc in MIN_CROSS:
            signal = build_signal(factors, comps, tradable, mc)
            for h in holdings:
                for sthr in SELL_THRS:
                    for cost in COSTS:
                        irs, nets, oks = [], [], []
                        for ph in range(h):
                            r = run_buffer(signal, close, dates, h, cost, tradable,
                                           mc, BUY_THR, sthr, t0=ph)
                            if not r:
                                continue
                            irs.append(r["ir"])
                            nets.append(r["net_excess_ann"])
                            oks.append(r["net_excess_ann"] > 0 and r["ir"] >= PASS_IR
                                       and r["pos_years"] / max(r["n_years"], 1) >= PASS_FRAC)
                        if not irs:
                            continue
                        cells.append({
                            "exclude_limit": bool(exlim), "min_cross": mc, "h": h,
                            "sell_thr": sthr, "cost": cost,
                            "ir_mean": round(float(np.mean(irs)), 4),
                            "ir_min": round(float(np.min(irs)), 4),
                            "ir_max": round(float(np.max(irs)), 4),
                            "net_mean": round(float(np.mean(nets)), 4),
                            "n_phase": len(irs),
                            "pass_frac": round(float(np.mean(oks)), 3),
                        })

    report["cells"] = cells

    # ---- 汇总 ----
    def sub(tag, fn):
        s = [c for c in cells if fn(c)]
        if not s:
            return None
        ir = np.array([c["ir_mean"] for c in s])
        nt = np.array([c["net_mean"] for c in s])
        return {"tag": tag, "n": len(s),
                "ir_mean": round(float(ir.mean()), 4),
                "ir_median": round(float(np.median(ir)), 4),
                "ir_p10": round(float(np.percentile(ir, 10)), 4),
                "ir_min": round(float(ir.min()), 4),
                "net_mean": round(float(nt.mean()), 4),
                "pass_rate": round(float(np.mean([c["pass_frac"] >= 0.6 for c in s])), 3)}

    print(f"\n\n{'=' * 104}")
    print("【邻域汇总】—— 脚下是高原还是尖峰")
    print(f"{'切片':<28}{'组合数':>7}{'IR均值':>9}{'IR中位':>9}{'IR P10':>9}{'IR最小':>9}"
          f"{'净超额均值':>11}{'通过率':>8}")
    print("-" * 104)
    summary = []
    slices = [
        ("全部邻域", lambda c: True),
        ("★ 剔涨停（真实可成交）", lambda c: c["exclude_limit"]),
        ("★ 剔涨停 + 高成本0.80%", lambda c: c["exclude_limit"] and c["cost"] == 0.008),
        ("★ 剔涨停 + 主成本0.50%", lambda c: c["exclude_limit"] and c["cost"] == 0.005),
        ("不剔涨停（对照）", lambda c: not c["exclude_limit"]),
    ]
    for h in holdings:
        slices.append((f"  H={h}", lambda c, hh=h: c["h"] == hh))
    for sthr in SELL_THRS:
        slices.append((f"  缓冲{int((sthr - BUY_THR) * 100)}%",
                       lambda c, s=sthr: c["sell_thr"] == s))
    for tag, fn in slices:
        r = sub(tag, fn)
        if not r:
            continue
        summary.append(r)
        print(f"{tag:<28}{r['n']:>7}{r['ir_mean']:>9.3f}{r['ir_median']:>9.3f}"
              f"{r['ir_p10']:>9.3f}{r['ir_min']:>9.3f}{r['net_mean']:>10.2%}"
              f"{r['pass_rate']:>8.0%}")
    report["summary"] = summary

    # ---- 交付配置明细：H=5 / 缓冲30% / min_cross=30 / 剔涨停 ----
    # 为什么选它：主成本 0.50% 下 IR 相位均值最高（0.548），且相位达标率 80%
    CHOSEN = {"h": 5, "sell_thr": 0.50, "min_cross": 30, "exclude_limit": True}
    tradable = (volume > 0) & np.isfinite(close)
    tradable[:, is_b] = False
    if bad_mask.any():
        tradable[:, bad_mask] = False
    _a20 = _roll_mean(amount, 20)
    tradable &= np.isfinite(_a20) & (_a20 > 0)
    tradable &= ~(np.isfinite(chg) & (chg >= lim_per_col * 0.95))
    _sig = build_signal(factors, comps, tradable, CHOSEN["min_cross"])

    print(f"\n\n{'=' * 104}")
    print(f"【交付配置明细】{slabel}  H={CHOSEN['h']}  缓冲30%  min_cross=30  剔涨停=是")
    detail = {}
    for cost in COSTS:
        irs, nets, gross, tnov, cdrag, yacc = [], [], [], [], [], {}
        for ph in range(CHOSEN["h"]):
            r = run_buffer(_sig, close, dates, CHOSEN["h"], cost, tradable,
                           CHOSEN["min_cross"], BUY_THR, CHOSEN["sell_thr"], t0=ph)
            if not r:
                continue
            irs.append(r["ir"]); nets.append(r["net_excess_ann"])
            gross.append(r["gross_excess_ann"]); tnov.append(r["annual_turnover_x"])
            cdrag.append(r["cost_drag_ann"])
            for ye in r["yearly_excess"]:
                yacc.setdefault(int(ye["year"]), []).append(ye["excess_ann"])
        detail[f"{cost:.3f}"] = {
            "ir_mean": round(float(np.mean(irs)), 3),
            "ir_min": round(float(np.min(irs)), 3),
            "net_excess_ann": round(float(np.mean(nets)), 4),
            "gross_excess_ann": round(float(np.mean(gross)), 4),
            "annual_turnover_x": round(float(np.mean(tnov)), 1),
            "cost_drag_ann": round(float(np.mean(cdrag)), 4),
            "n_phase": len(irs),
            "yearly_excess_phase_avg": {str(k): round(float(np.mean(v)), 4)
                                        for k, v in sorted(yacc.items())},
            "pos_years": int(sum(1 for k, v in yacc.items() if np.mean(v) > 0)),
            "n_years": len(yacc),
        }
        d0 = detail[f"{cost:.3f}"]
        print(f"  成本 {cost:.1%}  IR {d0['ir_mean']:>6.3f} [{d0['ir_min']:>6.3f}]  "
              f"毛 {d0['gross_excess_ann']:>6.2%} → 净 {d0['net_excess_ann']:>6.2%}  "
              f"年换手 {d0['annual_turnover_x']:>4.1f}x  成本吃掉 {d0['cost_drag_ann']:>5.2%}  "
              f"分年为正 {d0['pos_years']}/{d0['n_years']}")
    print("  分年度净超额（相位平均，主成本 0.50%）：")
    ye = detail["0.005"]["yearly_excess_phase_avg"]
    for y in sorted(ye):
        bar = "█" * int(abs(ye[y]) * 200)
        print(f"    {y}  {ye[y]:>7.2%}  {'+' if ye[y] > 0 else '-'}{bar}")
    report["chosen_detail"] = {"config": CHOSEN, "by_cost": detail}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / (f"ashare_agri_final{('_' + args.tag) if args.tag else ''}.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n报告已写入: {out}")


if __name__ == "__main__":
    main()
