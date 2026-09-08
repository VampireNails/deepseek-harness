#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
equity_narrow_pool_power.py — 窄池（细分行业池）量化策略的功效前置验证

用途
----
在决定「选哪个细分池 / 用多长持有期 / 要不要扩池」之前，先用现有数据把功效算清楚。

核心方法：把 IC 时间序列的方差分解为两部分的和
    Var_t(IC) = sigma_true^2 + Var_noise(N)
  - sigma_true  : 真实 IC 随时间的变异（因子本身的不稳定性 + 市场状态切换）
                  ★ 扩截面无法消除，只能靠增加期数来摊薄
  - Var_noise(N): 单期截面上的抽样噪声，理论值 ≈ 1/(N-1)（Spearman 零相关近似）
                  ★ 扩截面可以按 1/sqrt(N) 降低，但摊到 MDE 上收益递减极快

关键改进（相对 equity_power_extrapolate.py）
------------------------------------------
1. Var_noise 不再用理论公式硬套，而是用**每期截面置换检验**实测：
   对每个交易日，把当期的收益向量在股票间随机打乱（破坏真实关系但保留边际分布），
   重算 IC，得到该期截面宽度下的纯噪声方差。这样可以验证 1/(N-1) 定律是否成立。
2. sigma_true 由实测 Var(IC_obs) - 实测 Var_noise 反解，若 < 0 则取 0（退化解，需在报告中标注）。
3. 支持任意 (池宽 N, 持有期 H, 历史天数 T) 组合的 MDE 外推，用于选池。

踩坑记录（务必保留）
--------------------
- derived_factors 里价格因子的 source 是 'price_computed' 而非 'derived'，
  按 source='derived' 过滤会**静默漏掉全部价格因子**（上一版脚本踩过）。
  本脚本直接从 daily_quotes 重算，绕开该问题。
- sigma_true 反解为 0 是退化解（Var(IC_obs) < Var_noise），不代表因子稳定，
  只代表「在当前截面宽度下噪声已完全淹没信号」，报告里要显式标注。
- MDE 的期数必须用**非重叠有效期数** T_eff = T_days / H。
  日频调仓 + H 日持有 → 相邻期收益重叠，直接用 T_days 会把功效高估 sqrt(H) 倍。

用法
----
    python equity_narrow_pool_power.py            # 全量跑（含置换检验，约 1-2 分钟）
    python equity_narrow_pool_power.py --reps 10  # 降低置换次数快速试跑
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.stats import rankdata, norm

# ---------------------------------------------------------------- 路径

_HERE = Path(__file__).resolve().parent
# .../my-deepseek-harness/deepseek-harness/scripts/macro-analysis -> D:/tmp/deepseek-harness
ROOT = _HERE.parents[3]
DB = ROOT / "outputs" / "equity_fundamental.sqlite"
OUT_DIR = ROOT / "outputs" / "2026-09-02"

# ---------------------------------------------------------------- 参数

HOLDING_PERIODS = [1, 5, 10, 20]
MIN_CROSS = 20          # 单期截面最小股票数（低于此不计算 IC）
Z_ALPHA = norm.ppf(0.975)     # 1.96
Z_POWER = norm.ppf(0.80)      # 0.8416
Z_SUM = Z_ALPHA + Z_POWER     # 2.80

# 候选池（name, 标的数量, 可用历史交易日数, 备注）
# ⚠️ A 股各池的标的数量为估算值，落地前必须用申万/东财行业成分股接口核定
CANDIDATE_POOLS = [
    ("港股·信息技术(现状最大细分)",      9, 1560, "sector_map 实测，无农业板块"),
    ("港股·金融",                        8, 1560, "sector_map 实测"),
    ("港股·全池",                      110, 1730, "daily_quotes 实测"),
    ("A股·生猪产业链(预估)",             25, 2430, "养殖+饲料+动保，需核定"),
    ("A股·申万农林牧渔(预估)",           90, 2430, "申万一级，需核定"),
    ("A股·全市场",                    5000, 2430, "参照组"),
]

