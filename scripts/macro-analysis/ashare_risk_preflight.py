#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
方向 1（预测风险）功效预检 —— GO / NO-GO 闸门

【本脚本不是交付物】
  目标交付物：波动率预测 → 仓位上限建议 / 回撤预警 / 止损位（服务于投资决策辅助定位）。
  本脚本只回答关卡①（功效）：「现有数据下，波动率可预测这件事能不能被统计检出？」
  关卡②（显著性 CW/DM）与关卡③（经济可行性）待 GO 之后再做。

【为什么值得先做方向 1】
  现有 5 个价量因子全军覆没的共同原因是「信息源单一 + 池级截面排序器对单股无定义」。
  风险预测同时绕开这两条：
    - 波动率对单只股票有定义 ⇒ 不需要池级截面 ⇒ 能直接服务单只持仓；
    - 日频数据 ⇒ N 远大于宏观线的月频（宏观线败因之一是 N 被月频锁死）。

【口径】
  池    : ashare_csi800_hfq，经 ashare_hfq_access 的 QC 闸门（fail-loud）
  r_t   = log(P_t / P_{t-1})，后复权
  RV(h) = sqrt(Σ_{i=1..h} r²)，非重叠分块；未年化（年化只差常数倍，不影响相关系数）
  y     = 未来 h 块的 RV（被解释）
  x     = 过去 h 块的 RV（persistence 基准，与宏观线对标 persistence 同构）

【判据（沿用宏观线硬纪律）】
  |效应| / MDE >= 1  且  逐年为正比例 >= 60%   ⇒ GO
  N_eff 取三档估计中的**最保守**一档做判定。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[4]
assert (ROOT / "outputs").is_dir(), f"ROOT 推算错误: {ROOT}"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ashare_hfq_access import load_hfq_panel  # noqa: E402
from ashare_badj_collect import board_of  # noqa: E402

DEFAULT_DB = ROOT / "outputs" / "ashare_csi800_hfq_xq.sqlite"
OUT_DIR = ROOT / "outputs" / datetime.now().strftime("%Y-%m-%d")


def pearson(a: np.ndarray, b: np.ndarray) -> tuple[float, int]:
    m = np.isfinite(a) & np.isfinite(b)
    n = int(m.sum())
    if n < 10:
        return float("nan"), n
    x = a[m] - a[m].mean()
    y = b[m] - b[m].mean()
    sx, sy = x.std(), y.std()
    if sx <= 0 or sy <= 0:
        return float("nan"), n
    return float((x * y).mean() / (sx * sy)), n


def mde_r(n: float, alpha: float = 0.05, power: float = 0.80) -> float:
    """相关系数的 MDE（Fisher z 近似）。"""
    from scipy.stats import norm
    if not np.isfinite(n) or n <= 3:
        return float("nan")
    se = 1.0 / np.sqrt(n - 3)
    z = (norm.ppf(1 - alpha / 2) + norm.ppf(power)) * se
    return float(np.tanh(z))


def autocorrelation(x: np.ndarray, lag: int) -> float:
    """跨股票 pooled 的自相关（先按股票标准化，再 pooled）。"""
    x = x[np.isfinite(x)]
    if x.size < lag + 10:
        return float("nan")
    a, b = x[:-lag], x[lag:]
    return float(np.corrcoef(a, b)[0, 1])


def avg_pairwise_corr(Z: np.ndarray) -> float:
    """Z: (K, T) 每只股票已按时间标准化（均 0、标准差的倒数缩放）。
    利用恒等式 Σ_{i≠j} corr_ij = (‖Σ_i z_i‖²/T − K)，避免 O(K²) 显式矩阵。"""
    K, T = Z.shape
    if K < 2 or T < 3:
        return float("nan")
    S = np.nansum(Z, axis=0)
    total = float(np.nansum(S ** 2)) / T
    return float((total - K) / (K * (K - 1)))


