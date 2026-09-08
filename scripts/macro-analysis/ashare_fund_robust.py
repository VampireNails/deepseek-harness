# -*- coding: utf-8 -*-
"""A股基本面因子 · 稳健性诊断（稳定性 / 极端期 / 正交化 / 五分位经济性）。

【为什么要这个脚本】
ashare_fund_validate.py 跑出「净利同比(缩尾) IC = −0.111，t = −3.33，通过 MCC」，
但两个特征让这个结论不可直接采信：
  ① 6 个因子 IC **全部为负** —— 它们高度相关（都是盈利/成长维度），
     很可能是【同一个效应】被算了 6 次，而不是 6 个独立发现；
  ② |IC| = 0.111 远大于基本面因子的现实量级（0.02~0.05）。
     效应量异常大的时候，优先怀疑偏差，而不是先庆祝。

本脚本做四项检验，任何一项不过关就不能进第③关：
  A. 逐期 IC 序列 + 剔除最极端 2 期后是否仍显著   → 排除「少数极端期驱动」
  B. 时间对半拆分 + 分年度符号一致性             → 排除「单段样本/单一年度驱动」
  C. 缩尾水平敏感性（q = 0.01 / 0.05 / 0.10）    → 排除「缩尾参数挑出来的」
  D. 对 9 个价量风格因子正交化后重算 IC           → 排除「风格押注换马甲」
  E. 五分位价差的经济量级                        → 扣成本后还剩多少

【★ D 是最关键的一项】
此前已证明该池的价量超额 100% 来自风格押注。农业股里「低 ROE / 低毛利 / 低成长」
几乎等价于「小市值 / 高波动 / 价值股」——如果基本面 IC 正交化后归零，
那它只是同一个风格暴露穿了件财报外衣，不算新 alpha。
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.stats import rankdata

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]
sys.path.insert(0, str(_HERE))

from ashare_agri_backtest import (  # noqa: E402
    DB as PRICE_DB, PRICE_TABLE,
    load_panel, build_tradable, compute_factors, fwd,
)
from ashare_fund_validate import (  # noqa: E402
    FUND_DB, OUT_DIR, FACTORS,
    load_fund, build_panel, cross_section_z, spearman_ic,
    newey_west_t, effective_date,
)

# 正交化要剔除的价量风格基（9 个，覆盖此前 comp_turn_mom 用到的全部维度）
STYLE_KEYS = [
    "momentum_20d", "momentum_60d", "reversal_5d", "volatility_20d",
    "price_to_ma20", "volume_ratio_20d", "turnover_level_20d",
    "turnover_ratio_20d", "amihud_20d",
]

MAIN = ["np_yoy_wins", "roe_rel", "comp_roe_rev"]


def ortho_residual(z, style_zs, mask):
    """逐期把因子 z 对风格基做截面 OLS，返回 (残差 z, R²序列)。

    ⚠️ 教训：第一版要求 9 个风格因子全部非缺失，否则整期丢弃 →
       观测数从 25 暴跌到 8，正交化结论完全不可用。
       正确做法：风格 z 缺失填 0 —— z 已做截面标准化，0 就是截面均值，
       填 0 等价于「该风格上取中性」，不会引入偏差，也不会损失样本。
    """
    T = z.shape[0]
    out = np.full_like(z, np.nan)
    r2s = []
    for t in range(T):
        m = mask[t] & np.isfinite(z[t])
        if int(m.sum()) < 20:
            continue
        y = z[t][m].astype(float)
        cols = []
        for sz in style_zs:
            v = sz[t][m].astype(float)
            cols.append(np.where(np.isfinite(v), v, 0.0))   # 缺失 → 中性
        X = np.column_stack([np.ones(len(y))] + cols)
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        res = y - X @ beta
        # R² = 1 − Var(res)/Var(y)：原始信号有多少比例落在风格空间里
        vy = float(y.var())
        if vy > 0:
            r2s.append(1.0 - float(res.var()) / vy)
        sd = res.std()
        if sd > 0:
            out[t, m] = (res - res.mean()) / sd
    return out, r2s


def quintile_spread(z, ret, mask, dates, obs_idx, holding, label):
    """五分位：Q1(因子最低 20%) 与 Q5(最高 20%) 的等权前瞻收益。

    返回 (spread 序列, Q1 序列, Q5 序列)，spread = Q1 − Q5。
    因为实测 IC 为负（低盈利跑赢），预期 spread > 0。
    """
    sp, q1, q5, bench = [], [], [], []
    for t in obs_idx:
        m = mask[t] & np.isfinite(z[t]) & np.isfinite(ret[t])
        n = int(m.sum())
        if n < 20:
            continue
        v = z[t][m]
        r = ret[t][m]
        k = max(int(n * 0.2), 3)
        o = np.argsort(v)
        lo = float(r[o[:k]].mean())
        hi = float(r[o[-k:]].mean())
        q1.append(lo)
        q5.append(hi)
        bench.append(float(r.mean()))   # 池内等权基准（同期可比）
        sp.append(lo - hi)
    sp = np.array(sp)
    if len(sp) < 5:
        return None
    # 年化：每个观测覆盖 holding 个交易日，一年约 243 个
    per_year = 243.0 / holding
    # Q1 相对池内等权基准的年化超额（这才是可交付口径：不可做空，只能多头）
    ex = np.array(q1) - np.array(bench)
    ex_t = float(ex.mean() / (ex.std(ddof=1) / math.sqrt(len(ex)))) \
        if len(ex) > 3 and ex.std(ddof=1) > 0 else float("nan")
    return {
        "label": label,
        "n": int(len(sp)),
        "spread_mean_per_period": round(float(sp.mean()), 5),
        "spread_ann": round(float(sp.mean() * per_year), 5),
        "q1_ann": round(float(np.mean(q1) * per_year), 5),
        "q5_ann": round(float(np.mean(q5) * per_year), 5),
        "bench_ann": round(float(np.mean(bench) * per_year), 5),
        "q1_excess_ann": round(float(ex.mean() * per_year), 5),
        "q1_excess_t": round(ex_t, 3),
        "q1_excess_pos_frac": round(float((ex > 0).mean()), 3),
        "spread_std": round(float(sp.std(ddof=1)), 5),
        "t": round(float(sp.mean() / (sp.std(ddof=1) / math.sqrt(len(sp)))), 3),
        "pos_frac": round(float((sp > 0).mean()), 3),
    }


def summarize(ics, label, alpha=0.05, n_tests=6):
    ics = np.asarray(ics, dtype=float)
    n = len(ics)
    if n < 8:
        return None
    var_obs = float(ics.var(ddof=1))
    mde = 2.8006 * math.sqrt(var_obs) / math.sqrt(n)
    t, _ = newey_west_t(list(ics), lags=max(int(math.ceil(n ** 0.25)), 1))
    thr = 2.638
    return {
        "label": label, "n": n,
        "ic_mean": round(float(ics.mean()), 5),
        "ic_std": round(math.sqrt(var_obs), 5),
        "mde": round(mde, 5),
        "ratio": round(abs(float(ics.mean())) / mde, 3) if mde > 0 else None,
        "t_hac": round(float(t), 3),
        "sig": bool(abs(t) >= thr),
    }


def row(d, extra=""):
    if d is None:
        print(f"  {'':<34}  观测不足")
        return
    print(f"  {d['label']:<34}{d['n']:>5}{d['ic_mean']:>+9.4f}{d['ic_std']:>8.4f}"
          f"{d['mde']:>8.4f}{(d['ratio'] or 0):>8.2f}{d['t_hac']:>+8.2f}"
          f"{'  ✓' if d['sig'] else '    '}{extra}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holding", type=int, default=60)
    ap.add_argument("--min-cross", type=int, default=20)
    ap.add_argument("--fund-db", default=str(FUND_DB))
    ap.add_argument("--exclude-financial", action="store_true",
                    help="剔除毛利率全期缺失的金融/类金融股")
    ap.add_argument("--min-history", type=int, default=0,
                    help="点入时上市满 N 个有效交易日；宽基池必须设 250")
    ap.add_argument("--price-db", default=str(PRICE_DB))
    ap.add_argument("--start", default="2014-01-01")
    args = ap.parse_args()

    print("=" * 78)
    print(f"A股基本面因子 · 稳健性诊断（H={args.holding}）")
    print("=" * 78)

    fconn = sqlite3.connect(str(args.fund_db), timeout=30)
    fund_rows, med = load_fund(fconn)
    fconn.close()

    pconn = sqlite3.connect(str(args.price_db), timeout=30)
    try:
        qc_bad = {r[0] for r in pconn.execute("SELECT code FROM hfq_qc WHERE bad=1")}
    except sqlite3.OperationalError:
        qc_bad = set()
    dates, codes, _o, close, volume, amount, turn = load_panel(pconn, PRICE_TABLE)
    pconn.close()

    tradable = build_tradable(close, volume, amount, codes, qc_bad,
                              exclude_b=True, exclude_limit=False,
                              min_history=args.min_history)
    t0 = int(np.where(dates >= args.start)[0][0])
    T, M = close.shape
    print(f"价格面板 {T} 天 × {M} 只   起始 {dates[t0]}   可交易单元 {tradable.mean():.1%}")

    F = build_panel(dates, codes, fund_rows, med)
    ret_h = fwd(close, args.holding)

    # ---- 观测期（与 validate 同口径）----
    by_period = defaultdict(list)
    for r in fund_rows:
        by_period[r[1]].append(effective_date(r[1], r[2]))
    periods = []
    for rd in sorted(by_period):
        effs = sorted(e for e in by_period[rd] if e)
        if effs:
            periods.append((rd, effs[len(effs) // 2]))
    obs_idx = []
    for rd, eff in periods:
        i = int(np.searchsorted(dates, eff, side="right"))
        if t0 <= i < T - args.holding:
            obs_idx.append(i)
    obs_idx = sorted(set(obs_idx))
    keep = []
    for i in obs_idx:
        if not keep or i - keep[-1] >= args.holding * 0.8:
            keep.append(i)
    obs_idx = keep
    obs_dates = [dates[i][:10] for i in obs_idx]
    print(f"报告期 {len(periods)} 个 → 非重叠有效观测 {len(obs_idx)} 期"
          f"  [{obs_dates[0]} ~ {obs_dates[-1]}]\n")

    # ---- 准备 z 面板 ----
    Z = {fk: cross_section_z(F[fk], tradable) for fk, _ in FACTORS}
    STY_raw = compute_factors(close, volume, amount, turn)

    # ★ 自由流通市值（规模因子）：此前正交化漏掉了规模，而「买最差成长」最可能的真身
    #   就是「买小盘」。用【不复权】的成交额与换手率反推，避开复权因子扭曲：
    #       turnover_pct = volume / 自由流通股数 × 100
    #       amount      ≈ price × volume
    #   ⇒ 自由流通市值 = price × 自由流通股数 ≈ amount × 100 / turnover_pct
    #   取对数后截面 z-score。缺失填截面均值。
    with np.errstate(divide="ignore", invalid="ignore"):
        ff_mcap = np.where((turn > 0) & np.isfinite(amount) & np.isfinite(turn),
                           amount * 100.0 / np.where(turn > 0, turn, np.nan), np.nan)
    STY_raw["log_size"] = np.log(np.where(ff_mcap > 0, ff_mcap, np.nan))
    STY_raw["log_size"] = np.where(np.isfinite(STY_raw["log_size"]),
                                   STY_raw["log_size"], np.nan)
    cov = float(np.isfinite(STY_raw["log_size"]).mean())
    print(f"自由流通市值覆盖率: {cov:.1%}"
          f"   中位市值 {np.nanmedian(ff_mcap)/1e8:.1f} 亿元"
          if np.isfinite(ff_mcap).any() else
          f"自由流通市值覆盖率: {cov:.1%}")

    # ★ 宽基池回退：宽池库没有 daily_liquidity → turnover_pct 全 NaN，
    #   上面的反推会得到全 NaN，规模维度【静默消失】，正交化形同没做规模中性化。
    #   此时改用基本面面板的 log(营业收入) 做规模代理（与市值高度相关）。
    SIZE_KEY = "log_size"
    size_src = "amount×100/turnover_pct 反推的自由流通市值"
    size_ok = cov >= 0.20
    if not size_ok and np.isfinite(F.get("size_logrev", np.array([]))).mean() > cov:
        STY_raw["log_size"] = F["size_logrev"]
        size_src = "log(营业收入)（宽池无换手率，规模代理）"
        cov = float(np.isfinite(STY_raw["log_size"]).mean())
        size_ok = cov >= 0.20
        print(f"  ⚠️ 换手率缺失 → 规模因子回退为 {size_src}，覆盖率 {cov:.1%}")
    if not size_ok:
        print("  ⚠️⚠️ 规模因子不可用：换手率与营收均缺失 → 正交化【不含规模维】，"
              "结论需按此打折")

    # ⚠️ 只有规模可用时才把 log_size 放进正交化基；否则基里少一维，
    #    但绝不能拿一个全 NaN 的列去回归（lstsq 会得到垃圾解且静默通过）。
    basis_keys = STYLE_KEYS + (["log_size"] if size_ok else [])
    SZ = {k: cross_section_z(STY_raw[k], tradable) for k in basis_keys}

    report = {"generated_at": datetime.now().isoformat(timespec="seconds"),
              "holding": args.holding, "n_obs": len(obs_idx),
              "obs_dates": obs_dates}

    # ============ A. 逐期 IC + 剔除极端期 ============
    print("=" * 78)
    print("A. 逐期 IC 序列 · 剔除极端期检验")
    print("=" * 78)
    print(f"  {'因子':<34}{'N':>5}{'IC均值':>9}{'IC_std':>8}"
          f"{'MDE':>8}{'比值':>8}{'t':>8}  显著")
    print("  " + "-" * 88)
    report["A"] = {}
    for fk in MAIN:
        ics, _ = spearman_ic(Z[fk][obs_idx], ret_h[obs_idx], tradable[obs_idx])
        base = summarize(ics, fk)
        row(base)
        if base is None:
            continue
        arr = np.asarray(ics, dtype=float)
        # 剔除绝对值最大的 2 期
        drop = np.argsort(-np.abs(arr))[:2]
        trimmed = np.delete(arr, drop)
        tr = summarize(trimmed, fk + " ·剔最极端2期")
        row(tr, f"   剔掉 {obs_dates[drop[0]]} / {obs_dates[drop[1]]}")
        report["A"][fk] = {
            "base": base, "trimmed": tr,
            "dropped_dates": [obs_dates[int(i)] for i in drop],
            "ic_series": [round(float(v), 5) for v in arr],
        }
    print("\n  逐期 IC（主因子 np_yoy_wins）:")
    if "np_yoy_wins" in report["A"]:
        ser = report["A"]["np_yoy_wins"]["ic_series"]
        for d, v in zip(obs_dates, ser):
            bar = "█" * int(abs(v) * 100)
            print(f"    {d}  {v:+.4f}  {bar}")

    # ============ B. 时间对半拆分 + 分年度 ============
    print("\n" + "=" * 78)
    print("B. 子样本稳定性")
    print("=" * 78)
    half = len(obs_idx) // 2
    print(f"  {'因子':<34}{'N':>5}{'IC均值':>9}{'IC_std':>8}"
          f"{'MDE':>8}{'比值':>8}{'t':>8}  显著")
    print("  " + "-" * 88)
    report["B"] = {}
    for fk in MAIN:
        ics, _ = spearman_ic(Z[fk][obs_idx], ret_h[obs_idx], tradable[obs_idx])
        arr = np.asarray(ics, dtype=float)
        h1 = summarize(arr[:half], fk + " ·前半段")
        h2 = summarize(arr[half:], fk + " ·后半段")
        row(h1, f"   ≤{obs_dates[half-1]}")
        row(h2, f"   ≥{obs_dates[half]}")
        report["B"][fk] = {"first_half": h1, "second_half": h2,
                           "split_at": obs_dates[half]}

    # 分年度
    print("\n  分年度 IC（np_yoy_wins）:")
    yearly = defaultdict(list)
    for d, v in zip(obs_dates, report["A"]["np_yoy_wins"]["ic_series"]):
        yearly[d[:4]].append(v)
    pos_y = 0
    for y in sorted(yearly):
        v = yearly[y]
        m = float(np.mean(v))
        pos_y += 1 if m < 0 else 0
        print(f"    {y}  n={len(v)}  IC均值 {m:+.4f}  {'负' if m < 0 else '正'}")
    print(f"  → 年度符号与全样本一致(负): {pos_y}/{len(yearly)}")
    report["B"]["yearly_np_yoy"] = {y: [round(float(x), 5) for x in v]
                                    for y, v in yearly.items()}
    report["B"]["yearly_neg_frac"] = f"{pos_y}/{len(yearly)}"

    # ============ C. 缩尾敏感性 ============
    print("\n" + "=" * 78)
    print("C. 缩尾水平敏感性（仅影响 np_yoy）")
    print("=" * 78)
    print(f"  {'缩尾分位':<34}{'N':>5}{'IC均值':>9}{'IC_std':>8}"
          f"{'MDE':>8}{'比值':>8}{'t':>8}  显著")
    print("  " + "-" * 88)
    report["C"] = {}
    for q in (0.01, 0.05, 0.10):
        Fw = build_panel(dates, codes, fund_rows, med, wins_q=q)
        Zw = cross_section_z(Fw["np_yoy_wins"], tradable)
        ics, _ = spearman_ic(Zw[obs_idx], ret_h[obs_idx], tradable[obs_idx])
        d = summarize(ics, f"np_yoy 缩尾 q={q}")
        row(d)
        report["C"][str(q)] = d

    # ============ D. 风格正交化 ============
    print("\n" + "=" * 78)
    print(f"D. 对 {len(STYLE_KEYS)} 个价量风格因子正交化后重算 IC")
    print("=" * 78)
    print(f"  {'因子':<34}{'N':>5}{'IC均值':>9}{'IC_std':>8}"
          f"{'MDE':>8}{'比值':>8}{'t':>8}  显著")
    print("  " + "-" * 88)
    report["D"] = {}
    basis_choices = ([("9 个价量风格", STYLE_KEYS)] if not size_ok else
                     [("9 个价量风格", STYLE_KEYS),
                      (f"9 个价量风格 + 规模({size_src})", basis_keys)])
    for basis_name, keys in basis_choices:
        print(f"\n  ── 正交化基：{basis_name} ──")
        style_list = [SZ[k] for k in keys]
        for fk in MAIN:
            ics0, _ = spearman_ic(Z[fk][obs_idx], ret_h[obs_idx], tradable[obs_idx])
            base = summarize(ics0, fk + " ·原始")
            row(base)
            Zr, r2s = ortho_residual(Z[fk], style_list, tradable)
            ics1, _ = spearman_ic(Zr[obs_idx], ret_h[obs_idx], tradable[obs_idx])
            orth = summarize(ics1, fk + " ·正交化后")
            row(orth)
            if base and orth:
                keep_ratio = (abs(orth["ic_mean"]) / abs(base["ic_mean"])
                              if base["ic_mean"] else float("nan"))
                r2 = float(np.mean(r2s)) if r2s else float("nan")
                print(f"    → 信号落在风格空间的比例 R² = {r2:.1%}；"
                      f"正交化后保留 {keep_ratio:.1%} 的 IC；"
                      f"t 从 {base['t_hac']:+.2f} → {orth['t_hac']:+.2f}")
                report["D"].setdefault(basis_name, {})[fk] = {
                    "base": base, "orth": orth,
                    "r2_in_style_space": round(r2, 3),
                    "ic_retained": round(float(keep_ratio), 3)}

    # 规模相关性：因子 z 与规模 z 的逐期截面相关
    corrs = []
    if size_ok:
        for t in obs_idx:
            m = (tradable[t] & np.isfinite(Z["np_yoy_wins"][t])
                 & np.isfinite(SZ["log_size"][t]))
            if m.sum() < 20:
                continue
            a, b = Z["np_yoy_wins"][t][m], SZ["log_size"][t][m]
            if a.std() > 0 and b.std() > 0:
                corrs.append(float(((a - a.mean()) * (b - b.mean())).mean()
                                   / (a.std() * b.std())))
    elif not size_ok:
        print("\n  （规模因子不可用，跳过规模相关性检验）")
    if corrs:
        print(f"\n  np_yoy 与规模(log市值)的逐期截面相关: 均值 {np.mean(corrs):+.3f}"
              f"  （负 = 低成长股偏小盘）")
        report["D"]["size_corr_np_yoy"] = round(float(np.mean(corrs)), 4)

    # ============ E. 五分位经济性 ============
    print("\n" + "=" * 78)
    print("E. 五分位价差的经济量级（Q1 = 因子最低 20%，Q5 = 最高 20%）")
    print("=" * 78)
    report["E"] = {}
    for fk in MAIN:
        d = quintile_spread(Z[fk], ret_h, tradable, dates, obs_idx,
                            args.holding, fk)
        report["E"][fk] = d
        if d is None:
            print(f"  {fk:<20}  观测不足")
            continue
        print(f"  {fk:<16} 基准 {d['bench_ann']:+.2%}  Q1 {d['q1_ann']:+.2%}"
              f"  Q5 {d['q5_ann']:+.2%}   价差 {d['spread_ann']:+.2%}/年"
              f"  t={d['t']:+.2f} 为正 {d['pos_frac']:.0%}")
        print(f"  {'':<16} 多头 Q1 超额 {d['q1_excess_ann']:+.2%}/年"
              f"  t={d['q1_excess_t']:+.2f}  为正 {d['q1_excess_pos_frac']:.0%}  ← 可交付口径")
    print("\n  注：价差为【毛】收益，未扣交易成本。A股往返费率 0.30%~0.80%，")
    print("      基本面策略年换手约 2~4 倍 → 年成本约 0.6%~3.2%。")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"ashare_fund_robust_h{args.holding}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出: {out}")


if __name__ == "__main__":
    main()
