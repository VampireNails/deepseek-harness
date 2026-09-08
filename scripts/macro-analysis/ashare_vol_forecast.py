#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
方向 1 · 关卡②：波动率预测模型 vs persistence 的样本外检验

【前置】关卡①已 GO（SOP §14.2）：波动率在 A 股上功效充足（|效应|/MDE 1.87~5.22）。
【本脚本回答】真正的增量：模型能否**显著打败 persistence 基准**？
   预检里的 x 就是 persistence 本身，corr 0.73 是"波动率持续性"这个教科书事实的
   功效确认，**不是增量**。增量必须靠本脚本测定。

【统计量同源复用宏观线已验证实现】
   macro_predict.{dm_test, nw_var, mde_of, ridge_fit, ridge_pred}
   —— 不重写统计量。DM 的 se=sqrt(γ₀/n) + HLN 校正、ridge 的训练窗内 z-score 两条硬纪律。
   CW 在本脚本内实现（3 行），原因见 run_horizon 注释：聚合必须在平方之前完成，
   直接调 clark_west 会变成 (mean ŷ_p − mean ŷ_m)²，与所需的 mean((ŷ_p−ŷ_m)²) 不同。

【推断口径（本脚本最关键的设计）】
   个股误差横截面高度相关（关卡① ρ̄=0.17~0.32 ⇒ K_eff ≈ 4 / 636）
   ⇒ **不对 pooled 样本对做检验**（那会把 n 虚增约 100 倍、严重高估显著性），
      而是把每期的横截面均值损失差聚合成一条时序 {d_t}，再对这条时序做 DM/CW（n = 期数）。
   与关卡①「N_eff 取最保守下界 = 期数」同口径。

【QLIKE 口径（2026-09-04 由最小对照实验订正，见 outputs/2026-09-04/_verify/qlike_check.py）】
  ⚠️ 曾用 q = log(v̂²) + v²/v̂²，它与标准 QLIKE **只差一个仅依赖已实现值的平移项**
     （q = QLIKE_std + log(v²) + 1）⇒ 差值可用，但**取值可为负**，
     而削减率 1 − mean_m/mean_p 只在损失为正时可读 ⇒ 实测把真实的 +27% 算成 −7.5%。
  ✔ 改用标准非负形式 QLIKE = v²/v̂² − log(v²/v̂²) − 1（≥0，等号 iff v̂²=v²）。
  ⚠️ log 空间建模的偏差修正指数：QLIKE 的最优预测是 E[RV²|info] = exp(2ŷ + **2**s²)。
     曾用 corr=exp(s²/2)、corr²=exp(s²) ⇒ k=1（欠修正）；数值扫描确认最优 k=2.0，
     且 k=1 会**夸大**残差更小那个模型的 QLIKE 优势 ⇒ 改为 exp(2ŷ + 2s²)。

【判据（沿用宏观线 §12 硬纪律）】
   CW p<=0.05  且  NW-DM p<=0.10  且  |效应|/MDE>=1  且  逐年为正>=60%  ⇒ 通过关卡②
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy import stats as st

ROOT = Path(__file__).resolve().parents[4]
assert (ROOT / "outputs").is_dir(), f"ROOT 推算错误: {ROOT}"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ashare_hfq_access import load_hfq_panel          # noqa: E402
from ashare_risk_preflight import build_blocks        # noqa: E402
from macro_predict import (dm_test, nw_var, mde_of,   # noqa: E402
                           ridge_fit, ridge_pred)

DEFAULT_DB = ROOT / "outputs" / "ashare_csi800_hfq_xq.sqlite"
OUT_DIR = ROOT / "outputs" / datetime.now().strftime("%Y-%m-%d")

W_SHORT, W_LONG = 3, 12          # HAR 的"周/月"分量（以块为单位）
FEAT_NAMES = ["own_lag1", "own_meanW", "own_meanM", "mkt_lag1", "mkt_chg", "rel_mkt"]
SPEC = {"M1_HAR_own": [0, 1, 2], "M2_HAR_mkt": [0, 1, 2, 3, 4, 5]}


