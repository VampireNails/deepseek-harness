#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""宽基池（top1000）价量策略【跨池外推检验】—— 判定农业池结论是真结构还是窄池特异。

## 为什么必须做这个

农业窄池已经跑出一个通过三关的配置：

    合成·低换手+反转  H=5  缓冲30%  min_cross=30  剔涨停  成本0.50%
    → IR 均值 0.548（5 相位 min 0.391）  净超额 5.37%/年  分年为正 11/13

但归因诊断（`ashare_agri_attrib.py`）证明它的超额 **100% 来自风格倾斜**
（正交化后 7/7 策略转负）。这留下一个致命的未验命题：

    这个「周频评估 + 宽缓冲」的组合构建配置，究竟是
    (a) A 股普遍存在的市场结构效应（小盘/冷门/超跌的风险溢价 + 低换手实现方式），还是
    (b) 19 只农业股在 13 年里凑出来的窄池特异现象（= 过拟合的另一种形式）？

**同池内的任何检验都答不了这个问题**（相位、邻域、子周期都是同一批股票）。
唯一的答案在**独立截面**上 —— 这就是宽池数据的真正价值。

## ★ 硬约束：宽池没有换手率，候选策略无法直接迁移

宽池库（2026-09-06 起为雪球源 `ashare_wide_hfq_xq.sqlite`；腾讯源
`ashare_wide_hfq.sqlite` 已废弃，见 `ashare_hfq_access.DEPRECATED_SOURCES`）
只有 `daily_quotes_hfq`（date/code/open/close/volume），
没有 `daily_liquidity`（换手率来自东财，农业池是从主库复制的）。因此：

    turnover_level_20d = log(MA20(turn))   → 全 NaN，不可测
    turnover_ratio_20d                     → 全 NaN，不可测
    amihud_20d                             → amount 只能用 volume×100×close_hfq 回退，
                                             而 hfq 价/真实价的比值【逐股不同】
                                             → 截面排序被污染，不可测

⚠️⚠️ 这里埋着一个会静默出错的坑，必须显式拦掉：

`ashare_agri_backtest.build_signal` 的最后一行是

    return np.nanmean(np.stack(zs, axis=0), axis=0)

`nanmean` 遇到全 NaN 的分量会**直接忽略它**，不报错、不警告。于是
`comp_turn_mom = [低换手, 20日反转]` 在宽池上会**静默退化成单因子 20 日反转**，
跑出来的 IR 会被误读成"候选策略的跨池表现"，而实际测的是另一个策略。

本脚本的做法：对每个策略的**每个分量**单独检查覆盖率（可交易单元内的有限值占比），
任一分量低于 `MIN_COMP_COV` 就**拒绝该策略并显式打印原因**，绝不让它进入结果。

## 可测的 5 个策略（只依赖 close / volume）

    reversal_5d          单因子·5日反转
    momentum_20d         单因子·20日反转
    volatility_20d       单因子·低波动
    price_to_ma20        单因子·价格/MA20
    comp_rev_lowvol      合成·反转+低波     ← 两池都可测，最干净的跨池对比锚点

`comp_rev_lowvol` 是关键：它在农业池也测过，同配置直接可比。

## 判定逻辑

对每个可测策略，在候选配置的邻域上做全相位扫描，然后按三档结论：

  1. **结构效应**：宽池 IR 与农业池同策略同量级（差异 < 50%）且方向一致
     → 配置具有普适性，农业池结论可信度大幅提升
  2. **窄池特异**：宽池 IR 显著低于农业池（< 50%）或转负
     → 农业池的 5.37% 很可能是窄池噪声，不应交付
  3. **宽池更强**：宽池 IR 明显更高
     → 说明池子越宽越好，农业窄池本身是约束而非优势

## 用法
    python ashare_wide_extrap.py --quick        # H=5 单点，快速定性
    python ashare_wide_extrap.py                # 全邻域
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
    STRATEGIES, load_panel, compute_factors, build_signal, board_of, _roll_mean,
)
from ashare_agri_buffer import run_buffer  # noqa: E402

