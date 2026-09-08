#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
equity_daily_ic_robust.py — 日频 IC 的稳健性验证（含市值/流动性中性化）

背景
----
equity_narrow_pool_power.py 发现：日频口径下 volume_ratio_20d 的 Newey-West t 达到
4.25~4.36，显著高于此前月频/低频口径下的结论（MDE 0.048、判定"功效不足"）。
期数从 ~78 增到 ~1550，MDE 下降约 4.5 倍 —— 是**频率选错**而非因子无效。

但本项目历史上已有两次"假象"教训，其中第一次就是 volume_ratio 实为市值代理。
因此本脚本的目的不是"确认"这个发现，而是**尽最大努力去证伪它**。

五道防线
--------
1. 流动性/规模中性化：逐日把因子秩对 rank(log 20日均成交量) 做回归取残差，
   重算 IC。真信号不该被规模解释掉。（无每日市值，用成交量水平作规模代理）
2. 波动中性化：再对 rank(volatility_20d) 回归，排除低波/高波异象的混淆。
3. 非重叠子样本：H 日重叠收益取 offset=0..H-1 全部子样本，报告最小 t，
   避免重叠带来的 t 值虚高。
4. 分年度稳定性：真信号不该由单一年份驱动。
5. 多重检验校正（MCC）：n_tests = 因子数 × 持有期数，阈值 t ≥ norm.ppf(1-0.05/(2n))。

另做多因子合成（等权 z-score）作为参照。

数据质量提示
------------
daily_quotes 无复权价字段，除权除息日会产生虚假负收益。港股蓝筹股息集中在
特定月份，对全池日频 IC 的影响会被稀释，但分年度结果需结合这一点解读。

用法
----
    python equity_daily_ic_robust.py
    python equity_daily_ic_robust.py --no-null   # 跳过置换检验，更快
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.stats import rankdata, norm

from equity_narrow_pool_power import (
    DB, OUT_DIR, HOLDING_PERIODS, load_panel, compute_factors,
    forward_returns, newey_west_t, _roll_mean,
)

FACTORS = [
    ("momentum_20d",     "20日动量"),
    ("momentum_60d",     "60日动量"),
    ("reversal_5d",      "5日反转"),
    ("volatility_20d",   "20日波动"),
    ("price_to_ma20",    "价格/MA20"),
    ("volume_ratio_20d", "20日量比"),
]

# 合成因子：等权 z-score（方向按预期符号）
COMPOSITE = [("volume_ratio_20d", +1), ("reversal_5d", +1), ("volatility_20d", -1)]


def _rank_std(a: np.ndarray) -> np.ndarray:
    """秩变换后标准化到均值 0、标准差 1；长度 < 2 或常数列返回 0。"""
    r = rankdata(a).astype(np.float64)
    r -= r.mean()
    s = r.std()
    return r / s if s > 0 else np.zeros_like(r)


def neutralize(y: np.ndarray, controls: list[np.ndarray]) -> np.ndarray:
    """把 y 对 [1, controls...] 做 OLS 取残差。y 与 controls 已标准化。"""
    if not controls:
        return y
    X = np.column_stack([np.ones_like(y)] + list(controls))
    try:
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return y
    return y - X @ beta


def ic_series_neutral(fmat, rmat, ctrl_mats, min_cross=20):
    """
    逐日计算：原始 IC、中性化后 IC、以及因子与每个控制变量的截面相关（诊断用）。
    ctrl_mats: [(T,M) 控制变量矩阵]
    返回 dict of arrays
    """
    out = {"ic_raw": [], "ic_neut": [], "dates": [], "widths": [], "corr_ctrl": [[] for _ in ctrl_mats]}
    for t in range(fmat.shape[0]):
        f_row, r_row = fmat[t], rmat[t]
        m = np.isfinite(f_row) & np.isfinite(r_row)
        for cm in ctrl_mats:
            m &= np.isfinite(cm[t])          # 注意：必须按当日索引，用整张矩阵会广播报错
        n = int(m.sum())
        if n < min_cross:
            continue
        f, r = f_row[m], r_row[m]
        rf, rr = _rank_std(f), _rank_std(r)
        if rf.std() == 0 or rr.std() == 0:
            continue

        out["ic_raw"].append(float((rf * rr).sum() / n))

        cts = []
        for k, cm in enumerate(ctrl_mats):
            rc = _rank_std(cm[t][m])
            cts.append(rc)
            if rc.std() > 0:
                out["corr_ctrl"][k].append(float((rf * rc).sum() / n))
            else:
                out["corr_ctrl"][k].append(np.nan)

        rf_n = neutralize(rf, cts)
        if rf_n.std() > 0:
            rn = rf_n / rf_n.std()
            out["ic_neut"].append(float((rn * rr).sum() / n))
        else:
            out["ic_neut"].append(0.0)

        out["dates"].append(t)
        out["widths"].append(n)

    return {k: (np.asarray(v) if not isinstance(v, list) or (v and isinstance(v[0], list))
                else np.asarray(v))
            for k, v in out.items()}


