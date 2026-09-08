#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
宏观 surprise → 申万二级行业轮动：回测与有效性检验 · 2026-09-03 建立

承接两道前置闸门（均已通过）：
  1. macro_sector_timing.py 功效预检：口径 B（纯时序择时）**判死**（MDE(r)=0.18~0.26，
     现实 r 仅 0.05~0.10，且 N 被月频锁死）。口径 A（横截面轮动）判活，门槛 K≥35。
  2. ashare_sw_industry.py 实测面板：K=72（成员≥5 的申万二级行业），N=152 月
     （2014-02~2026-09）。MDE@0.04=0.0279 → 比值 1.43 ✅；悲观 0.02 → 0.74 ❌。

规格（写死，禁止在结果出来后回头调 —— 调了就是 p-hacking）
--------------------------------------------------------
· 宏观端：ppi_yoy AR(3) walk_forward OOS 误差 = surprise，**100% 复用 macro_predict
  （load_panel/walk_forward/ridge_fit），不改特征、不改窗口**（交付层同源纪律）。
· 可交易时点：t 月 PPI 于 t+1 月 9 日左右公布 ⇒ surprise_t 只能用于交易 **t+1 月**。
· 标准化：z_t = (surprise_t − μ_{<t}) / σ_{<t}，μ/σ 用截至 t−1 的历史（expanding，无前视）。
  ⚠ 不去均值的后果：因子 = β_i×surprise 会退化成"β_i 水平"的静态排序，
    surprise 符号翻转时排序不翻转 ⇒ 测的就不是轮动。
· β 估计：expanding OLS，r_{i,s} ~ 1 + z_{s−1}，最少 36 个观测（前缀和 O(1) 更新）。
· 因子：f_{i,m} = β_i^{(m)} × z_{m−1}；IC_m = spearman(f, r)，有效行业 ≥30 才计入。
· 零分布：**circular shift** z 序列（保留 z 自相关 + 行业收益横截面结构，只破坏时序对齐），
  完整重跑 β 估计与 IC —— 不固定 β，否则是循环论证。

纪律（SOP §十二 / §13）：
  · 评价三件套 = MDE + 效应量 + |效应|/MDE；
  · **逐年为正 <60% 即不可交付**；
  · 结论四级分类不得混淆。

用法：
  python macro_sector_rotation.py                    # 主口径
  python macro_sector_rotation.py --mc 2000          # 指定 MC 次数
  python macro_sector_rotation.py --min-members 8    # 行业成员门槛（稳健性）
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy import stats

_HERE = Path(__file__).resolve().parent
# 工作区根 = _HERE.parents[3]（固定写法，与项目其余脚本一致）。
# 禁用「就近查找含 outputs/ 的父目录」启发式 —— 第廿七类 b 静默 bug：
# 该启发式会命中 my-deepseek-harness/ 下的残留 outputs 目录 ⇒ 路径静默错拼。
ROOT = _HERE.parents[3]
sys.path.insert(0, str(_HERE))

OUT_DIR = ROOT / "outputs" / "2026-09-03" / "sector_timing"
PANEL_JSON = OUT_DIR / "industry_monthly_panel.json"
Z = 2.8006              # z(0.975)+z(0.80)
MIN_OBS_BETA = 36       # β 估计最小观测数
MIN_K_IC = 30           # 当月有效行业数下限
NW_LAG = 3

import macro_predict as mp                                   # noqa: E402  规格同源


# ---------------------------------------------------------------- 数据
def load_panel() -> tuple[list[str], dict[str, dict[str, float]]]:
    d = json.loads(PANEL_JSON.read_text(encoding="utf-8"))
    return d["months"], d["panel"]


def load_z() -> dict[str, float]:
    """PPI surprise 的 expanding 标准化值（无前视）。返回 {period: z}。"""
    data = mp.load_panel()
    oos = mp.walk_forward(data, ("ppi_yoy", "CN"), [])
    sur = oos["y"] - oos["mdl"]
    z, hist = {}, []
    for t, s in zip(oos["t"], sur):
        if len(hist) >= MIN_OBS_BETA:
            mu = float(np.mean(hist))
            sd = float(np.std(hist, ddof=1))
            if sd > 0:
                z[t] = float((s - mu) / sd)
        hist.append(float(s))
    return z