FACTOR_DEFS = [
    ("momentum_20d",      "20日动量"),
    ("momentum_60d",      "60日动量"),
    ("reversal_5d",       "5日反转"),
    ("volatility_20d",    "20日波动"),
    ("price_to_ma20",     "价格/MA20"),
    ("volume_ratio_20d",  "20日量比"),
]


# ---------------------------------------------------------------- 数据加载

def load_panel(conn: sqlite3.Connection):
    """返回 (dates, tickers, close(T,M), volume(T,M))，缺失为 NaN。"""
    rows = conn.execute(
        "SELECT quote_date, ticker, close, volume FROM daily_quotes "
        "WHERE close IS NOT NULL ORDER BY quote_date, ticker"
    ).fetchall()
    if not rows:
        raise SystemExit("daily_quotes 为空")

    dates = sorted({r[0] for r in rows})
    tickers = sorted({r[1] for r in rows})
    di = {d: i for i, d in enumerate(dates)}
    ti = {t: j for j, t in enumerate(tickers)}

    close = np.full((len(dates), len(tickers)), np.nan)
    volume = np.full((len(dates), len(tickers)), np.nan)
    for d, t, c, v in rows:
        i, j = di[d], ti[t]
        close[i, j] = float(c) if c is not None else np.nan
        volume[i, j] = float(v) if v is not None else np.nan

    return np.array(dates), np.array(tickers), close, volume


# ---------------------------------------------------------------- 因子

def _shift_div(cur: np.ndarray, lag: int) -> np.ndarray:
    """cur[t] / cur[t-lag] - 1，前 lag 行为 NaN。"""
    out = np.full_like(cur, np.nan)
    if cur.shape[0] > lag:
        out[lag:] = cur[lag:] / cur[:-lag] - 1.0
    return out


def _roll_mean(a: np.ndarray, w: int) -> np.ndarray:
    out = np.full_like(a, np.nan)
    cs = np.nancumsum(np.where(np.isnan(a), 0.0, a), axis=0)
    cnt = np.cumsum(~np.isnan(a), axis=0).astype(float)
    out[w - 1:] = (cs[w - 1:] - np.concatenate([np.zeros((1, a.shape[1])), cs[:-w]], axis=0))
    denom = (cnt[w - 1:] - np.concatenate([np.zeros((1, a.shape[1])), cnt[:-w]], axis=0))
    out[w - 1:] = np.where(denom > 0, out[w - 1:] / np.where(denom > 0, denom, 1.0), np.nan)
    # 窗口内含 NaN 的一律置 NaN（避免 nancumsum 把缺失当 0 拉低均值）
    valid = (denom == w)
    out[w - 1:] = np.where(valid, out[w - 1:], np.nan)
    return out


def compute_factors(close: np.ndarray, volume: np.ndarray) -> dict:
    f = {}
    f["momentum_20d"] = _shift_div(close, 20)
    f["momentum_60d"] = _shift_div(close, 60)
    r5 = _shift_div(close, 5)
    f["reversal_5d"] = -r5

    ret1 = _shift_div(close, 1)
    sq = ret1 ** 2
    var20 = _roll_mean(sq, 20) - _roll_mean(ret1, 20) ** 2
    f["volatility_20d"] = np.sqrt(np.where(var20 > 0, var20, np.nan))

    f["price_to_ma20"] = close / _roll_mean(close, 20) - 1.0

    v20 = _roll_mean(volume, 20)
    v20_prev = np.full_like(volume, np.nan)
    v20_prev[20:] = v20[:-20]
    f["volume_ratio_20d"] = np.where(v20_prev > 0, v20 / np.where(v20_prev > 0, v20_prev, 1.0), np.nan)
    return f


def forward_returns(close: np.ndarray, h: int) -> np.ndarray:
    out = np.full_like(close, np.nan)
    if close.shape[0] > h:
        out[:-h] = close[h:] / close[:-h] - 1.0
    return out


# ---------------------------------------------------------------- IC 与置换检验

