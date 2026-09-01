#!/usr/bin/env python3
"""统计功效分析：判定「无 alpha」是信息性结论还是功效不足（2026-09-01）。

动机（用户提醒）：不要一味扩充数据，先评估卡点在哪。
本项目已跑 5+ 正交框架全为负结果，但**负结果有两种完全不同的含义**：
  - 信息性零结果：样本足以检出真实量级的效应却没检出 → 真无效应
  - 功效不足：样本根本检不出真实量级的效应 → 无法下结论（不是没效，是看不见）

核心指标 MDE（最小可检测效应，two-sided α=0.05、power=0.80）：
    MDE = (t_{1-α/2, N-1} + t_{power, N-1}) · σ_IC / √N
其中 σ_IC 为「每期截面 IC 的标准差」，N 为独立期数（期级）或独立窗口数（价格）。

σ_IC 的构成（关键）：
    σ_IC² ≈ 估计噪声² + 真实时变²，估计噪声 ≈ 1/√(截面标的数 - 1)
→ 扩宽横截面降低 σ（110 只扩池的价值在此），但 √N 才是主要杠杆；
  半年度数据一年只有 2 期，N 增长极慢 = 功效天花板。

真实因子基准（业界经验）：月频 IC 典型 0.02~0.05，ICIR 0.1~0.5。
若 MDE 远大于 0.05 → 该框架检不出任何真实因子 = 无效投入。

用法:
  equity_power.py audit [--json out]
  equity_power.py self-test
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from equity_quant import period_level_ic_neutralized, cross_sectional_ic, _mean, _std  # noqa: E402

DB_PATH = Path(__file__).resolve().parents[4] / "outputs" / "equity_fundamental.sqlite"

ALPHA = 0.05
POWER = 0.80
# 真实世界基本面/价格因子的典型 IC 量级（业界经验区间）
PLAUSIBLE_IC_LO, PLAUSIBLE_IC_HI = 0.02, 0.05

FUNDAMENTAL_FACTORS = ["revenue_yoy", "gross_margin", "net_margin_proxy",
                       "ocf_to_revenue", "asset_liability_ratio",
                       "roe", "roa", "equity_ratio", "net_profit_yoy"]
PRICE_FACTORS = ["momentum_20d", "momentum_60d", "volatility_20d",
                 "volume_ratio_20d", "price_to_ma20"]


def _t_quantile(p: float, df: int) -> float:
    """t 分布分位数（正态近似 + Cornish-Fisher 校正，无 scipy 依赖）。"""
    # 正态分位数（Acklam 逆 CDF 二分）
    lo, hi = -8.0, 8.0
    for _ in range(80):
        mid = (lo + hi) / 2
        phi = 0.5 * (1 + math.erf(mid / math.sqrt(2)))
        if phi < p:
            lo = mid
        else:
            hi = mid
    z = (lo + hi) / 2
    if df <= 0:
        return z
    # Cornish-Fisher t 展开：t ≈ z + (z³+z)/(4ν) + (5z⁵+16z³+3z)/(96ν²)
    nu = float(df)
    return z + (z ** 3 + z) / (4 * nu) + (5 * z ** 5 + 16 * z ** 3 + 3 * z) / (96 * nu ** 2)


def mde(n: int, sigma: float, alpha: float = ALPHA, power: float = POWER) -> float | None:
    """最小可检测 |IC|（two-sided）。n<3 时无法估计，返回 None。"""
    if n < 3 or sigma <= 0:
        return None
    df = n - 1
    return (_t_quantile(1 - alpha / 2, df) + _t_quantile(power, df)) * sigma / math.sqrt(n)


def required_n(target_ic: float, sigma: float, alpha: float = ALPHA,
               power: float = POWER, cap: int = 400) -> int | None:
    """检出 target_ic 所需独立期数（迭代求解，超过 cap 判为不可行）。"""
    if target_ic <= 0 or sigma <= 0:
        return None
    for n in range(3, cap + 1):
        m = mde(n, sigma, alpha, power)
        if m is not None and m <= target_ic:
            return n
    return None


def verdict(observed: float | None, m: float | None, n: int) -> str:
    if m is None:
        return "样本不足"
    if observed is not None and abs(observed) >= m:
        return f"检出信号(|IC|≥MDE)"
    if m <= PLAUSIBLE_IC_LO:
        return "信息性零结果（可排除真实量级效应）"
    if m <= PLAUSIBLE_IC_HI:
        return "弱信息性（仅能排除较大效应）"
    return f"功效不足（检不出 {PLAUSIBLE_IC_LO}~{PLAUSIBLE_IC_HI} 量级真实因子）"


def _spearman_local(xs: list[float], ys: list[float]) -> float:
    """本地 Spearman（避免额外依赖，供 compare 使用）。"""
    def rank(v: list[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return num / (dx * dy) if dx > 0 and dy > 0 else 0.0


def _winsor_z(vals: list[float], lo: float = 0.01, hi: float = 0.99) -> list[float]:
    """截面 winsorize（1%/99%）+ z-score，抑制极端值支配回归。"""
    if len(vals) < 5:
        return vals
    s = sorted(vals)
    a, b = s[int(len(s) * lo)], s[min(len(s) - 1, int(len(s) * hi))]
    clipped = [min(max(v, a), b) for v in vals]
    m = sum(clipped) / len(clipped)
    sd = math.sqrt(sum((v - m) ** 2 for v in clipped) / len(clipped))
    return [(v - m) / sd for v in clipped] if sd > 0 else [0.0] * len(clipped)


def _dem(v: list[float], keys: list[str]) -> list[float]:
    agg: dict[str, list[float]] = {}
    for x, k in zip(v, keys):
        agg.setdefault(k, []).append(x)
    avg = {k: sum(g) / len(g) for k, g in agg.items()}
    return [x - avg[k] for x, k in zip(v, keys)]


def _ols_slope(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 5:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx


def _ols_residual_local(xs: list[float], ys: list[float]) -> list[float]:
    """y 对 x 做 OLS 后的残差（本地实现）。"""
    n = len(xs)
    if n < 3:
        return ys
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return ys
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return [y - (my + b * (x - mx)) for x, y in zip(xs, ys)]


def run_compare(db: Path, horizon: str = "fwd_20d") -> dict:
    """多方案比对（用户要求：先比对验证再择优推进）。

    同一份数据、同一评价指标，比较 6 种估计量：
      s1_rank_ic          秩 IC（现状基线，size+sector 中性）
      s2_spread30         多空 30% 收益差（行业中性）
      s3_spread30_size    多空 30% 收益差（市值+行业双中性）
      s4_spread10         多空 10% 分位（更极端）
      s5_fm_slope         Fama-MacBeth 截面回归斜率（因子每期标准化，每 SD）
      s6_fm_slope_wz      winsor(1%/99%)+标准化后 FM 斜率
    择优依据（**不能用 detect_ratio**）：
      detect_ratio = |效应|/MDE ≡ |t|/(t_α+t_β)，排的是**显著性**而非功效，
      且各方案量纲不同无法横比 —— 已废弃。
    改用 **power_gap = MDE / plausible_target**（各方案按自身量纲设目标）：
      s1 IC 目标 0.03；s2/s3/s4 收益差目标 0.006/期；s5/s6 斜率目标 0.003/(SD·期)。
    power_gap 越小 = 越接近能检出真实量级因子；并给出 required_n 折算所需年数。
    """
    # 各方案的「真实量级目标」（量纲对齐：均按 IC≈0.03 的真实因子反推）
    TARGET = {"s1_rank_ic": 0.03, "s2_spread30": 0.006, "s3_spread30_size": 0.006,
              "s4_spread10": 0.006, "s5_fm_slope": 0.003, "s6_fm_slope_wz": 0.003}
    # 每年独立期数：基本面半年度 2 期/年；价格因子 20 日窗口 ≈12.6 期/年
    PER_YEAR = {True: 2.0, False: 12.6}
    from equity_quant import _load_panel_meta
    from equity_sector_het import _effective_dates
    schemes = ["s1_rank_ic", "s2_spread30", "s3_spread30_size",
               "s4_spread10", "s5_fm_slope", "s6_fm_slope_wz"]
    out: dict = {"schemes": schemes, "by_scheme": {}, "by_factor": {}}

    factors = ["roe", "ocf_to_revenue", "gross_margin", "net_profit_yoy",
               "revenue_yoy", "momentum_20d", "volume_ratio_20d"]
    for f in factors:
        panel = _load_panel_meta(db, f, horizon, 5)
        if not panel:
            continue
        eff = _effective_dates(db, f)
        dates = sorted(panel.keys())
        windows = ([next((x for x in dates if x >= e), None) for _, e in eff]
                   if f not in PRICE_FACTORS else dates[::20])
        windows = [w for w in windows if w]
        series: dict[str, list[float]] = {s: [] for s in schemes}
        for d in windows:
            rows = [(v, r, sec, lm) for tk, v, r, sec, lm in panel.get(d, []) if lm is not None]
            if len(rows) < 8:
                continue
            fs = [x[0] for x in rows]
            rs = [x[1] for x in rows]
            secs = [x[2] for x in rows]
            lms = [x[3] for x in rows]
            fs_d, rs_d = _dem(fs, secs), _dem(rs, secs)
            fr = _ols_residual_local(lms, fs_d)
            rr = _ols_residual_local(lms, rs_d)
            series["s1_rank_ic"].append(_spearman_local(fr, rr))
            for q, key in ((0.3, "s2_spread30"), (0.1, "s4_spread10")):
                items = sorted(zip(fs_d, rs_d), key=lambda x: x[0])
                k = max(2, int(len(items) * q))
                lo, hi = items[:k], items[-k:]
                series[key].append(sum(x[1] for x in hi) / len(hi)
                                   - sum(x[1] for x in lo) / len(lo))
            items = sorted(zip(fr, rr), key=lambda x: x[0])
            k = max(2, int(len(items) * 0.3))
            lo, hi = items[:k], items[-k:]
            series["s3_spread30_size"].append(sum(x[1] for x in hi) / len(hi)
                                              - sum(x[1] for x in lo) / len(lo))
            # s5/s6：因子每期标准化（每 SD 的收益斜率，量纲可比）
            fz = _winsor_z(fr, 0.0, 1.0)  # 仅 z-score，不截尾
            sl = _ols_slope(fz, rr)
            if sl is not None:
                series["s5_fm_slope"].append(sl)
            sl2 = _ols_slope(_winsor_z(fr), rr)
            if sl2 is not None:
                series["s6_fm_slope_wz"].append(sl2)

        out["by_factor"][f] = {}
        is_fund = f not in PRICE_FACTORS
        for s in schemes:
            vals = series[s]
            n = len(vals)
            if n < 3:
                out["by_factor"][f][s] = {"n": n, "error": "样本不足"}
                continue
            m, sd = _mean(vals), _std(vals)
            t = m / (sd / math.sqrt(n)) if sd > 0 else 0.0
            mm = mde(n, sd)
            tgt = TARGET[s]
            rn = required_n(tgt, sd, cap=2000)
            out["by_factor"][f][s] = {
                "n": n, "mean": round(m, 6), "std": round(sd, 6),
                "t": round(t, 4), "mde": round(mm, 6) if mm else None,
                "target": tgt,
                "power_gap": (round(mm / tgt, 2) if mm else None),
                "required_n": rn,
                "required_years": (round(rn / PER_YEAR[is_fund], 1) if rn else None)}
    for s in schemes:
        gaps = [d[s].get("power_gap") for d in out["by_factor"].values()
                if isinstance(d.get(s), dict) and d[s].get("power_gap") is not None]
        yrs = [d[s].get("required_years") for d in out["by_factor"].values()
               if isinstance(d.get(s), dict) and d[s].get("required_years") is not None]
        out["by_scheme"][s] = {
            "n_factors": len(gaps),
            "mean_power_gap": round(sum(gaps) / len(gaps), 2) if gaps else None,
            "min_power_gap": round(min(gaps), 2) if gaps else None,
            "median_required_years": (sorted(yrs)[len(yrs) // 2] if yrs else None),
        }
    return out


def run_spread(db: Path, horizon: str = "fwd_20d", quantile: float = 0.3) -> dict:
    """多空收益差口径（代替 IC 秩统计量）的期级检验 + 功效。

    为什么更合理：IC 是**秩相关**，把经济量级压成了 [-1,1] 的噪声统计量；
    多空组合收益差直接是经济量级（每期 %），跨期标准差更小 → 同一 N 下功效更高。
    这是「不靠扩数据、改用更好估计量」提效的核心候选。

    口径：每期截面上按因子排序，取前后各 quantile 分位，算多头-空头的
    forward return 均值差（收益已按行业去均值，与既有纪律一致）。
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from equity_quant import _load_panel_sector
    from equity_sector_het import _effective_dates
    out: dict = {}
    for f in FUNDAMENTAL_FACTORS + PRICE_FACTORS:
        panel = _load_panel_sector(db, f, horizon, 5)
        eff = _effective_dates(db, f) if f in FUNDAMENTAL_FACTORS else None
        all_dates = sorted(panel.keys())
        if eff is None:  # 价格因子：按固定步长切非重叠窗口
            step = 20
            windows = all_dates[::step]
        else:
            windows = [next((x for x in all_dates if x >= e), None) for _, e in eff]
            windows = [w for w in windows if w]
        spreads: list[float] = []
        for d in windows:
            items = [(v, r, s) for tk, v, r, s in panel.get(d, [])]
            if len(items) < 8:
                continue
            # 行业内去均值（收益端），与既有行业中性化纪律一致
            by_sector: dict[str, list[float]] = {}
            for _, r, s in items:
                by_sector.setdefault(s, []).append(r)
            savg = {k: sum(v) / len(v) for k, v in by_sector.items()}
            items = [(v, r - savg[s], s) for v, r, s in items]
            items.sort(key=lambda x: x[0])
            k = max(2, int(len(items) * quantile))
            lo = items[:k]
            hi = items[-k:]
            spread = (sum(x[1] for x in hi) / len(hi)) - (sum(x[1] for x in lo) / len(lo))
            spreads.append(spread)
        n = len(spreads)
        if n < 3:
            out[f] = {"n_periods": n, "error": "样本不足"}
            continue
        m, sd = _mean(spreads), _std(spreads)
        t = m / (sd / math.sqrt(n)) if sd > 0 else 0.0
        mm = mde(n, sd)
        out[f] = {"n_periods": n, "spread_mean": round(m, 6), "spread_std": round(sd, 6),
                  "t": round(t, 4), "mde": round(mm, 6) if mm else None,
                  "annualized_alpha_pct": round(m * (12 if f in FUNDAMENTAL_FACTORS else 12.6) * 100, 2),
                  "verdict": ("检出信号" if abs(t) >= _t_quantile(0.975, n - 1) else "无信号")}
    return out