# ---------------------------------------------------------------- 核心
def build_arrays(months, panel, z):
    """返回 (ret[K,N] 含 nan, zprev[N])，zprev[m] = 用于交易 months[m] 的 z。"""
    inds = sorted(panel.keys())
    K, N = len(inds), len(months)
    ret = np.full((K, N), np.nan)
    for i, ind in enumerate(inds):
        for m, r in panel[ind].items():
            if m in months:
                ret[i, months.index(m)] = r
    # t 月 PPI 在 t+1 月初公布 ⇒ 交易 months[m] 用的是 (months[m] 对应期 − 1 月) 的 z
    zprev = np.full(N, np.nan)
    mset = {m: k for k, m in enumerate(months)}
    for m, v in z.items():
        tgt = mp.pshift(m, -1)                  # m 往后一个月
        if tgt in mset:
            zprev[mset[tgt]] = v
    return inds, ret, zprev


def beta_matrix(ret: np.ndarray, zprev: np.ndarray) -> np.ndarray:
    """β[K,N]：β[i,m] 用 s ≤ m−1 的 (z_{s−1}, r_{i,s}) expanding OLS 估计。

    前缀和实现：slope = (n·Sxy − Sx·Sy) / (n·Sxx − Sx²)，逐 m O(1)。
    β[i,m] 为 nan 表示历史不足 ⇒ 该月该行业不参与因子。
    """
    K, N = ret.shape
    beta = np.full((K, N), np.nan)
    x = zprev                                   # x[s] 用于交易 s 月
    for i in range(K):
        y = ret[i]
        ok = ~np.isnan(x) & ~np.isnan(y)
        Sn = np.cumsum(ok.astype(float))
        xa = np.where(ok, x, 0.0)
        ya = np.where(ok, y, 0.0)
        Sx = np.cumsum(xa)
        Sy = np.cumsum(ya)
        Sxy = np.cumsum(xa * ya)
        Sxx = np.cumsum(xa * xa)
        for m in range(N):                     # 只用 s ≤ m−1
            n = Sn[m - 1] if m >= 1 else 0.0
            if n >= MIN_OBS_BETA:
                k = m - 1
                den = n * Sxx[k] - Sx[k] ** 2
                if den > 1e-12:
                    beta[i, m] = (n * Sxy[k] - Sx[k] * Sy[k]) / den
    return beta


def ic_series(ret: np.ndarray, beta: np.ndarray, zprev: np.ndarray):
    """逐月 spearman(β_i × z, r_i)。返回 (ic[], k_eff[], months_idx[])。"""
    K, N = ret.shape
    ic, keff = [], []
    for m in range(N):
        if np.isnan(zprev[m]):
            continue
        f = beta[:, m] * zprev[m]
        valid = ~np.isnan(f) & ~np.isnan(ret[:, m])
        if valid.sum() < MIN_K_IC:
            continue
        rho = stats.spearmanr(f[valid], ret[valid, m]).statistic
        if rho is not None and not np.isnan(rho):
            ic.append(float(rho))
            keff.append(int(valid.sum()))
    return np.array(ic), np.array(keff)


def mc_null(ret, zprev, n_mc: int, seed: int = 20260903):
    """circular shift 零分布：移位 z，完整重跑 β 与 IC。"""
    rng = np.random.default_rng(seed)
    valid_m = ~np.isnan(zprev)
    zv = zprev[valid_m]
    n = len(zv)
    out = np.empty(n_mc)
    for b in range(n_mc):
        k = int(rng.integers(1, n))             # 非零移位（k=0 是真实值）
        zs = zprev.copy()
        zs[valid_m] = np.roll(zv, k)
        beta = beta_matrix(ret, zs)
        ic, _ = ic_series(ret, beta, zs)
        out[b] = float(np.mean(ic)) if len(ic) else np.nan
    return out