def _rank_pearson(x: np.ndarray, y: np.ndarray) -> float:
    rx = rankdata(x)
    ry = rankdata(y)
    rx = rx - rx.mean()
    ry = ry - ry.mean()
    den = float(np.sqrt((rx * rx).sum() * (ry * ry).sum()))
    return 0.0 if den == 0 else float((rx * ry).sum() / den)


def ic_series_with_null(fmat: np.ndarray, rmat: np.ndarray, rng: np.random.Generator,
                        reps: int, min_cross: int):
    """
    逐日计算 Spearman IC，同时用截面置换估计纯噪声方差。

    返回 (ic_obs, dates_idx, cross_widths, noise_var_mean, noise_var_theory_mean)
      - ic_obs      : 每个有效交易日的实测 IC
      - cross_widths: 该日的截面宽度
      - noise_var_* : 置换得到的噪声方差 / 理论 1/(N-1) 的日期均值

    性能要点
    --------
    rankdata(permutation(y)) 与 permutation(rankdata(y)) 同分布 —— 因此内层
    循环**不需要重新排序**，只需置换已算好的秩向量再做点积，可整批向量化。
    朴素实现（每次置换都调 rankdata）在此数据规模下会被超时杀掉。
    """
    valid_rows = []
    for t in range(fmat.shape[0]):
        m = np.isfinite(fmat[t]) & np.isfinite(rmat[t])
        if int(m.sum()) >= min_cross:
            valid_rows.append((t, m))

    n_rows = len(valid_rows)
    ic_obs = np.empty(n_rows)
    widths = np.empty(n_rows, dtype=int)
    idx = np.empty(n_rows, dtype=int)
    null_vars = np.empty(n_rows)
    theory_vars = np.empty(n_rows)

    for k, (t, m) in enumerate(valid_rows):
        x = fmat[t][m]
        y = rmat[t][m]
        n = x.size

        rx = rankdata(x).astype(np.float64)
        rx -= rx.mean()
        rx_norm = float(np.sqrt((rx * rx).sum()))

        ry = rankdata(y).astype(np.float64)
        ry -= ry.mean()
        ry_norm = float(np.sqrt((ry * ry).sum()))

        ic_obs[k] = float(rx @ ry) / (rx_norm * ry_norm) if rx_norm and ry_norm else 0.0
        idx[k] = t
        widths[k] = n

        # 置换检验：直接置换秩向量（与置换原始值再排序同分布）
        if rx_norm > 0:
            R = np.array([rng.permutation(ry) for _ in range(reps)])  # (reps, n)
            R -= R.mean(axis=1, keepdims=True)
            R_norm = np.sqrt((R * R).sum(axis=1))
            safe = R_norm > 0
            dots = R[safe] @ rx
            ic_null = np.zeros(reps)
            ic_null[safe] = dots / (R_norm[safe] * rx_norm)
            # 置换分布以 0 为中心 → 二阶矩即噪声方差
            null_vars[k] = float((ic_null ** 2).mean())
        else:
            null_vars[k] = np.nan
        theory_vars[k] = 1.0 / (n - 1)

    good = np.isfinite(null_vars)
    return (ic_obs, idx, widths,
            float(np.mean(null_vars[good])) if good.any() else np.nan,
            float(np.mean(theory_vars)) if n_rows else np.nan)


def newey_west_t(x: np.ndarray, lags: int) -> tuple:
    """Newey-West (Bartlett 核) 均值 t 检验。返回 (mean, se, t)。"""
    n = len(x)
    if n < 5:
        return float(np.mean(x)), np.nan, np.nan
    m = float(x.mean())
    e = x - m
    g0 = float((e * e).sum()) / n
    s = g0
    for l in range(1, min(lags, n - 1) + 1):
        gl = float((e[l:] * e[:-l]).sum()) / n
        s += 2.0 * (1.0 - l / (lags + 1.0)) * gl
    var_m = max(s / n, 1e-18)
    se = float(np.sqrt(var_m))
    return m, se, m / se


# ---------------------------------------------------------------- MDE

