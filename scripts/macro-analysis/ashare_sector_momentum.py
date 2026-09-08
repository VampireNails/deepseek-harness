# -*- coding: utf-8 -*-
"""申万二级行业【自身动量】截面 IC 检验 —— ETF/板块配置方向的第①②关。

背景（2026-09-05 用户提「板块配置 / ETF 方向可深挖」）：
    「板块配置」须拆成两条路径，切勿混为一谈：
      A. 宏观因子 → 行业轮动（PPI surprise / 厄尔尼诺 → 行业排序）
         —— **已判死**：macro_sector_rotation.py 完整跑过三关，IC +0.0065（预期 0.04）、
            t=0.34、p=0.74、逐年 3/9=33% ⇒ 不可交付（见 rotation_result.json）。
      B. 行业【自身】动量/反转（行业过去 N 月收益 → 未来收益，纯价量，不靠宏观）
         —— **从未检验**。本脚本做这条。它不同于个股价量（个股价量源已证伪），
            因为行业层面有聚合效应、噪声更低，是否复活是经验问题，必须实测。

    数据零采集成本：复用 ashare_sw_industry.py 已构建的 industry_monthly_panel.json
    （K=72 申万二级行业 × N=152 月，2014-02~2026-09）。

    纪律：换目标 ≠ 换因子重挖。这是「个股 → 行业」的换目标检验；若仍负，彻底收手。

    因子：mom1/mom3/mom6/mom12（过去 1/3/6/12 月累计收益）
    目标：未来 1/3 月收益
    评价：截面 spearman IC + NW + 逐年为正占比（<60% 不可交付）。
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy import stats

_HERE = Path(__file__).resolve().parent
# 工作区根 = _HERE.parents[3]（固定写法，与项目其余脚本一致）。
# 禁用「就近查找含 outputs/ 的父目录」启发式 —— 第廿七类 b 静默 bug：
# 该启发式会命中 my-deepseek-harness/ 下的残留 outputs 目录 ⇒ 路径静默错拼。
ROOT = _HERE.parents[3]

PANEL_JSON = ROOT / "outputs" / "2026-09-03" / "sector_timing" / "industry_monthly_panel.json"
OUT_DIR = ROOT / "outputs" / "2026-09-05"
MIN_K_IC = 30          # 当月有效行业数下限
NW_LAG = 3


def nw_t(x, lags=3):
    """Newey-West HAC t 统计量（截断 lag）。"""
    x = np.asarray(x, float)
    n = len(x)
    if n < 4:
        return 0.0, 1.0
    mu = x.mean()
    e = x - mu
    v = (e @ e) / n
    for lag in range(1, lags + 1):
        w = 1.0 - lag / (lags + 1)
        v += 2 * w * (e[lag:] @ e[:-lag]) / n
    se = math.sqrt(v / n) if v > 0 else 1e-12
    t = mu / se
    p = 2 * (1 - stats.t.cdf(abs(t), n - 1))
    return float(t), float(p)


def cum_ret(col, start, end):
    """从 start 到 end 的累计复合收益（月度简单收益），忽略 NaN。"""
    seg = col[start:end]
    seg = seg[~np.isnan(seg)]
    if len(seg) == 0:
        return np.nan
    return float(np.prod(1.0 + seg) - 1.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mom", default="1,3,6,12")
    ap.add_argument("--fwd", default="1,3")
    ap.add_argument("--min-k", type=int, default=MIN_K_IC)
    ap.add_argument("--out", default="ashare_sector_momentum.json")
    args = ap.parse_args()
    moms = [int(x) for x in args.mom.split(",")]
    fwds = [int(x) for x in args.fwd.split(",")]

    d = json.loads(PANEL_JSON.read_text(encoding="utf-8"))
    months = d["months"]
    panel = d["panel"]
    inds = list(panel.keys())
    N, K = len(months), len(inds)
    R = np.full((N, K), np.nan)
    for j, ind in enumerate(inds):
        for i, m in enumerate(months):
            v = panel[ind].get(m)
            if v is not None:
                R[i, j] = float(v)
    print("=" * 78)
    print("申万二级行业【自身动量】截面 IC 检验 —— ETF/板块配置方向（路径 B）")
    print("=" * 78)
    print(f"行业面板 K={K}  N={N} 月（{months[0]}~{months[-1]}）")
    print(f"有效单元比例 {np.isfinite(R).mean():.1%}")
    print(f"  {'组合':<10}{'N_ic':>5}{'IC均值':>9}{'σ_true':>8}{'MDE':>8}"
          f"{'|IC|/MDE':>10}{'t(NW)':>8}{'p':>7}{'逐年':>7}")

    rows_out = []
    for h in fwds:
        for m in moms:
            ics, ks = [], []
            for t in range(m, N - h):
                mom = np.full(K, np.nan)
                fwd = np.full(K, np.nan)
                for j in range(K):
                    mom[j] = cum_ret(R[:, j], t - m, t)
                    fwd[j] = cum_ret(R[:, j], t, t + h)
                valid = np.isfinite(mom) & np.isfinite(fwd)
                k = int(valid.sum())
                if k < args.min_k:
                    continue
                ic = stats.spearmanr(mom[valid], fwd[valid]).statistic
                ics.append(ic)
                ks.append(k)
            if len(ics) < 12:
                print(f"  mom{m:>2}→fwd{h:<2}  观测不足（{len(ics)}）")
                continue
            ics = np.array(ics)
            ic_mean = float(ics.mean())
            ic_sd = float(ics.std(ddof=1))
            var_noise = 1.0 / (np.median(ks) - 1)
            sigma_true = math.sqrt(max(ic_sd ** 2 - var_noise, 0.0))
            mde = 2.8006 * ic_sd / math.sqrt(len(ics))
            t, p = nw_t(ics, NW_LAG)
            ratio = abs(ic_mean) / mde if mde > 0 else float("nan")
            yrs = {}
            for i, ic in enumerate(ics):
                yrs[months[m + i][:4]] = yrs.get(months[m + i][:4], []) + [ic]
            pos = sum(1 for y in yrs if np.mean(yrs[y]) > 0)
            pos_ratio = pos / len(yrs) if yrs else 0.0
            verdict = "不可交付" if pos_ratio < 0.6 else "待三关③"
            print(f"  mom{m:>2}→fwd{h:<2}  {len(ics):>5}{ic_mean:>+9.4f}{sigma_true:>8.4f}"
                  f"{mde:>8.4f}{ratio:>10.2f}{t:>+8.2f}{p:>7.3f}"
                  f"{pos}/{len(yrs)}={pos_ratio:.0%}")
            rows_out.append({
                "mom_months": m, "fwd_months": h,
                "n_ic": int(len(ics)), "k_median": float(np.median(ks)),
                "ic_mean": round(ic_mean, 5), "sigma_true": round(sigma_true, 5),
                "mde": round(mde, 5), "ratio_ic_mde": round(ratio, 3),
                "t_nw": round(t, 3), "p_nw": round(p, 4),
                "pos_year_ratio": round(pos_ratio, 3),
                "verdict": verdict,
                "ic_series": [round(float(v), 5) for v in ics],
            })
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "K_industries": K, "N_months": N,
        "note": "行业自身动量（不靠宏观因子）截面 IC。板块配置路径 B，区别于已判死的路径 A（宏观→行业轮动）。",
        "results": rows_out,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    op = OUT_DIR / args.out
    op.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出: {op}")


if __name__ == "__main__":
    main()