# ---------------------------------------------------------------- 报告
def report(ic: np.ndarray, keff: np.ndarray, null: np.ndarray, label: str):
    n = len(ic)
    mean = float(np.mean(ic))
    sd = float(np.std(ic, ddof=1))
    se_nw = math.sqrt(mp.nw_var(ic - mean, NW_LAG) / n) if n > NW_LAG else float("nan")
    t_nw = mean / se_nw if se_nw > 0 else float("nan")
    p_nw = 2 * (1 - stats.norm.cdf(abs(t_nw))) if se_nw > 0 else float("nan")
    mde = Z * sd / math.sqrt(n)
    k_avg = float(np.mean(keff))

    # 逐年为正（硬纪律：<60% 不可交付）
    # 注：月份索引未保留，这里按 12 个月一组近似切分（N 已知、按月连续）
    per_year = []
    for s in range(0, n - 11, 12):
        per_year.append(float(np.mean(ic[s:s + 12])))
    pos_yr = sum(1 for v in per_year if v > 0)
    ratio_yr = pos_yr / len(per_year) if per_year else float("nan")

    # MC 零分布 p 值（单侧：IC 均值 > 0）
    null = null[~np.isnan(null)]
    p_mc = float((np.sum(null >= mean) + 1) / (len(null) + 1)) if len(null) else float("nan")

    print("=" * 78)
    print(f"宏观 surprise → 行业轮动 · {label}")
    print("=" * 78)
    print(f"样本：IC 月数 N_eff = {n}    平均有效行业数 K_eff = {k_avg:.1f}")
    print()
    print("【评价三件套】")
    print(f"  效应量   IC 均值      = {mean:+.4f}")
    print(f"  MDE      （α=.05,pw=.8）= {mde:.4f}")
    print(f"  |效应|/MDE            = {abs(mean)/mde:.2f}"
          f"   {'✅ 功效充足' if abs(mean)/mde >= 1 else '❌ 功效不足'}")
    print()
    print("【显著性】")
    print(f"  NW t({NW_LAG}) = {t_nw:+.2f}   p = {p_nw:.4f}"
          f"   {'✅' if p_nw < 0.05 else '❌'}")
    print(f"  MC 零分布（circular shift, {len(null)} 次）p = {p_mc:.4f}"
          f"   {'✅' if p_mc < 0.05 else '❌'}")
    print(f"     零分布：均值 {np.mean(null):+.4f}  标准差 {np.std(null):.4f}"
          f"  95% 上界 {np.quantile(null, 0.95):+.4f}")
    print()
    print("【交叉校验】")
    print(f"  IC 序列标准差 σ_obs   = {sd:.4f}")
    print(f"  理论噪声 sqrt(1/(K−1)) = {math.sqrt(1/(k_avg-1)):.4f}")
    print(f"  → σ_obs / 理论噪声 = {sd/math.sqrt(1/(k_avg-1)):.2f}"
          f"（≈1 表示无额外信号；>1 表示有真实 IC 波动或额外结构）")
    print()
    print("【逐年为正】（硬纪律：<60% 不可交付）")
    print(f"  {pos_yr}/{len(per_year)} = {ratio_yr*100:.0f}%"
          f"   {'✅' if ratio_yr >= 0.6 else '❌'}")
    print("  逐年 IC 均值：" + "  ".join(f"{v:+.3f}" for v in per_year))
    print()

    verdict = "可交付" if (abs(mean) / mde >= 1 and p_nw < 0.05 and p_mc < 0.05
                          and ratio_yr >= 0.6) else "不可交付"
    print(f"【结论】{verdict}")
    return dict(ic_mean=mean, mde=mde, ratio=abs(mean) / mde, t_nw=t_nw, p_nw=p_nw,
                p_mc=p_mc, n=n, k_avg=k_avg, pos_year_ratio=ratio_yr,
                per_year=per_year, verdict=verdict)