def mde_of(sigma_true: float, n_cross: int, t_days: int, h: int) -> dict:
    """给定 sigma_true 与配置，返回 MDE 及其构成。"""
    var_noise = 1.0 / (n_cross - 1)
    sigma_obs = float(np.sqrt(sigma_true ** 2 + var_noise))
    t_eff = t_days / h                      # 非重叠有效期数
    se_mean = sigma_obs / np.sqrt(t_eff)
    mde = Z_SUM * se_mean
    return {
        "n_cross": n_cross,
        "holding_days": h,
        "t_days": t_days,
        "t_eff": round(t_eff, 1),
        "var_noise": round(var_noise, 5),
        "sigma_obs": round(sigma_obs, 4),
        "se_mean_ic": round(float(se_mean), 5),
        "mde": round(float(mde), 4),
    }


def verdict_for(ic: float, mde: float) -> str:
    a = abs(ic)
    if a < 1e-12:
        return "无观测"
    if a < mde:
        return "功效不足（观测 < MDE，无法区分于 0）"
    if a < 2 * mde:
        return "弱可检出（观测 ≥ MDE，效应量小）"
    return "可检出（观测 ≥ 2×MDE）"


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=40, help="每个交易日的置换次数")
    ap.add_argument("--min-cross", type=int, default=MIN_CROSS)
    args = ap.parse_args()

    if not DB.exists():
        raise SystemExit(f"数据库不存在: {DB}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB))
    dates, tickers, close, volume = load_panel(conn)
    conn.close()
    T, M = close.shape
    print(f"面板: {T} 个交易日 × {M} 只标的   区间 {dates[0]} ~ {dates[-1]}")

    factors = compute_factors(close, volume)
    rng = np.random.default_rng(20260902)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "panel": {"days": int(T), "tickers": int(M),
                  "start": str(dates[0]), "end": str(dates[-1])},
        "method": {
            "decomposition": "Var_t(IC) = sigma_true^2 + Var_noise(N)",
            "var_noise_source": "per-date cross-sectional permutation test (measured, not assumed)",
            "t_eff": "T_days / H  (non-overlapping effective periods)",
            "z_sum": round(Z_SUM, 4),
            "reps": args.reps,
        },
        "factors": {},
        "pool_matrix": [],
    }

    print(f"\n置换次数/期 = {args.reps}，最小截面 = {args.min_cross}\n")
    hdr = f"{'因子':<16}{'H':>4}{'期数':>7}{'截面':>7}{'IC均值':>9}{'NW-t':>8}{'σ_obs':>8}{'噪声(实测)':>11}{'噪声(理论)':>11}{'σ_true':>8}{'MDE':>8}"
    print(hdr)
    print("-" * len(hdr))

    for fkey, flabel in FACTOR_DEFS:
        fmat = factors[fkey]
        report["factors"][fkey] = {"label": flabel, "by_holding": {}}
        for h in HOLDING_PERIODS:
            rmat = forward_returns(close, h)
            ic, idx, widths, nv_meas, nv_theory = ic_series_with_null(
                fmat, rmat, rng, args.reps, args.min_cross)
            if len(ic) < 30:
                continue

            ic_centered = ic - ic.mean()
            var_obs = float((ic_centered ** 2).sum() / (len(ic) - 1))
            sigma_true = float(np.sqrt(max(var_obs - nv_meas, 0.0)))
            degenerate = var_obs < nv_meas

            mean_ic, se_ic, t_nw = newey_west_t(ic, lags=max(h - 1, 0))
            mde_here = mde_of(sigma_true, int(np.median(widths)), T, h)

            line = (f"{flabel:<16}{h:>4}{len(ic):>7}{int(np.median(widths)):>7}"
                    f"{mean_ic:>9.4f}{t_nw:>8.2f}{np.sqrt(var_obs):>8.4f}"
                    f"{nv_meas:>11.4f}{nv_theory:>11.4f}{sigma_true:>8.4f}"
                    f"{mde_here['mde']:>8.4f}")
            print(line)

            report["factors"][fkey]["by_holding"][str(h)] = {
                "n_periods": int(len(ic)),
                "median_cross": int(np.median(widths)),
                "ic_mean": round(mean_ic, 5),
                "ic_nw_t": round(float(t_nw), 3),
                "ic_nw_se": round(float(se_ic), 5),
                "var_obs": round(var_obs, 5),
                "var_noise_measured": round(nv_meas, 5),
                "var_noise_theory_1_over_nm1": round(nv_theory, 5),
                "noise_law_ratio_meas_over_theory": round(nv_meas / nv_theory, 3) if nv_theory else None,
                "sigma_true": round(sigma_true, 5),
                "sigma_true_degenerate": bool(degenerate),
                "mde_current_pool": mde_here,
                "verdict": verdict_for(mean_ic, mde_here["mde"]),
            }
        # 用 H=5 的 sigma_true 作为该因子的代表值（日频主力口径）
        h5 = report["factors"][fkey]["by_holding"].get("5")
        report["factors"][fkey]["sigma_true_ref"] = h5["sigma_true"] if h5 else None

    # ------------------------------------------------- 候选池 MDE 矩阵
    print("\n" + "=" * 96)
    print("候选池 × 持有期 的 MDE 矩阵（sigma_true 取各因子 H=5 的中位数）")
    print("=" * 96)

    st_vals = [v["sigma_true_ref"] for v in report["factors"].values()
               if v.get("sigma_true_ref") is not None]
    st_med = float(np.median(st_vals)) if st_vals else 0.0
    st_min = float(np.min(st_vals)) if st_vals else 0.0
    st_max = float(np.max(st_vals)) if st_vals else 0.0
    print(f"sigma_true 参考值: 中位 {st_med:.4f}  区间 [{st_min:.4f}, {st_max:.4f}]")

    print(f"\n{'候选池':<28}{'N':>6}{'T(日)':>7}{'H':>5}{'T_eff':>8}{'MDE(中位σ)':>12}{'MDE(乐观σ)':>12}{'MDE(悲观σ)':>12}")
    print("-" * 96)
    for name, n, tdays, note in CANDIDATE_POOLS:
        for h in HOLDING_PERIODS:
            a = mde_of(st_med, n, tdays, h)["mde"]
            b = mde_of(st_min, n, tdays, h)["mde"]
            c = mde_of(st_max, n, tdays, h)["mde"]
            print(f"{name:<28}{n:>6}{tdays:>7}{h:>5}{tdays/h:>8.0f}{a:>12.4f}{b:>12.4f}{c:>12.4f}")
            report["pool_matrix"].append({
                "pool": name, "n": n, "t_days": tdays, "holding_days": h,
                "t_eff": round(tdays / h, 1),
                "mde_median_sigma": a, "mde_optimistic": b, "mde_pessimistic": c,
                "note": note,
            })
        print("-" * 96)

    # ------------------------------------------------- 池宽的边际收益
    print("\n" + "=" * 96)
    print("扩截面的边际收益（H=5，T=2430 日 ≈ 10 年）—— 检验「要不要扩池」")
    print("=" * 96)
    print(f"{'池宽 N':>8}{'Var_noise':>12}{'σ_obs':>10}{'MDE':>10}{'相对 N=25 的改善':>18}")
    base = None
    curve = []
    for n in [9, 15, 25, 50, 90, 200, 500, 1000, 5000]:
        m = mde_of(st_med, n, 2430, 5)
        if n == 25:
            base = m["mde"]
        imp = (1 - m["mde"] / base) * 100 if base else 0.0
        print(f"{n:>8}{m['var_noise']:>12.5f}{m['sigma_obs']:>10.4f}{m['mde']:>10.4f}{imp:>17.1f}%")
        curve.append({"n": n, **m, "improve_vs_25": round(imp, 1)})
    report["width_curve"] = {"holding_days": 5, "t_days": 2430,
                             "sigma_true_used": round(st_med, 5), "rows": curve}

    out = OUT_DIR / "narrow_pool_power.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入: {out}")


if __name__ == "__main__":
    main()