OUT_DIR = ROOT / "outputs" / datetime.now().strftime("%Y-%m-%d")
WIDE_DB = ROOT / "outputs" / "ashare_wide_hfq_xq.sqlite"

# ★ 2018+ 窗口内这些策略在宽池可测（派生换手率已补齐 2018+ 覆盖，见
#   ashare_wide_shares.py）。全窗口（2014+）下 comp_turn_mom / turnover_level_20d
# 仍会因 2014~2017 换手率缺失而在覆盖率闸门处被拒，属正常行为。
TESTABLE = ["reversal_5d", "momentum_20d", "volatility_20d",
            "price_to_ma20", "comp_rev_lowvol",
            "turnover_level_20d", "comp_turn_mom"]
# 历史上宽池无换手率，comp_turn_mom 不可测；现已用派生换手率补齐 2018+ 窗口，
# 故不再列入 UNTESTABLE。保留空字典以维持结构。
UNTESTABLE = {}

# ★ 分量覆盖率下限：低于此值判定该分量不可用 → 拒绝整个策略
#   （不是为了容错，是为了防 nanmean 静默退化成子集）
MIN_COMP_COV = 0.30

# 候选配置的邻域（与 ashare_agri_final.py 保持一致，保证跨池可比）
HOLDINGS = [4, 5, 6]
SELL_THRS = [0.45, 0.50, 0.55]
BUY_THR = 0.20
MIN_CROSS = [60, 100, 150]     # 宽池截面 ~900 只，min_cross 相应放大
COSTS = [0.003, 0.005, 0.008]
PASS_IR, PASS_FRAC = 0.50, 2 / 3

# 农业池同配置基准（来自 ashare_agri_final_chosen.json，成本 0.50% 主口径）
AGRI_REF = {
    "comp_turn_mom": {"ir": 0.548, "net": 0.0537, "note": "农业池候选（宽池不可测）"},
    "comp_rev_lowvol": None,   # 运行时从农业池产物读取
}