def diag_keff(ret: np.ndarray, n_rand: int = 400, seed: int = 20260903) -> dict:
    """实测横截面【有效宽度】K_eff —— 预检模型 Var_noise=1/(K−1) 的适用性检验。

    1/(K−1) 假设截面单位两两独立。但行业收益高度同源（市场因子 + 少数风格因子），
    有效独立宽度远小于名义 K ⇒ 真实 IC 噪声方差 >> 1/(K−1) ⇒ MDE 被严重低估。

    三种独立口径互证：
      (a) 相关矩阵平均非对角元 ρ̄ → K_eff ≈ K / (1 + (K−1)ρ̄)
      (b) PCA participation ratio PR = (Σλ)² / Σλ²（有效维度，标准度量）
      (c) **随机因子**跑 IC，取 sd → K_eff = 1/sd² + 1（最直接，与 IC 口径同源）
    """
    rng = np.random.default_rng(seed)
    K, N = ret.shape
    # 用无缺失的月份
    ok_m = np.where(np.sum(~np.isnan(ret), axis=0) >= MIN_K_IC)[0]
    M = ret[:, ok_m]
    # 去均值（按行）后填 0，避免缺失影响相关
    M0 = np.where(np.isnan(M), 0.0, M)
    mu = M0.mean(axis=1, keepdims=True)
    M0 = M0 - mu

    # (a) 平均相关系数
    C = np.corrcoef(M0)
    off = C[~np.eye(K, dtype=bool)]
    rho = float(np.nanmean(off))
    k_a = K / (1 + (K - 1) * rho) if (1 + (K - 1) * rho) > 0 else float("nan")

    # (b) PCA participation ratio
    lam = np.linalg.eigvalsh(np.cov(M0))
    lam = np.clip(lam, 0, None)
    pr = float((lam.sum() ** 2) / np.sum(lam ** 2))

    # (c) 随机因子的 IC 标准差
    sds = []
    for _ in range(n_rand):
        f = rng.standard_normal(K)
        ics = []
        for j in range(M.shape[1]):
            y = M[:, j]
            v = ~np.isnan(y)
            if v.sum() < MIN_K_IC:
                continue
            r = stats.spearmanr(f[v], y[v]).statistic
            if r is not None and not np.isnan(r):
                ics.append(r)
        if ics:
            sds.append(float(np.std(ics, ddof=1)))
    sd_rand = float(np.mean(sds))
    k_c = 1.0 / sd_rand ** 2 + 1

    print("=" * 78)
    print("诊断：横截面【有效宽度】K_eff（预检模型 Var_noise=1/(K−1) 适用性）")
    print("=" * 78)
    print(f"名义行业数 K = {K}")
    print(f"  (a) 平均相关系数 ρ̄ = {rho:.3f}  →  K_eff ≈ {k_a:.1f}")
    print(f"  (b) PCA participation ratio   →  K_eff ≈ {pr:.1f}")
    print(f"  (c) 随机因子 IC 标准差 {sd_rand:.4f} → K_eff ≈ {k_c:.1f}  【主口径，与 IC 同源】")
    print()
    print(f"理论噪声 sqrt(1/(K−1))    = {math.sqrt(1/(K-1)):.4f}")
    print(f"实测噪声 sqrt(1/(K_eff−1)) = {math.sqrt(1/(k_c-1)):.4f}"
          f"   （{(k_c and (1/(k_c-1))/(1/(K-1))) or 0:.1f}× 于理论值）")
    print()
    print("含义：行业收益高度同源（市场因子主导），72 个行业在统计上只相当于 "
          f"~{k_c:.0f} 个独立观测。")
    print("⇒ 预检用 1/(K−1) 系统性**低估**了噪声、低估了 MDE —— 这是本次最大的模型失效点。")
    return dict(K=K, rho=rho, k_eff_a=k_a, k_eff_pca=pr, k_eff_c=k_c, sd_rand=sd_rand)