def build_feature_tensor(L: np.ndarray, mk: np.ndarray) -> np.ndarray:
    """L: (K, n_blocks) 对数已实现波动率；mk: (n_blocks,) 横截面中位数（市场波动）。
    返回 F: (K, n_blocks, 6)。"""
    K, n = L.shape
    F = np.full((K, n, 6), np.nan)
    with warnings.catch_warnings():          # 全 NaN 行的 nanmean 会告警，属预期（该股该窗口无数据）
        warnings.simplefilter("ignore", RuntimeWarning)
        for t in range(n):
            F[:, t, 0] = L[:, t]
            F[:, t, 1] = np.nanmean(L[:, max(0, t - W_SHORT + 1): t + 1], axis=1)
            F[:, t, 2] = np.nanmean(L[:, max(0, t - W_LONG + 1): t + 1], axis=1)
            F[:, t, 3] = mk[t]
            F[:, t, 4] = mk[t] - np.nanmean(mk[max(0, t - W_SHORT + 1): t + 1])
            F[:, t, 5] = L[:, t] - mk[t]
    return F


def nw_lag_of(n: int) -> int:
    return max(1, int(np.floor(4 * (n / 100.0) ** (2.0 / 9.0))))


def run_horizon(L: np.ndarray, mk: np.ndarray, block_years: np.ndarray,
                cols: list, min_train: int, winsor: tuple | None) -> dict:
    K, n_blocks = L.shape
    F = build_feature_tensor(L, mk)
    n_feat = len(cols)

    # ---- 预构建全部训练行（扩张窗 ⇒ 每期训练集 = 前缀切片，避免 O(n²) 的重复 vstack）----
    s_lo, s_hi = W_LONG - 1, n_blocks - 2          # s 取值范围（目标为 L[:, s+1]）
    X_parts, y_parts, res_parts = [], [], []
    for s in range(s_lo, s_hi + 1):
        X = F[:, s, cols]
        yv = L[:, s + 1]
        ok = np.isfinite(X).all(axis=1) & np.isfinite(yv)
        if ok.sum() < 50:
            X_parts.append(np.zeros((0, n_feat)))
            y_parts.append(np.zeros(0))
            res_parts.append(np.zeros(0))
            continue
        X_parts.append(X[ok])
        y_parts.append(yv[ok])
        res_parts.append(yv[ok] - L[ok, s])        # persistence 的训练残差（QLIKE 修正用）
    cum = np.cumsum([p.size for p in y_parts])     # cum[j] = s_lo..s_lo+j 的累计行数
    Xall = np.vstack(X_parts)
    yall = np.concatenate(y_parts)
    resall = np.concatenate(res_parts)

    mse_p_s, mse_m_s, adj_s = [], [], []      # 每期聚合：MSE_p / MSE_m / CW 修正项
    ql_p_s, ql_m_s = [], []
    years, per_year = [], {}
    # 分桶诊断用（判定 QLIKE 恶化来自哪个波动区间）
    bag_y, bag_p, bag_m, bag_cp, bag_cm = [], [], [], [], []

    for t in range(min_train, n_blocks - 1):
        j = (t - 1) - s_lo                        # 训练用到 s = s_lo .. t-1
        if j < 0:
            continue
        ntr = int(cum[j])
        if ntr < 200:
            continue
        Xtr, ytr = Xall[:ntr], yall[:ntr]
        if winsor:                                 # 缩尾在训练窗内估计（无前视）
            lo, hi = np.quantile(ytr, winsor[0]), np.quantile(ytr, winsor[1])
            ytr = np.clip(ytr, lo, hi)
        mdl = ridge_fit(Xtr, ytr)

        # ---- QLIKE 的对数正态偏差修正：QLIKE 的最优预测是 E[RV²|info]。
        #      若 logRV|info ~ N(ŷ, s²)，则 E[RV²] = exp(2ŷ + 2s²)（指数是 **2**，非 1）。
        #      ⚠️ 必须用**各自模型自己**的训练残差方差：共用会把残差小的模型
        #      系统性高估其方差预测。指数取错同样失真（k=1 夸大优势，见 _verify/qlike_check.py）。
        s2_p = float(np.var(resall[:ntr], ddof=1))
        s2_m = float(np.var(ytr - ridge_pred(mdl, Xtr), ddof=1))

        # ---- 预测 t+1 ----
        Xt = F[:, t, cols]
        ytrue = L[:, t + 1]
        ok = np.isfinite(Xt).all(axis=1) & np.isfinite(ytrue)
        if ok.sum() < 50:
            continue
        yh_m = ridge_pred(mdl, Xt[ok])
        yh_p = L[ok, t]                                # persistence
        yt = ytrue[ok]
        e_p, e_m = yt - yh_p, yt - yh_m

        mse_p = float(np.mean(e_p ** 2))
        mse_m = float(np.mean(e_m ** 2))
        mse_p_s.append(mse_p)
        mse_m_s.append(mse_m)
        # CW 修正项：必须先在个股层面平方再取横截面均值（聚合在平方之后 ⇒ 不能调 clark_west）
        adj_s.append(float(np.mean((yh_p - yh_m) ** 2)))

        # 标准非负 QLIKE：v²/v̂² − log(v²/v̂²) − 1（≥0，等号 iff v̂²=v²）
        v2 = np.exp(2.0 * yt)                          # 已实现方差
        vp2 = np.exp(2.0 * yh_p + 2.0 * s2_p)
        vm2 = np.exp(2.0 * yh_m + 2.0 * s2_m)
        ql_p_s.append(float(np.mean(v2 / vp2 - np.log(v2 / vp2) - 1.0)))
        ql_m_s.append(float(np.mean(v2 / vm2 - np.log(v2 / vm2) - 1.0)))

        bag_y.append(yt)
        bag_p.append(yh_p)
        bag_m.append(yh_m)
        bag_cp.append(np.full(yt.size, s2_p))
        bag_cm.append(np.full(yt.size, s2_m))

        yr = int(block_years[t + 1])
        years.append(yr)
        per_year.setdefault(yr, []).append((mse_p, mse_m))

    n = len(mse_p_s)
    if n < 12:
        return {"error": f"OOS 期数不足（{n}）"}

    mse_p_s = np.asarray(mse_p_s)
    mse_m_s = np.asarray(mse_m_s)
    adj_s = np.asarray(adj_s)
    d = mse_p_s - mse_m_s                    # 每期：persistence 损失 − 模型损失（>0 即模型优）
    f = d + adj_s                            # CW 统计量所用的序列

    mse_p = float(mse_p_s.mean())
    mse_m = float(mse_m_s.mean())
    rmse_red = 1.0 - float(np.sqrt(mse_m / mse_p)) if mse_p > 0 else float("nan")
    # ⚠️ 削减率用比值的前提是**损失非负**。标准 QLIKE 已满足（见模块 docstring）；
    #    曾用「log(v̂²)+v²/v̂²」形式为负，比值会符号翻转 —— 已订正。
    ql_p_m = float(np.mean(ql_p_s))
    ql_m_m = float(np.mean(ql_m_s))
    qlike_red = 1.0 - ql_m_m / ql_p_m if ql_p_m > 0 else float("nan")

    # ---- DM：复用 macro_predict.dm_test，传入每期聚合损失。
    #      ① 主口径 MSE：传 RMSE（e_p²−e_m² 恰为 d_t）
    #      ② 辅口径 QLIKE：传 sqrt(QLIKE)（dm_test 内部平方 ⇒ d_t = QLIKE_p − QLIKE_m）
    #         QLIKE 才是波动率的标准损失，辅口径用于确认结论不依赖损失函数的选择。
    lag = nw_lag_of(n)
    dm_stat, dm_p = dm_test(np.sqrt(mse_m_s), np.sqrt(mse_p_s), nw_lag=lag)
    ql_p_s_a = np.asarray(ql_p_s)
    ql_m_s_a = np.asarray(ql_m_s)
    ql_dm_stat, ql_dm_p = dm_test(np.sqrt(np.maximum(ql_m_s_a, 0.0)),
                                  np.sqrt(np.maximum(ql_p_s_a, 0.0)), nw_lag=lag)

    # ---- CW：对 {f_t} 做 t 检验（与 clark_west 同式，仅聚合顺序不同）----
    fbar = float(f.mean())
    se_cw = float(np.sqrt(np.var(f, ddof=1) / n))
    cw_stat = fbar / se_cw if se_cw > 0 else float("nan")
    cw_p = float(1.0 - st.t.cdf(cw_stat, df=n - 1))

    mde, _ = mde_of(d)
    ratio = abs(float(d.mean())) / mde if mde else float("nan")

    # ---- 分波动区间诊断：QLIKE 恶化来自哪一段？ ----
    ay = np.concatenate(bag_y)
    ap_ = np.concatenate(bag_p)
    am_ = np.concatenate(bag_m)
    acp = np.concatenate(bag_cp)
    acm = np.concatenate(bag_cm)
    q = np.quantile(ap_, [0.2, 0.4, 0.6, 0.8])
    bkt = np.digitize(ap_, q)
    buckets = {}
    for b in range(5):
        m = bkt == b
        if m.sum() < 100:
            continue
        v2 = np.exp(2.0 * ay[m])
        vp2 = np.exp(2.0 * ap_[m] + 2.0 * acp[m])
        vm2 = np.exp(2.0 * am_[m] + 2.0 * acm[m])
        buckets[f"Q{b+1}_predvol_{'低' if b == 0 else ('高' if b == 4 else '中')}"] = {
            "n": int(m.sum()),
            "mean_pred_logvol": float(ap_[m].mean()),
            "bias_persistence(实际-预测)": float((ay[m] - ap_[m]).mean()),
            "bias_model(实际-预测)": float((ay[m] - am_[m]).mean()),
            "mse_persistence": float(np.mean((ay[m] - ap_[m]) ** 2)),
            "mse_model": float(np.mean((ay[m] - am_[m]) ** 2)),
            "qlike_persistence": float(np.mean(v2 / vp2 - np.log(v2 / vp2) - 1.0)),
            "qlike_model": float(np.mean(v2 / vm2 - np.log(v2 / vm2) - 1.0)),
        }

    yr = {}
    for yv, pairs in sorted(per_year.items()):
        mp = float(np.mean([a for a, _ in pairs]))
        mm = float(np.mean([b for _, b in pairs]))
        yr[str(yv)] = {"mse_p": mp, "mse_m": mm, "better": bool(mm < mp)}
    n_years = len(yr)
    n_pos = sum(1 for v in yr.values() if v["better"])
    year_ratio = n_pos / max(1, n_years)

    passed = bool(cw_p <= 0.05 and dm_p <= 0.10 and ratio >= 1.0 and year_ratio >= 0.60)

    return {
        "n_oos_periods": n, "n_stocks": int(K), "nw_lag": int(lag),
        "features": [FEAT_NAMES[c] for c in cols],
        "mse_persistence": mse_p, "mse_model": mse_m,
        "rmse_reduction_pct": rmse_red * 100.0,
        "qlike_persistence": ql_p_m, "qlike_model": ql_m_m,
        "qlike_reduction_pct": qlike_red * 100.0,
        "mean_loss_diff": float(d.mean()), "mde": mde, "effect_over_mde": ratio,
        "dm_stat": dm_stat, "dm_p_nw": dm_p,
        "qlike_dm_stat": ql_dm_stat, "qlike_dm_p_nw": ql_dm_p,
        "cw_stat": cw_stat, "cw_p": cw_p,
        "predvol_buckets": buckets,
        "yearly": yr, "year_positive": f"{n_pos}/{n_years}",
        "year_positive_ratio": year_ratio,
        "passed_gate2": passed,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--since", default="2014-01-02")
    ap.add_argument("--horizons", default="5,20")
    ap.add_argument("--min-train", type=int, default=40)
    ap.add_argument("--min-valid-frac", type=float, default=0.25)
    ap.add_argument("--winsor", default="",
                    help="稳健性：训练窗内对 y 缩尾，格式 lo,hi（如 0.01,0.99）")
    ap.add_argument("--exclude-limit", action="store_true",
                    help="剔除含涨跌停日的分块（截断会压低 RV）")
    ap.add_argument("--models", default="M1_HAR_own,M2_HAR_mkt")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    winsor = tuple(float(x) for x in args.winsor.split(",")) if args.winsor else None
    db = Path(args.db)

    panel = load_hfq_panel(db, since=args.since, apply_qc=True, drop_first_n_days=5)
    print("=" * 74)
    print("方向 1 · 关卡②：波动率模型 vs persistence（样本外）")
    print("=" * 74)
    print(f"库: {db.name}   QC 剔除 {panel.n_bad} 只   "
          f"面板 {len(panel.codes)} 只 × {len(panel.dates)} 日")
    print(f"最小训练块数 {args.min_train}   缩尾={winsor}   剔涨跌停块={args.exclude_limit}")

    results = {}
    for h in [int(x) for x in args.horizons.split(",")]:
        rv, n_lim = build_blocks(panel.close, h, panel.codes, args.exclude_limit)
        if rv.shape[1] < args.min_train + 12:
            print(f"\n[h={h}] 块数不足，跳过")
            continue
        ok = np.mean(np.isfinite(rv), axis=1) >= args.min_valid_frac
        rv = rv[ok]
        with np.errstate(divide="ignore", invalid="ignore"):
            L = np.log(rv)
        L[~np.isfinite(L)] = np.nan
        mk = np.nanmedian(L, axis=0)
        K, n_blocks = L.shape

        block_dates = [panel.dates[min((i + 1) * h, len(panel.dates) - 1)]
                       for i in range(n_blocks)]
        by = np.array([int(d[:4]) for d in block_dates])

        print(f"\n{'='*74}\n[h={h} 日]  {K} 只 × {n_blocks} 块"
              + (f"（剔涨跌停块 {n_lim:,}）" if args.exclude_limit else ""))
        for name in args.models.split(","):
            r = run_horizon(L, mk, by, SPEC[name], args.min_train, winsor)
            results[f"h{h}_{name}"] = r
            if "error" in r:
                print(f"  {name}: {r['error']}")
                continue
            print(f"\n  --- {name}  特征={r['features']} ---")
            print(f"    OOS 期数 {r['n_oos_periods']}（NW lag={r['nw_lag']}）"
                  f"   ← 检验 n=期数，非 pooled 样本对")
            print(f"    MSE  persistence={r['mse_persistence']:.6f}   "
                  f"model={r['mse_model']:.6f}")
            print(f"    RMSE  降低 {r['rmse_reduction_pct']:+.2f}%")
            print(f"    QLIKE persistence={r['qlike_persistence']:.6f}   "
                  f"model={r['qlike_model']:.6f}   降低 {r['qlike_reduction_pct']:+.2f}%")
            print(f"    MDE={r['mde']:.6f}   |效应|/MDE={r['effect_over_mde']:.2f}")
            print(f"    CW    stat={r['cw_stat']:+.3f}  p={r['cw_p']:.4f}")
            print(f"    NW-DM(MSE)   stat={r['dm_stat']:+.3f}  p={r['dm_p_nw']:.4f}")
            print(f"    NW-DM(QLIKE) stat={r['qlike_dm_stat']:+.3f}  "
                  f"p={r['qlike_dm_p_nw']:.4f}")
            print(f"    逐年占优 {r['year_positive']} = {r['year_positive_ratio']:.0%}")
            print(f"    ⇒ 关卡②: {'通过' if r['passed_gate2'] else '未通过'}")
            if r.get("predvol_buckets"):
                print("    分预测波动五分位诊断（bias>0 = 低估实际波动）：")
                print("      桶        n      bias_P   bias_M    MSE_P   MSE_M   QLIKE_P  QLIKE_M")
                for k, v in r["predvol_buckets"].items():
                    print(f"      {k:<14s}{v['n']:>7d}  "
                          f"{v['bias_persistence(实际-预测)']:+.4f}  "
                          f"{v['bias_model(实际-预测)']:+.4f}  "
                          f"{v['mse_persistence']:.4f}  {v['mse_model']:.4f}  "
                          f"{v['qlike_persistence']:+.3f}  {v['qlike_model']:+.3f}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else OUT_DIR / f"vol_forecast_{db.stem}.json"
    out.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "db_snapshot": db.name,
        "qc": {"applied": True, "n_excluded": panel.n_bad},
        "panel": {"n_stocks": len(panel.codes), "n_days": len(panel.dates)},
        "params": {"min_train": args.min_train, "winsor": args.winsor,
                   "exclude_limit": args.exclude_limit,
                   "min_valid_frac": args.min_valid_frac},
        "results": results,
        "note": "关卡②：模型 vs persistence。检验 n=期数（横截面相关按 K_eff≈4 折损，"
                "故先聚合到每期再检验）。统计量复用 macro_predict（DM/NW/MDE/ridge）。",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n产物: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