def comp_coverage(factors, comps, tradable):
    """逐分量计算「可交易单元内的有限值占比」。

    ⚠️ 必须在 tradable 内算，不能全面板算 —— 否则未上市期的 NaN 会拉低所有分量，
       看不出哪个分量是真缺失。
    """
    out = {}
    denom = float(tradable.sum())
    for fk, _sign in comps:
        fm = factors.get(fk)
        if fm is None:
            out[fk] = 0.0
            continue
        out[fk] = float((np.isfinite(fm) & tradable).sum() / denom) if denom > 0 else 0.0
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(WIDE_DB))
    ap.add_argument("--quick", action="store_true", help="只跑 H=5 / 缓冲30% / 成本0.50%")
    ap.add_argument("--min-history", type=int, default=250,
                    help="点入时上市满 N 个有效交易日（宽池必开，见 build_tradable 文档）")
    ap.add_argument("--start", default="2018-01-02",
                    help="面板起始日；派生换手率只回溯到 2018-01-02，"
                         "跨池对照必须限同一窗口，默认 2018-01-02")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    conn = sqlite3.connect(Path(args.db), timeout=30)
    try:
        qc_bad = {r[0] for r in conn.execute("SELECT code FROM hfq_qc WHERE bad = 1")}
    except sqlite3.OperationalError:
        qc_bad = set()
    # amount_fallback=True：宽池无 daily_liquidity，回退 volume×100×close
    #   只用于「是否可交易」判定，不用于 amihud 量级（后者已被排除在 TESTABLE 外）
    dates, codes, open_, close, volume, amount, turn = load_panel(conn)
    conn.close()

    # ★ 窗口切片：派生换手率只覆盖 2018-01-02 起，限窗才能给 comp_turn_mom 过
    #   覆盖率闸门；农业池也必须限同一窗口才可比（见 --start 默认值）。
    if args.start:
        keep = np.array([d >= args.start for d in dates])
        if keep.sum() == 0:
            raise SystemExit(f"--start {args.start} 无数据，请检查面板范围")
        dates = dates[keep]
        open_, close = open_[keep], close[keep]
        volume, amount, turn = volume[keep], amount[keep], turn[keep]
        print(f"（已切片面板至 {args.start} 起：{keep.sum()} 日）")

    T, M = close.shape
    print(f"宽基池面板: {T} 日 × {M} 只   {dates[0]} ~ {dates[-1]}")
    print(f"QC 异常剔除: {len(qc_bad)} 只")

    lim_per_col = np.array([board_of(c)[1] for c in codes], dtype=float)
    is_b = np.array([c.startswith(("200", "900")) for c in codes])
    bad_mask = np.array([c in qc_bad for c in codes]) if qc_bad else np.zeros(M, bool)

    chg = np.full_like(close, np.nan)
    chg[1:] = close[1:] / close[:-1] - 1.0

    factors = compute_factors(close, volume, amount, turn)

    # ---- 可交易掩码（剔涨停版，与农业池终检口径一致）----
    tradable = (volume > 0) & np.isfinite(close)
    tradable[:, is_b] = False
    if bad_mask.any():
        tradable[:, bad_mask] = False
    a20 = _roll_mean(amount, 20)
    tradable &= np.isfinite(a20) & (a20 > 0)
    tradable &= ~(np.isfinite(chg) & (chg >= lim_per_col * 0.95))
    # ★ 点入时上市过滤：东财业绩报表按公司存，"有财报"≠"当时A股可交易"
    if args.min_history > 0:
        listed = np.cumsum(np.isfinite(close), axis=0)
        tradable &= listed >= int(args.min_history)
    print(f"可交易单元占比: {tradable.mean():.1%}   "
          f"日均可交易只数中位 {np.median(tradable.sum(axis=1)):.0f}")

    # ---- 分量覆盖率闸门 ----
    print(f"\n{'=' * 96}")
    print("分量覆盖率闸门（可交易单元内的有限值占比，阈值 "
          f"{MIN_COMP_COV:.0%}）—— 防 nanmean 静默退化")
    print(f"{'策略':<20}{'分量覆盖率':<52}判定")
    print("-" * 96)
    accepted, rejected = [], {}
    for skey, comps, slabel in STRATEGIES:
        cov = comp_coverage(factors, comps, tradable)
        cov_s = "  ".join(f"{k}={v:.1%}" for k, v in cov.items())
        bad = [k for k, v in cov.items() if v < MIN_COMP_COV]
        if bad:
            rejected[skey] = {"label": slabel, "coverage": cov, "missing": bad,
                              "reason": UNTESTABLE.get(skey, "分量覆盖率不足")}
            print(f"{slabel:<20}{cov_s:<52}✗ 拒绝（{','.join(bad)} 不可用）")
        else:
            accepted.append((skey, comps, slabel))
            print(f"{slabel:<20}{cov_s:<52}✓ 可测")

    if not accepted:
        print("\n没有任何策略可测，退出。")
        return

    holdings = [5] if args.quick else HOLDINGS
    sthrs = [0.50] if args.quick else SELL_THRS
    mcs = [100] if args.quick else MIN_CROSS
    costs = [0.005] if args.quick else COSTS

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pool": "宽基 top1000（营收排序，全程存续）",
        "window": f"{dates[0]} ~ {dates[-1]}",
        "panel": {"days": T, "codes": M, "tradable_frac": round(float(tradable.mean()), 4),
                  "median_daily_tradable": int(np.median(tradable.sum(axis=1)))},
        "purpose": "判定农业池「周频+宽缓冲」配置是结构效应还是窄池特异",
        "gate": {"min_comp_coverage": MIN_COMP_COV,
                 "why": "build_signal 用 nanmean，全 NaN 分量会被静默忽略 → "
                        "合成策略会退化成子集且不报错",
                 "rejected": rejected},
        "grid": {"holdings": holdings, "sell_thrs": sthrs, "min_cross": mcs,
                 "costs": costs, "buy_thr": BUY_THR, "exclude_limit": True,
                 "min_history": args.min_history},
        "strategies": {},
    }

    for skey, comps, slabel in accepted:
        print(f"\n{'=' * 96}")
        print(f"【{slabel}】 组件: {comps}")
        cells = []
        for mc in mcs:
            signal = build_signal(factors, comps, tradable, mc)
            for h in holdings:
                for sthr in sthrs:
                    for cost in costs:
                        irs, nets, oks, turns, gross = [], [], [], [], []
                        pos_y, n_y = [], []
                        for ph in range(h):
                            r = run_buffer(signal, close, dates, h, cost, tradable,
                                           mc, BUY_THR, sthr, t0=ph)
                            if not r:
                                continue
                            irs.append(r["ir"])
                            nets.append(r["net_excess_ann"])
                            gross.append(r["gross_excess_ann"])
                            turns.append(r["annual_turnover_x"])
                            pos_y.append(r["pos_years"]); n_y.append(r["n_years"])
                            oks.append(r["net_excess_ann"] > 0 and r["ir"] >= PASS_IR
                                       and r["pos_years"] / max(r["n_years"], 1) >= PASS_FRAC)
                        if not irs:
                            continue
                        cells.append({
                            "min_cross": mc, "h": h, "sell_thr": sthr, "cost": cost,
                            "ir_mean": round(float(np.mean(irs)), 4),
                            "ir_min": round(float(np.min(irs)), 4),
                            "ir_max": round(float(np.max(irs)), 4),
                            "ir_std": round(float(np.std(irs)), 4),
                            "gross_mean": round(float(np.mean(gross)), 4),
                            "net_mean": round(float(np.mean(nets)), 4),
                            "turnover_mean": round(float(np.mean(turns)), 2),
                            "pos_years_mean": round(float(np.mean(pos_y)), 2),
                            "n_years": int(np.median(n_y)),
                            "n_phase": len(irs),
                            "pass_frac": round(float(np.mean(oks)), 3),
                        })
        report["strategies"][skey] = {"label": slabel, "components": comps, "cells": cells}

        # 主口径小结（成本 0.50%）
        mid = [c for c in cells if abs(c["cost"] - 0.005) < 1e-9]
        if mid:
            print(f"  主口径（成本0.50%）: IR均值 {np.mean([c['ir_mean'] for c in mid]):+.3f}  "
                  f"最小 {min(c['ir_min'] for c in mid):+.3f}  "
                  f"净超额均值 {np.mean([c['net_mean'] for c in mid]):+.2%}  "
                  f"毛超额均值 {np.mean([c['gross_mean'] for c in mid]):+.2%}  "
                  f"年换手 {np.mean([c['turnover_mean'] for c in mid]):.1f}x  "
                  f"通过率 {np.mean([c['pass_frac'] for c in mid]):.0%}")

    # ---- 汇总表 ----
    print(f"\n{'#' * 96}")
    print("宽基池 vs 农业池 —— 同配置对比（H=5 / 缓冲30% / 成本0.50%）")
    print(f"{'策略':<20}{'宽池IR':>9}{'宽池净超额':>12}{'宽池换手':>10}"
          f"{'宽池通过率':>12}  判定")
    print("-" * 96)
    for skey, sv in report["strategies"].items():
        pick = [c for c in sv["cells"]
                if c["h"] == 5 and abs(c["sell_thr"] - 0.50) < 1e-9
                and abs(c["cost"] - 0.005) < 1e-9]
        if not pick:
            continue
        c = max(pick, key=lambda x: x["ir_mean"])
        vd = ("可交付" if c["ir_mean"] >= PASS_IR and c["pass_frac"] >= 0.7
              else ("方向为正但强度不足" if c["net_mean"] > 0 else "无效/为负"))
        print(f"{sv['label']:<20}{c['ir_mean']:>9.3f}{c['net_mean']:>12.2%}"
              f"{c['turnover_mean']:>9.1f}x{c['pass_frac']:>12.0%}  {vd}")

    print(f"\n农业池候选基准（comp_turn_mom H=5/缓冲30%/成本0.50%，限 2018+ 同窗对照）: "
          f"IR 0.548  净超额 5.37%  —— 宽池已用派生换手率补齐 2018+，现可直接对比")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"ashare_wide_extrap{('_' + args.tag) if args.tag else ''}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n报告已写入: {out}")


if __name__ == "__main__":
    main()