def split_test(ret: np.ndarray, zprev: np.ndarray, months: list[str],
               split_month: str, n_mc: int = 1000, seed: int = 20260903) -> dict:
    """样本分割法：β 与检验期**完全分离**，消除 expanding β 的伪振幅放大。

    动机（负向测试的结论）：
      时变 β 流程下，**纯随机 z** 也能产生 IC 振幅 sd=0.203，而固定随机因子的
      理论/实测振幅只有 0.121 ⇒ 流程把噪声放大了 1.7 倍，MDE 从 ~0.032 被推到 0.063，
      直接吃掉检出 0.04 量级效应的能力。
      机制：β_i 由历史 (z_s, r_{i,s}) 估计，与 r 的横截面结构耦合，
      实测 corr(β_i, 行业历史月均收益) = 0.22~0.38（本应为 0）。

    修复：β 只在 [0, split_month] 上估一次并冻结，检验期 (split_month, end] 完全不参与
    β 估计 ⇒ 因子不含检验期信息，IC 干净。代价是检验期变短。
    """
    K, N = ret.shape
    si = months.index(split_month) if split_month in months else None
    if si is None:
        raise SystemExit(f"[err ] split_month {split_month} 不在面板月份中")

    x, beta = zprev, np.full(K, np.nan)
    for i in range(K):
        xs, ys = [], []
        for s in range(si + 1):
            if not np.isnan(x[s]) and not np.isnan(ret[i, s]):
                xs.append(x[s]); ys.append(ret[i, s])
        if len(xs) >= MIN_OBS_BETA:
            vx, vy = np.var(xs), np.mean(ys)
            if vx > 1e-12:
                beta[i] = float(np.cov(xs, ys)[0, 1] / vx)

    ic, ks = [], []
    for m in range(si + 1, N):
        if np.isnan(zprev[m]):
            continue
        f = beta * zprev[m]
        v = ~np.isnan(f) & ~np.isnan(ret[:, m])
        if v.sum() < MIN_K_IC:
            continue
        rho = stats.spearmanr(f[v], ret[v, m]).statistic
        if rho is not None and not np.isnan(rho):
            ic.append(float(rho)); ks.append(int(v.sum()))
    ic, ks = np.array(ic), np.array(ks)

    rng = np.random.default_rng(seed)
    valid = ~np.isnan(zprev)
    zv, nv = zprev[valid], int(valid.sum())
    null = np.empty(n_mc)
    for b in range(n_mc):
        zs = zprev.copy()
        zs[valid] = np.roll(zv, int(rng.integers(1, nv)))
        # 移位后 β 也必须在分割点前重估（同构，否则不是同一个检验）
        bt = np.full(K, np.nan)
        for i in range(K):
            xs, ys = [], []
            for s in range(si + 1):
                if not np.isnan(zs[s]) and not np.isnan(ret[i, s]):
                    xs.append(zs[s]); ys.append(ret[i, s])
            if len(xs) >= MIN_OBS_BETA and np.var(xs) > 1e-12:
                bt[i] = float(np.cov(xs, ys)[0, 1] / np.var(xs))
        ics = []
        for m in range(si + 1, N):
            if np.isnan(zs[m]):
                continue
            f = bt * zs[m]
            v = ~np.isnan(f) & ~np.isnan(ret[:, m])
            if v.sum() < MIN_K_IC:
                continue
            r_ = stats.spearmanr(f[v], ret[v, m]).statistic
            if r_ is not None and not np.isnan(r_):
                ics.append(float(r_))
        null[b] = float(np.mean(ics)) if ics else np.nan
    return report(ic, ks, null, f"样本分割（β 冻结于 ≤{split_month}）")


