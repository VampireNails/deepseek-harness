# -*- coding: utf-8 -*-
"""
ashare_margin_overlay.py — 方向 2 关卡③：两融拥挤度排除 overlay 的经济可行性

预检（§14.13）已证：chg20（Δ20 日 ln 融资余额）携带独立于价量路径的**负向**预测信息。
本脚本检验其经济可行性：A 股不可做空 ⇒ 唯一可交易形态 = **拥挤排除 overlay**——
在每个再平衡日剔除 chg20 最高十分位，等权持有其余，与全持有对照。

预注册（判定前固定）：
- ★主检：chg20 + 再平衡 R=20 交易日 + 剔除顶 10%(D10) + 费 0.30%/单元 + EXCL vs EQ_ALL
  （level-matched 单侧，复用 ashare_vol_targeting.lev_matched_test）
- 次级：R=5；剔除顶 20%(D20)；费率敏感性 0.50%
- 基准样本 = chg20 可计算的两融标的（可比 A/B；刚入两融未满 20 日者两侧都不进）
- 语义预期：**降风险**（拥挤名单在下跌期更脆）而非提收益；若收益也升则如实报告。
- 对齐纪律：共同样本窗（两侧组合有效期取交集）、净费口径、信息集 t−1（chg20 用
  再平衡日收盘已披露的两融值）。

用法：python ashare_margin_overlay.py [--outdir outputs/.../margin_overlay]
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ashare_vol_targeting import lev_matched_test, perf, TRADING_DAYS  # noqa: E402
from ashare_altdata_preflight import (  # noqa: E402
    load_calendar_and_close, load_margin_map, build_factor, _f)

ALTDB = "outputs/ashare_altdata.sqlite"
PXDB = "outputs/ashare_csi800_hfq_xq.sqlite"


def chg20_value(mm: dict, p: int) -> float | None:
    if p not in mm or p - 20 not in mm:
        return None
    r0, r1 = mm[p - 20][0], mm[p][0]
    if r0 <= 0 or r1 <= 0:
        return None
    return math.log(r1) - math.log(r0)


def block_close_returns(dates, close, p0: int, p1: int, codes: list[str]) -> dict:
    """code -> p0→p1 收盘简单收益（任一端 NaN → 剔除）。"""
    out = {}
    for code in codes:
        arr = close[code]
        c0, c1 = arr[p0], arr[p1]
        if _f(c0) and _f(c1):
            out[code] = c1 / c0 - 1.0
    return out


def turnover_oneway(w_drift: dict, w_new: dict) -> float:
    """单边换手 = 0.5·Σ|w_new − w_drift|。w_drift = 上期目标权重经块内收益漂移后。"""
    keys = set(w_drift) | set(w_new)
    s = sum(abs(w_new.get(k, 0.0) - w_drift.get(k, 0.0)) for k in keys)
    return 0.5 * s


def drift_weights(w_prev: dict, rets: dict) -> dict:
    """w_prev(上期目标) × (1+r_i) 归一 → 本期再平衡前的漂移权重。"""
    num, den = {}, 0.0
    for k, w in w_prev.items():
        r = rets.get(k)
        if r is None or not math.isfinite(r):
            num[k] = 0.0          # 停牌：视作现金漂移（保守）
            continue
        num[k] = w * (1.0 + r)
        den += num[k]
    if den <= 0:
        return num
    return {k: v / den for k, v in num.items()}


def run_config(dates, close, M, cfg: dict) -> dict:
    h = cfg["R"]
    cutoff = cfg["cutoff"]
    fee = cfg["fee"]
    name = cfg["name"]
    n_d = len(dates)
    codes = sorted(close.keys())

    # 再平衡日序列（块收益在相邻再平衡日之间）
    rebal = list(range(60, n_d - 1, h))
    blocks = []          # (p_start, p_end)
    for i in range(len(rebal) - 1):
        blocks.append((rebal[i], rebal[i + 1]))

    rows = []            # 每块: {ret_eq, ret_ex, ret_top, turn_eq, turn_ex}
    w_all_prev = w_ex_prev = w_top_prev = None   # 上期目标权重（漂移后算换手）
    for p0, p1 in blocks:
        d0 = dates[p0]
        # 基准确认：当天有足够多的 chg20 可计算标的
        base = {}
        for code in codes:
            if code not in M:
                continue
            v = chg20_value(M[code], p0)
            if v is not None and _f(close[code][p0]):
                base[code] = v
        if len(base) < 100:
            w_all_prev = w_ex_prev = w_top_prev = None   # 断档重置，避免跨期错配
            continue
        # 分位阈值（顶 cutoff 分位）
        vals = sorted(base.values())
        q = vals[int(math.floor((1.0 - cutoff) * len(vals)))]
        top = {c for c, v in base.items() if v >= q}
        if len(top) < 20:
            continue
        keep = [c for c in base if c not in top]
        # 块内收益
        rets = block_close_returns(dates, close, p0, p1, codes)
        ret_eq = float(np.mean([rets[c] for c in base if c in rets])) if any(
            c in rets for c in base) else float("nan")
        ret_ex = float(np.mean([rets[c] for c in keep if c in rets])) if any(
            c in rets for c in keep) else float("nan")
        ret_top = float(np.mean([rets[c] for c in top if c in rets])) if any(
            c in rets for c in top) else float("nan")
        if not (math.isfinite(ret_eq) and math.isfinite(ret_ex)):
            continue
        # 目标权重（等权）
        w_all = {c: 1.0 / len(base) for c in base}
        w_ex = {c: 1.0 / len(keep) for c in keep}
        w_top = {c: 1.0 / len(top) for c in top}
        # 换手 = 上期目标漂移 → 本期目标；首块不计建仓
        if w_all_prev is None:
            turn_eq = turn_ex = turn_top = 0.0
        else:
            turn_eq = turnover_oneway(drift_weights(w_all_prev, rets), w_all)
            turn_ex = turnover_oneway(drift_weights(w_ex_prev, rets), w_ex)
            turn_top = turnover_oneway(drift_weights(w_top_prev, rets), w_top)
        w_all_prev, w_ex_prev, w_top_prev = w_all, w_ex, w_top
        rows.append({
            "d": dates[p1], "ret_eq": ret_eq, "ret_ex": ret_ex,
            "ret_top": ret_top,
            "turn_eq": turn_eq, "turn_ex": turn_ex, "turn_top": turn_top,
            "K_base": len(base), "K_top": len(top),
        })

    if len(rows) < 24:
        return {"name": name, "error": f"块数不足 {len(rows)}"}

    # 共同样本（全部有限）。⚠️ 检验必须用**净费**收益（与报表同口径）——
    # 用毛收益检验会无视 EXCL 更高换手的成本拖累（方向 1 bug ㉕ 复发防线）。
    r_eq = np.array([r["ret_eq"] for r in rows])
    r_ex = np.array([r["ret_ex"] for r in rows])
    r_top = np.array([r["ret_top"] for r in rows])
    t_ex = np.array([r["turn_ex"] for r in rows])
    t_eq = np.array([r["turn_eq"] for r in rows])
    years = np.array([float(r["d"][:4]) for r in rows])
    r_ex_net = r_ex - t_ex * fee
    r_eq_net = r_eq - t_eq * fee

    p_eq = perf(r_eq_net, h, 0.0, None)     # 已扣费，不再二次扣
    p_ex = perf(r_ex_net, h, 0.0, None)
    # top 组合不参与主检验（描述性）
    t_top = np.array([r["turn_top"] for r in rows])
    p_top = perf(r_top - t_top * fee, h, 0.0, None)

    test = lev_matched_test(r_ex_net, r_eq_net, h, years)
    if "error" in test:
        return {"name": name, "error": test["error"]}

    # 描述性：净差（不缩放）、风险
    py = TRADING_DAYS / h
    diff_ann = float(np.mean(r_ex_net - r_eq_net)) * py * 100.0
    return {
        "name": name, "R": h, "cutoff": cutoff, "fee": fee,
        "n_blocks": len(rows), "period": f"{rows[0]['d']}~{rows[-1]['d']}",
        "mean_K_base": float(np.mean([r["K_base"] for r in rows])),
        "eq": p_eq, "excl": p_ex, "top10": p_top,
        "turn_ann_eq": float(np.mean(t_eq)) * py,
        "turn_ann_ex": float(np.mean(t_ex)) * py,
        "diff_ann_pct_net": diff_ann,
        "test": {k: test[k] for k in
                 ("scale_a", "n", "nw_lag", "mean_diff_ann_pct", "stat",
                  "p_nw_one_sided", "mde_ann_pct", "effect_over_mde",
                  "year_positive", "year_ratio", "yearly")},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="outputs/2026-09-04/margin_overlay")
    # 2026-09-06 新增：neglist 的 claim 是「全 A 个股风险标签」，
    # 但此前只在 csi800（780 只）上复验过 —— claim 与证据不匹配。
    # 宽池雪球库就位后，可用 --price-db 在 1065 只上复验「全 A」这一 claim。
    ap.add_argument("--price-db", default=PXDB,
                    help="后复权行情库（相对工作区根或绝对路径）")
    args = ap.parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    # base 必须是【工作区根】（D:\tmp\deepseek-harness），不是脚本目录。
    # 2026-09-06 修（第廿七类）：原为 Path(__file__).resolve().parent，
    # 拼出 macro-analysis/outputs/... 不存在 → sqlite3 "unable to open database file"。
    # 之前只在 cwd=工作区根 时因 Path(PXDB).exists() 命中相对路径而侥幸能跑。
    base = Path(__file__).resolve().parents[4]
    altdb = str(base / ALTDB) if not Path(ALTDB).exists() else ALTDB
    pxdb = args.price_db
    if not Path(pxdb).exists():
        pxdb = str(base / args.price_db)

    print("[1/2] 装载价格与两融...")
    t0 = time.time()
    dates, pos, close = load_calendar_and_close(pxdb)
    M = load_margin_map(altdb, "2013-06-01", pos)
    print(f"      日历 {dates[0]}~{dates[-1]} {len(dates)} 日, 两融标的 "
          f"{len([c for c in close if c in M])} 只  耗时 {time.time()-t0:.0f}s")

    cfgs = [
        # ★ 主检
        {"name": "★主检 chg20 R=20 D10 fee0.30", "R": 20, "cutoff": 0.10, "fee": 0.0030},
        # 次级
        {"name": "次级 chg20 R=5 D10 fee0.30", "R": 5, "cutoff": 0.10, "fee": 0.0030},
        {"name": "次级 chg20 R=20 D20 fee0.30", "R": 20, "cutoff": 0.20, "fee": 0.0030},
        {"name": "敏感 chg20 R=20 D10 fee0.50", "R": 20, "cutoff": 0.10, "fee": 0.0050},
    ]
    out = []
    for cfg in cfgs:
        print(f"[2/2] {cfg['name']} ...")
        r = run_config(dates, close, M, cfg)
        out.append(r)
        if "error" in r:
            print(f"      ERROR: {r['error']}")
            continue
        t = r["test"]
        print(f"      {r['period']} {r['n_blocks']}块 K~{r['mean_K_base']:.0f} | "
              f"EQ cagr={r['eq']['cagr']:.2%} vol={r['eq']['ann_vol']:.2%} | "
              f"EXCL cagr={r['excl']['cagr']:.2%} vol={r['excl']['ann_vol']:.2%} | "
              f"TOP cagr={r['top10']['cagr']:.2%}")
        print(f"      ★EXCL vs EQ(净费): 年化差(缩放)={t['mean_diff_ann_pct']:+.2f}% "
              f"stat={t['stat']:+.2f} p={t['p_nw_one_sided']:.4f} "
              f"|效|/MDE={t['effect_over_mde']:.2f} 年正={t['year_positive']} | "
              f"净毛差={r['diff_ann_pct_net']:+.2f}%/年 | "
              f"换手 EQ={r['turn_ann_eq']:.2f}x EXCL={r['turn_ann_ex']:.2f}x")
    outpath = outdir / "margin_overlay_result.json"
    with open(outpath, "w", encoding="utf-8") as f:
        json.dump({"cfgs": out}, f, ensure_ascii=False, indent=1, default=str)
    print(f"\n结果 → {outpath}")


if __name__ == "__main__":
    main()