def nonoverlap_t(ic: np.ndarray, h: int) -> dict:
    """H 日重叠收益：取所有 offset 的非重叠子样本，返回各 offset 的 t 及最小值。"""
    ts = []
    for off in range(h):
        sub = ic[off::h]
        if len(sub) < 20:
            continue
        m, se, t = newey_west_t(sub, lags=0)   # 子样本内已非重叠，无需 NW
        ts.append(float(t))
    if not ts:
        return {"offsets": 0, "t_min": np.nan, "t_mean": np.nan, "t_max": np.nan}
    return {"offsets": len(ts), "t_min": round(min(ts), 3), "t_mean": round(float(np.mean(ts)), 3),
            "t_max": round(max(ts), 3)}


def yearly_ic(ic: np.ndarray, dates_idx: np.ndarray, all_dates: np.ndarray) -> list:
    yrs = {}
    for v, i in zip(ic, dates_idx):
        y = str(all_dates[i])[:4]
        yrs.setdefault(y, []).append(v)
    out = []
    for y in sorted(yrs):
        a = np.asarray(yrs[y])
        m, se, t = newey_west_t(a, lags=0)
        out.append({"year": y, "n": len(a), "ic_mean": round(float(m), 4),
                    "t": round(float(t), 2), "pos_share": round(float((a > 0).mean()), 3)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-cross", type=int, default=20)
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB))
    dates, tickers, close, volume = load_panel(conn)
    conn.close()
    T, M = close.shape
    print(f"面板: {T} 日 × {M} 只   {dates[0]} ~ {dates[-1]}")

    factors = compute_factors(close, volume)

    # 控制变量：规模/流动性代理 + 波动
    avg_vol20 = _roll_mean(volume, 20)
    ctrl_liq = np.where(avg_vol20 > 0, np.log(avg_vol20), np.nan)
    ctrl_vol = factors["volatility_20d"]

    # 流动性过滤：剔除成交极度稀疏的观测（20日均量低于当期 10% 分位）
    liq_ok = np.full_like(close, True, dtype=bool)
    for t in range(T):
        v = avg_vol20[t]
        ok = np.isfinite(v)
        if ok.sum() >= 20:
            thr = np.nanpercentile(v[ok], 10)
            liq_ok[t] = ok & (v >= thr)
        else:
            liq_ok[t] = ok

    n_tests = len(FACTORS) * len(HOLDING_PERIODS)
    mcc_t = float(norm.ppf(1 - 0.05 / (2 * n_tests)))
    print(f"多重检验: {n_tests} 次 → MCC 阈值 |t| ≥ {mcc_t:.3f}（未校正 1.96）\n")

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "panel": {"days": int(T), "tickers": int(M), "start": str(dates[0]), "end": str(dates[-1])},
        "mcc_threshold_t": round(mcc_t, 3),
        "n_tests": n_tests,
        "caveats": [
            "daily_quotes 无复权价，除权除息日产生虚假收益",
            "无每日市值，规模代理用 log(20日均成交量)",
            "amount 字段全 NULL，无法用成交额过滤流动性",
        ],
        "results": {},
    }

    for fkey, flabel in FACTORS:
        fmat_raw = factors[fkey].copy()
        # 波动因子自身不再作为自己的控制变量
        ctrls = [ctrl_liq] if fkey == "volatility_20d" else [ctrl_liq, ctrl_vol]
        report["results"][fkey] = {"label": flabel, "by_holding": {}}
        print(f"{'='*104}")
        print(f"【{flabel}】  控制变量: log(20日均量)" + (" + 波动" if len(ctrls) > 1 else ""))
        print(f"{'H':>3}{'原始IC':>10}{'NW-t':>8}{'中性IC':>10}{'中性NW-t':>9}"
              f"{'非重叠t_min':>12}{'非重叠t_mean':>13}{'与流动性相关':>13}{'判定':>22}")
        print("-" * 104)

        for h in HOLDING_PERIODS:
            rmat = forward_returns(close, h)
            fmat = np.where(liq_ok, fmat_raw, np.nan) if fkey == "volume_ratio_20d" else fmat_raw
            res = ic_series_neutral(fmat, rmat, ctrls, args.min_cross)
            ic_raw = res["ic_raw"]
            ic_neu = res["ic_neut"]
            if len(ic_raw) < 60:
                continue

            m_r, se_r, t_r = newey_west_t(ic_raw, lags=max(h - 1, 0))
            m_n, se_n, t_n = newey_west_t(ic_neu, lags=max(h - 1, 0))
            no = nonoverlap_t(ic_raw, h)
            no_n = nonoverlap_t(ic_neu, h)
            corr_liq = float(np.nanmean(res["corr_ctrl"][0])) if len(res["corr_ctrl"]) else np.nan

            passed = abs(t_n) >= mcc_t and abs(no_n["t_min"]) >= 1.96
            if abs(t_n) >= mcc_t and abs(no_n["t_min"]) >= 1.96:
                verdict = "★ 通过MCC+非重叠"
            elif abs(t_n) >= mcc_t:
                verdict = "MCC通过/非重叠不稳"
            elif abs(t_r) >= mcc_t and abs(t_n) < mcc_t:
                verdict = "⚠ 中性化后消失(疑似规模假象)"
            else:
                verdict = "未通过"

            print(f"{h:>3}{m_r:>10.4f}{t_r:>8.2f}{m_n:>10.4f}{t_n:>9.2f}"
                  f"{no_n['t_min']:>12.2f}{no_n['t_mean']:>13.2f}{corr_liq:>13.3f}{verdict:>22}")

            report["results"][fkey]["by_holding"][str(h)] = {
                "ic_raw": round(float(m_r), 5), "t_raw_nw": round(float(t_r), 3),
                "ic_neutral": round(float(m_n), 5), "t_neutral_nw": round(float(t_n), 3),
                "nonoverlap_t_min": no_n["t_min"], "nonoverlap_t_mean": no_n["t_mean"],
                "nonoverlap_raw_t_min": no["t_min"],
                "corr_with_liquidity": round(corr_liq, 4) if corr_liq == corr_liq else None,
                "mcc_threshold": round(mcc_t, 3),
                "verdict": verdict,
                "yearly": yearly_ic(ic_neu, res["dates"], dates),
            }

    # ---------------- 多因子合成 ----------------
    print(f"\n{'='*104}")
    print("【合成因子】等权 z-score: +量比 +5日反转 -20日波动")
    print(f"{'H':>3}{'IC':>10}{'NW-t':>8}{'非重叠t_min':>13}{'判定':>22}")
    print("-" * 104)
    comp_report = {}
    for h in HOLDING_PERIODS:
        rmat = forward_returns(close, h)
        zs, w = [], []
        for fk, sign in COMPOSITE:
            fm = factors[fk]
            z = np.full_like(fm, np.nan)
            for t in range(T):
                row = fm[t]
                m = np.isfinite(row) & liq_ok[t]
                if m.sum() >= 20:
                    z[t, m] = _rank_std(row[m]) * sign
            zs.append(z)
        comp = np.nanmean(np.stack(zs, axis=0), axis=0)
        res = ic_series_neutral(comp, rmat, [ctrl_liq], args.min_cross)
        ic = res["ic_neut"]
        if len(ic) < 60:
            continue
        m, se, t = newey_west_t(ic, lags=max(h - 1, 0))
        no = nonoverlap_t(ic, h)
        passed = abs(t) >= mcc_t and abs(no["t_min"]) >= 1.96
        verdict = "★ 通过MCC+非重叠" if passed else ("MCC通过/非重叠不稳" if abs(t) >= mcc_t else "未通过")
        print(f"{h:>3}{m:>10.4f}{t:>8.2f}{no['t_min']:>13.2f}{verdict:>22}")
        comp_report[str(h)] = {"ic": round(float(m), 5), "t_nw": round(float(t), 3),
                               "nonoverlap_t_min": no["t_min"], "verdict": verdict,
                               "yearly": yearly_ic(ic, res["dates"], dates)}
    report["composite"] = {"components": COMPOSITE, "by_holding": comp_report}

    out = OUT_DIR / "daily_ic_robust.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入: {out}")
    print(f"\n判定口径: |t_neutral| ≥ {mcc_t:.2f}(MCC) 且 非重叠子样本 |t_min| ≥ 1.96 才算「通过」")


if __name__ == "__main__":
    main()