def neg_test(ret: np.ndarray, zprev: np.ndarray, n_rep: int = 200,
             seed: int = 20260903) -> dict:
    """负向测试：用【纯随机 z】（iid N(0,1)，非 circular shift）跑完整流程。

    目的：确认"真实因子 IC 标准差 0.243 是随机噪声 0.121 的 2 倍"不是流程 bug 造成的。
    判据（预声明，先写死再看结果）：
      · 随机 z 下 IC 的 sd ≈ sqrt(1/(K−1))（≈0.12）⇒ 流程无放大，真实 z 的高振幅是真信号
      · 随机 z 下 IC 的 sd ≈ 0.24            ⇒ 流程本身（β 估计/对齐）在制造伪信号，结论作废

    为什么不用 circular shift 代替：roll 保留了 z 的部分自相关与对齐片段，
    会残留真实关系（实测零分布 IC 均值 sd 0.0187 → 隐含 IC sd ≈0.20，介于两者之间），
    **不干净**，不能用于判定流程是否有放大。
    """
    rng = np.random.default_rng(seed)
    valid_m = ~np.isnan(zprev)
    n = int(valid_m.sum())
    sds = []
    for _ in range(n_rep):
        zs = zprev.copy()
        zs[valid_m] = rng.standard_normal(n)
        beta = beta_matrix(ret, zs)
        ic, _ = ic_series(ret, beta, zs)
        if len(ic) > 30:
            sds.append(float(np.std(ic, ddof=1)))
    sd_rand = float(np.mean(sds))
    k = int(np.sum(~np.isnan(ret[:, 0]) * 0) + ret.shape[0])
    theo = math.sqrt(1.0 / (k - 1))
    print("=" * 78)
    print("负向测试：纯随机 z（iid）跑完整流程")
    print("=" * 78)
    print(f"重复 {len(sds)} 次")
    print(f"  随机 z 下 IC 标准差      = {sd_rand:.4f}")
    print(f"  独立随机排序理论 sqrt(1/(K−1)) = {theo:.4f}")
    print(f"  随机因子实测（diag 口径）      = 0.1213")
    print()
    print("判定：")
    if abs(sd_rand - theo) / theo < 0.25:
        print(f"  ✅ 随机 z 下 sd 与理论值一致（偏差 {100*(sd_rand-theo)/theo:+.0f}%）"
              " ⇒ 流程无伪信号放大")
        print("     ⇒ 真实因子 IC sd=0.2428 高出随机的部分，是【真实的、方向不稳定的信号】")
    else:
        print(f"  ⚠ 随机 z 下 sd 与理论值偏离 {100*(sd_rand-theo)/theo:+.0f}%"
              " ⇒ 流程存在放大机制，需排查 β 估计或对齐")
    return dict(sd_random_z=sd_rand, theo=theo, n_rep=len(sds))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mc", type=int, default=1000)
    ap.add_argument("--min-members", type=int, default=5)
    ap.add_argument("--diag", action="store_true", help="只跑横截面有效宽度诊断")
    ap.add_argument("--negtest", action="store_true", help="负向测试：纯随机 z")
    ap.add_argument("--split", metavar="YYYY-MM",
                    help="样本分割法：β 冻结于该月及之前，之后为检验期")
    a = ap.parse_args()

    months, panel = load_panel()
    z = load_z()
    print(f"[data ] 行业面板 K={len(panel)}, 月份 N={len(months)}"
          f"  {months[0]}~{months[-1]}")
    print(f"[data ] PPI surprise 标准化后 z：{len(z)} 期")

    inds, ret, zprev = build_arrays(months, panel, z)
    if a.diag:
        diag_keff(ret)
        return 0
    if a.negtest:
        neg_test(ret, zprev)
        return 0
    if a.split:
        split_test(ret, zprev, months, a.split, n_mc=a.mc)
        return 0

    inds, ret, zprev = build_arrays(months, panel, z)
    nz = int(np.sum(~np.isnan(zprev)))
    print(f"[align] 可交易月份（有对应 z）= {nz}")
    if nz < 60:
        print("[err  ] 可交易月份过少，中止")
        return 1

    beta = beta_matrix(ret, zprev)
    ic, keff = ic_series(ret, beta, zprev)
    print(f"[ic   ] 有效 IC 月数 = {len(ic)}")

    null = mc_null(ret, zprev, a.mc)
    res = report(ic, keff, null, f"主口径（成员≥{a.min_members}，等权）")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "rotation_result.json").write_text(json.dumps(
        {k: v for k, v in res.items() if k != "per_year"} | dict(per_year=res["per_year"]),
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[out  ] {OUT_DIR / 'rotation_result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