def build_blocks(close: np.ndarray, h: int, codes: list,
                 exclude_limit: bool = False) -> tuple[np.ndarray, int]:
    """close (K, T) → RV (K, n_blocks)，非重叠 h 日已实现波动率。

    返回 (RV, 因含涨跌停而被置 NaN 的块数)。
    exclude_limit: 涨跌停会**截断**真实收益，使该日 |r| 被系统性低估 → RV 被低估。
        若 x 块与 y 块同受截断影响，会人为抬高 corr(RV_future, RV_past)。
        打开本开关后，凡含涨跌停日的块整块置 NaN（x/y 任一侧被剔则该样本对剔除）。
    """
    r = np.full_like(close, np.nan)
    r[:, 1:] = np.log(close[:, 1:] / close[:, :-1])
    # 单日 |收益| > 50% 视为数据异常（新股/复权断点），置 NaN
    bad = np.abs(r) > 0.5
    r[bad] = np.nan
    K, T = r.shape
    n_blocks = T // h
    if n_blocks < 4:
        return np.zeros((K, 0)), 0
    r = r[:, : n_blocks * h].reshape(K, n_blocks, h)

    n_lim = 0
    if exclude_limit:
        lim = np.array([board_of(c)[1] for c in codes], dtype=float)[:, None]
        with np.errstate(invalid="ignore"):
            is_limit = np.abs(r) >= (lim[:, :, None] - 0.005)
        has_limit = np.nansum(is_limit.astype(float), axis=2) >= 1
        n_lim = int(np.nansum(has_limit))

    with np.errstate(invalid="ignore"):
        rv = np.sqrt(np.nansum(r ** 2, axis=2))
    # 该块内有效天数不足一半的置 NaN
    valid = np.sum(np.isfinite(r), axis=2) >= (h // 2)
    rv[~valid] = np.nan
    if exclude_limit:
        rv[has_limit] = np.nan
    return rv, n_lim


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--since", default="2014-01-02")
    ap.add_argument("--horizons", default="5,20,60")
    ap.add_argument("--min-valid-frac", type=float, default=0.5,
                    help="一只股票至少要有多少比例的分块有效才纳入")
    ap.add_argument("--exclude-limit", action="store_true",
                    help="剔除含涨跌停日的分块（涨跌停会截断收益、压低 RV，"
                         "若 x/y 同受截断会人为抬高相关性）")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    db = Path(args.db)
    print("=" * 72)
    print("方向 1 功效预检：波动率可预测性 GO/NO-GO")
    print("=" * 72)

    panel = load_hfq_panel(db, since=args.since, apply_qc=True, drop_first_n_days=5)
    if not panel.codes:
        print("[FATAL] 面板为空")
        return 2
    print(f"库        : {db.name}")
    print(f"QC 闸门   : 剔除 {panel.n_bad} 只（表 {panel.qc_source}）"
          f" {panel.bad_codes[:5]}{'...' if panel.n_bad > 5 else ''}")
    print(f"面板      : {len(panel.codes)} 只 × {len(panel.dates)} 日 "
          f"（{panel.dates[0]} ~ {panel.dates[-1]}）")

    # ---- |r| 自相关：波动聚集的直接证据，与 h 无关，只算一次 ----
    with np.errstate(invalid="ignore", divide="ignore"):
        lg = np.log(panel.close[:, 1:] / panel.close[:, :-1])
    lg[np.abs(lg) > 0.5] = np.nan
    zs_all = (np.abs(lg) - np.nanmean(np.abs(lg), axis=1, keepdims=True)) \
        / np.nanstd(np.abs(lg), axis=1, keepdims=True)
    ac = {f"lag{l}": autocorrelation(zs_all[np.isfinite(zs_all)], l) for l in (1, 5, 20)}
    print(f"\n|r| pooled 自相关（波动聚集直接证据）:"
          f"  lag1={ac['lag1']:.4f}  lag5={ac['lag5']:.4f}  lag20={ac['lag20']:.4f}")

    results = {}
    for h in [int(x) for x in args.horizons.split(",")]:
        rv, n_lim = build_blocks(panel.close, h, panel.codes, args.exclude_limit)
        if rv.shape[1] < 8:
            print(f"\n[h={h}] 分块数不足，跳过")
            continue
        K, n_blocks = rv.shape
        ok = np.mean(np.isfinite(rv), axis=1) >= args.min_valid_frac
        rv = rv[ok]
        K_ok = int(rv.shape[0])

        x = rv[:, :-1].ravel()      # 过去（persistence 基准）
        y = rv[:, 1:].ravel()       # 未来（被解释）
        lx = np.log(rv[:, :-1]).ravel()
        ly = np.log(rv[:, 1:]).ravel()

        r_raw, n_pairs = pearson(x, y)
        r_log, _ = pearson(lx, ly)

        # ---- 残差（用 log 口径，波动率右偏）----
        s = np.isfinite(lx) & np.isfinite(ly)
        b = np.polyfit(lx[s], ly[s], 1)
        resid = np.full_like(rv[:, 1:], np.nan)
        resid[:] = ly.reshape(rv[:, 1:].shape) - (b[0] * lx.reshape(rv[:, :-1].shape) + b[1])

        # ---- 逐年（按未来块的结束年份分组）----
        block_dates = [panel.dates[min((i + 2) * h, len(panel.dates) - 1)]
                       for i in range(n_blocks - 1)]
        years = np.array([int(d[:4]) for d in block_dates])
        yearly = {}
        for yr in sorted(set(years)):
            m = years == yr
            sub_x = rv[:, :-1][:, m].ravel()
            sub_y = rv[:, 1:][:, m].ravel()
            c, _ = pearson(sub_x, sub_y)
            yearly[yr] = c
        pos_years = sum(1 for v in yearly.values() if np.isfinite(v) and v > 0)
        year_ratio = pos_years / max(1, len([v for v in yearly.values() if np.isfinite(v)]))

        # ---- N_eff 三档 ----
        n_periods = n_blocks - 1
        T = n_periods
        Z = resid.copy()
        mu = np.nanmean(Z, axis=1, keepdims=True)
        sd = np.nanstd(Z, axis=1, keepdims=True)
        sd[sd <= 0] = np.nan
        Z = (Z - mu) / sd
        Z = np.nan_to_num(Z, nan=0.0)
        rho = avg_pairwise_corr(Z)
        k_eff = K_ok / (1 + (K_ok - 1) * rho) if np.isfinite(rho) and rho > 0 else float("nan")
        n_eff_panel = n_periods * k_eff if np.isfinite(k_eff) else float("nan")

        n_effs = {
            "n_pairs_上界(完全独立)": float(n_pairs),
            "n_eff_panel(平均相关修正)": float(n_eff_panel),
            "n_periods_下界(完全相关)": float(n_periods),
        }
        n_eff_use = float(n_periods)  # 最保守
        mde = mde_r(n_eff_use)

        eff = abs(r_log)
        ratio = eff / mde if np.isfinite(mde) and mde > 0 else float("nan")
        verdict = "GO" if (np.isfinite(ratio) and ratio >= 1.0 and year_ratio >= 0.60) else "NO-GO"

        results[h] = {
            "n_stocks": K_ok, "n_blocks": n_blocks, "n_periods": n_periods,
            "blocks_dropped_limit": n_lim,
            "corr_raw": r_raw, "corr_log": r_log, "r2": r_log ** 2 if np.isfinite(r_log) else None,
            "n_eff_estimates": n_effs, "n_eff_used": n_eff_use,
            "mde_r": mde, "effect_over_mde": ratio,
            "avg_pairwise_resid_corr": rho, "k_eff": k_eff,
            "yearly_corr": {str(k): v for k, v in yearly.items()},
            "year_positive_ratio": year_ratio,
            "verdict": verdict,
        }

        print(f"\n--- h={h} 日（非重叠） ---")
        print(f"  股票 {K_ok} 只 × 期数 {n_periods}  →  pooled 样本对 {n_pairs:,}")
        if args.exclude_limit:
            print(f"  已剔含涨跌停的块: {n_lim:,} 个")
        print(f"  corr(RV_future, RV_past)      = {r_raw:.4f}")
        print(f"  corr(logRV_future, logRV_past)= {r_log:.4f}   R² = {r_log**2:.4f}")
        print(f"  残差平均横截面相关 ρ̄ = {rho:.4f}   K_eff = {k_eff:.1f} / {K_ok}")
        print(f"  N_eff 估计: 上界 {n_pairs:,.0f} | 面板 {n_eff_panel:,.0f} | 下界 {n_periods}  ← 用下界")
        print(f"  MDE(r) = {mde:.4f}   |效应|/MDE = {ratio:.2f}")
        print(f"  逐年为正 {pos_years}/{len([v for v in yearly.values() if np.isfinite(v)])}"
              f" = {year_ratio:.0%}")
        print(f"  ⇒ 判定: {verdict}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else OUT_DIR / f"risk_preflight_{db.stem}.json"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "db": str(db), "db_snapshot": db.name,
        "since": args.since,
        "exclude_limit": args.exclude_limit,
        "abs_ret_autocorr": ac,
        "qc": {"applied": True, "n_excluded": panel.n_bad, "excluded": panel.bad_codes},
        "panel": {"n_stocks": len(panel.codes), "n_days": len(panel.dates),
                  "from": panel.dates[0], "to": panel.dates[-1]},
        "horizons": {str(k): v for k, v in results.items()},
        "note": "关卡①功效预检。判据 |效应|/MDE>=1 且 逐年为正>=60%，N_eff 取最保守下界。",
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n产物: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