def run_audit(db: Path) -> dict:
    out: dict = {"alpha": ALPHA, "power": POWER,
                 "plausible_ic": [PLAUSIBLE_IC_LO, PLAUSIBLE_IC_HI],
                 "fundamentals": {}, "prices": {}}

    # ---- 基本面：期级 IC（size_sector 最严口径）----
    for f in FUNDAMENTAL_FACTORS:
        r = period_level_ic_neutralized(db, f, "fwd_20d")
        ss = r.get("size_sector") or {}
        ics = list((ss.get("ic_by_period") or {}).values())
        n = ss.get("n_periods") or len(ics)
        if n < 3 or not ics:
            out["fundamentals"][f] = {"n_periods": n, "error": "样本不足"}
            continue
        sigma = _std(ics)
        m = mde(n, sigma)
        out["fundamentals"][f] = {
            "n_periods": n, "ic_mean": ss.get("ic_mean"), "ic_std": round(sigma, 4),
            "mde": round(m, 4) if m else None,
            "need_n_for_ic_0.03": required_n(0.03, sigma),
            "need_n_for_ic_0.05": required_n(0.05, sigma),
            "verdict": verdict(ss.get("ic_mean"), m, n),
        }

    # ---- 价格：非重叠窗口 IC（独立窗口数 = 有效 N）----
    for f in PRICE_FACTORS:
        r = cross_sectional_ic(db, f, "fwd_20d", nonoverlap=True)
        ics = [v for _, v in (r.get("ic_series") or [])]
        n = len(ics)
        if n < 3:
            out["prices"][f] = {"n_windows": n, "error": "样本不足"}
            continue
        sigma = _std(ics)
        m = mde(n, sigma)
        out["prices"][f] = {
            "n_windows": n, "ic_mean": round(_mean(ics), 4), "ic_std": round(sigma, 4),
            "mde": round(m, 4) if m else None,
            "t_nw": r.get("ic_tstat_nw"),
            "verdict": verdict(_mean(ics), m, n),
        }
    return out


def self_test() -> bool:
    """已知答案：MDE 公式与单调性、required_n、t 分位数。"""
    checks = []
    # 1) t 分位数：df 大时趋近正态 1.96
    checks.append(("t(0.975, df=1000)≈1.96", abs(_t_quantile(0.975, 1000) - 1.96) < 0.01))
    # 2) t 分位数：df=11 时 ≈2.20（教科书值）
    checks.append(("t(0.975, df=11)≈2.20", abs(_t_quantile(0.975, 11) - 2.201) < 0.02))
    # 3) MDE 随 N 增大而下降（功效提升）
    m12, m48 = mde(12, 0.15), mde(48, 0.15)
    checks.append(("MDE 随 N 下降", m12 is not None and m48 is not None and m48 < m12))
    # 4) MDE 随 σ 增大而上升（噪声降低功效）
    checks.append(("MDE 随 σ 上升", mde(12, 0.30) > mde(12, 0.15)))
    # 5) MDE 量级合理：N=12,σ=0.15 → ≈0.134
    checks.append(("MDE(12, 0.15)≈0.13~0.14", 0.12 < mde(12, 0.15) < 0.15))
    # 6) required_n：σ=0.15 检 IC=0.05 需 ~68 期（半年度≈34 年 → 不可行）
    n5 = required_n(0.05, 0.15)
    checks.append((f"检出 IC=0.05 需 {n5} 期（>50 判不可行）", n5 is not None and n5 > 50))
    # 7) verdict 分类
    checks.append(("MDE>0.05 判功效不足", "功效不足" in verdict(0.01, 0.13, 12)))
    checks.append(("MDE<=0.02 判信息性", "信息性" in verdict(0.005, 0.02, 100)))
    ok = all(c for _, c in checks)
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print(f"[self-test] {'ALL PASS' if ok else 'FAILED'} ({sum(c for _, c in checks)}/{len(checks)})")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", nargs="?", default="audit",
                    choices=["audit", "spread", "compare", "self-test"])
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    if args.cmd == "self-test":
        raise SystemExit(0 if self_test() else 1)

    if args.cmd == "compare":
        res = run_compare(Path(args.db))
        print("== 多方案比对（power_gap = MDE / 真实量级目标，越小越有效；required_years = 检出真实因子所需年数）==")
        print(f"{'factor':20s}" + "".join(f"{s.replace('s','S',1):>22s}" for s in res["schemes"]))
        print(f"{'':20s}" + "".join(f"{'gap/年数':>22s}" for _ in res["schemes"]))
        for f, d in res["by_factor"].items():
            line = f"{f:20s}"
            for s in res["schemes"]:
                v = d.get(s, {})
                if v.get("power_gap") is not None:
                    line += f"{('%gx%s年' % (v['power_gap'], v['required_years'])):>22s}"
                else:
                    line += f"{'n/a':>22s}"
            print(line)
        print("\n-- 方案汇总（mean_power_gap 越小越优；median_required_years 直观化）--")
        for s, v in sorted(res["by_scheme"].items(),
                           key=lambda kv: (kv[1]["mean_power_gap"] or 9e9)):
            print(f"  {s:20s} n={v['n_factors']}  mean_power_gap={v['mean_power_gap']:>6}"
                  f"  min={v['min_power_gap']:>6}  中位所需年数={v['median_required_years']}")
        if args.json:
            Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=2,
                                                  default=str), encoding="utf-8")
            print(f"\n[power] 已落盘 {args.json}")
        return

    if args.cmd == "spread":
        res = run_spread(Path(args.db))
        print("== 多空收益差口径（每期行业中性化，前后 30% 分位）==")
        print(f"{'factor':22s} {'N期':>4s} {'每期收益差':>10s} {'σ':>8s} {'t':>7s} "
              f"{'MDE':>8s} {'年化α%':>8s}  结论")
        for f, d in res.items():
            if "error" in d:
                print(f"{f:22s} {d['n_periods']:>4}  {d['error']}")
                continue
            print(f"{f:22s} {d['n_periods']:>4d} {d['spread_mean']*100:>9.3f}% "
                  f"{d['spread_std']*100:>7.3f}% {d['t']:>7.2f} {d['mde']*100:>7.3f}% "
                  f"{d['annualized_alpha_pct']:>8.2f}  {d['verdict']}")
        if args.json:
            Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=2,
                                                  default=str), encoding="utf-8")
            print(f"\n[power] 已落盘 {args.json}")
        return

    res = run_audit(Path(args.db))
    print(f"== 功效分析（α={ALPHA}, power={POWER}；真实因子典型 IC "
          f"{PLAUSIBLE_IC_LO}~{PLAUSIBLE_IC_HI}）==")
    print("\n-- 基本面（期级 IC，size_sector 口径）--")
    print(f"{'factor':22s} {'N期':>4s} {'IC均值':>8s} {'ICσ':>7s} {'MDE':>7s} "
          f"{'需N(IC=.03)':>11s}  结论")
    for f, d in res["fundamentals"].items():
        if "error" in d:
            print(f"{f:22s} {d['n_periods']:>4}  {d['error']}")
            continue
        print(f"{f:22s} {d['n_periods']:>4d} {d['ic_mean']:>8.4f} {d['ic_std']:>7.4f} "
              f"{d['mde']:>7.4f} {str(d['need_n_for_ic_0.03']):>11s}  {d['verdict']}")
    print("\n-- 价格因子（非重叠 20 日窗口）--")
    print(f"{'factor':22s} {'N窗':>4s} {'IC均值':>8s} {'ICσ':>7s} {'MDE':>7s} {'NW t':>7s}  结论")
    for f, d in res["prices"].items():
        if "error" in d:
            print(f"{f:22s} {d['n_windows']:>4}  {d['error']}")
            continue
        print(f"{f:22s} {d['n_windows']:>4d} {d['ic_mean']:>8.4f} {d['ic_std']:>7.4f} "
              f"{d['mde']:>7.4f} {(d['t_nw'] or 0):>7.2f}  {d['verdict']}")

    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str),
                                   encoding="utf-8")
        print(f"\n[power] 已落盘 {args.json}")


if __name__ == "__main__":
    main()
