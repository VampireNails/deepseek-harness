# -*- coding: utf-8 -*-
"""
ashare_altdata_preflight.py — 方向 2 工序 3：另类数据（两融）独立信息层级预检 v2

读 outputs/ashare_altdata.sqlite(margin_daily) + outputs/ashare_csi800_hfq_xq.sqlite(daily_quotes_hfq)，
对两融派生因子做四道检验（沿用 SOP 关卡① 口径 + 项目统计核心）：

  ① 截面 IC 显著性（按期聚合，n=期数）
  ② 随机 x 负向测试（IC sd ≈ sqrt(1/(K−1))，防流程伪振幅 — 第十一类 bug 防线）
  ③ 正交化价量代理控制后的增量 IC（判独立层级 — 方向 2 生死关）
  ④ 功效/MDE + 逐年为正占比（可交付门槛 <60% 即不可交付）

v2 修正（2026-09-04 首跑自查发现）：
  - ★ 同样本对照：raw IC 与 orth IC 必须跑在**同一批行**上（控制变量非有限的行
    同时从 raw 与 orth 剔除），否则样本差冒充控制效果。首跑 chg5 orth 崩塌
    可能是样本差而非真崩塌——以 v2 为准。
  - ★ 扩展控制集（ret60）作为**已声明稳健性**：主口径 {ln余额,ret20,ret5}，
    若某因子存活则看 +ret60 是否仍存活（防"控制集没覆盖完整价量信息"）。

口径纪律（写死，勿改）：
  - 因子一律用"当日两融值、t−1 信息集"：两融 T 日余额/买入额收盘后才披露，预测
    close(T)→close(T+h) 前向收益（因子不含 T 之后的任何信息）。
  - 融资余额增速/净买入比全部自归一化，不依赖流通市值字段。
  - 检验仅用"当日有融资余额(rzye>0)"标的（扩容阶梯，不得要求全期覆盖）。
  - IC 结论以 nonoverlap 相位中位数 t 为准（项目保守口径）。

因子（预注册主检 = chg5、netbuy；其余次级/探索）：
  chg5     Δ5 日 ln(融资余额)
  chg20    Δ20 日 ln(融资余额)
  netbuy   (融资买入−融资偿还)/昨融资余额
  grossbuy 融资买入/昨融资余额
  shortr   融券余额/(融资+融券余额)

用法：python ashare_altdata_preflight.py [--horizons 5,20] [--outdir ...]
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from equity_quant import _spearman, newey_west_tstat  # noqa: E402

ALTDB = "outputs/ashare_altdata.sqlite"
PXDB = "outputs/ashare_csi800_hfq_xq.sqlite"


# ---------------------------------------------------------------- 数据装载

def load_calendar_and_close(pxdb: str, start: str = "2013-06-01"):
    """全局交易日历 + 每股后复权收盘（NaN = 停牌/未上市）。"""
    c = sqlite3.connect(pxdb)
    dates = [r[0] for r in c.execute(
        "SELECT DISTINCT trade_date FROM daily_quotes_hfq WHERE trade_date>=? ORDER BY trade_date",
        (start,))]
    pos = {d: i for i, d in enumerate(dates)}
    close = {}  # code -> np.array(close, len(dates))
    cur = c.execute(
        "SELECT code, trade_date, close FROM daily_quotes_hfq WHERE trade_date>=? ORDER BY code, trade_date",
        (start,))
    last_code, arr, n_codes = None, None, 0
    for code, d, px in cur:
        if px is None or (isinstance(px, float) and math.isnan(px)):
            continue
        if code != last_code:
            if arr is not None:
                close[last_code] = arr
            arr = np.full(len(dates), np.nan)
            last_code, n_codes = code, n_codes + 1
        arr[pos[d]] = float(px)
    if arr is not None:
        close[last_code] = arr
    c.close()
    return dates, pos, close


def load_margin_map(altdb: str, start: str, pos: dict) -> dict:
    c = sqlite3.connect(altdb)
    rows = c.execute(
        "SELECT code, trade_date, rzye, rzmre, rzche, rqye FROM margin_daily "
        "WHERE trade_date>=? AND rzye>0 ORDER BY code, trade_date", (start,)).fetchall()
    c.close()
    M: dict[str, dict[int, tuple]] = {}
    for code, d, rzye, rzmre, rzche, rqye in rows:
        if d not in pos:
            continue
        M.setdefault(code, {})[pos[d]] = (float(rzye), float(rzmre), float(rzche), float(rqye))
    return M


# ---------------------------------------------------------------- 检验工具

def _f(x):
    return x is not None and math.isfinite(x)


def phase_tstats(ics: list[float], stride: int) -> list[float]:
    """nonoverlap 相位 t：逐日 IC 按相位(stride=持有期)切分，每相内部互不重叠，
    朴素 t = mean/(sd/sqrt(n))。返回各相位 t（结论取中位数，保守口径）。"""
    ts = []
    for ph in range(stride):
        sub = ics[ph::stride]
        n = len(sub)
        if n < 8:
            continue
        m = float(np.mean(sub))
        s = float(np.std(sub, ddof=1))
        if s <= 0:
            continue
        ts.append(m / (s / math.sqrt(n)))
    return ts


def nw_t(ics: list[float], lag: int) -> float | None:
    return newey_west_tstat(ics, lag)


def yearly_stats(ics: list[float], dates: list[str]) -> dict:
    yr: dict[str, list[float]] = {}
    for d, v in zip(dates, ics):
        yr.setdefault(d[:4], []).append(v)
    out = {}
    for y in sorted(yr):
        vals = yr[y]
        out[y] = {"n": len(vals), "mean": float(np.mean(vals)),
                  "pos": sum(1 for v in vals if v > 0) / len(vals)}
    return out


def rand_x_sanity(K_list: list[int], seed: int = 42) -> tuple[float, float]:
    """随机 x 负向测试：每期用实际 K 抽标准正态，算截面 IC。
    返回 (realized_sd_of_random_IC, theory_sd)。判据 realized/theory ≤ 1.35。"""
    rng = random.Random(seed)
    rand_ics, theory = [], []
    for K in K_list:
        if K < 10:
            continue
        x = [rng.gauss(0, 1) for _ in range(K)]
        y = [rng.gauss(0, 1) for _ in range(K)]
        rand_ics.append(_spearman(x, y))
        theory.append(1.0 / math.sqrt(K - 1))
    if len(rand_ics) < 10:
        return float("nan"), float("nan")
    return float(np.std(rand_ics, ddof=1)), float(np.mean(theory))


def residualize(controls: list[list[float]], y: list[float]) -> list[float] | None:
    """y 对多控制变量（含截距）OLS 残差。行内任一控制非有限 → 该行残差 NaN。"""
    n = len(y)
    if n < 10:
        return None
    X, Y, keep = [], [], []
    for i in range(n):
        ok = all(_f(cv[i]) for cv in controls) and _f(y[i])
        if ok:
            X.append([1.0] + [float(cv[i]) for cv in controls])
            Y.append(float(y[i]))
            keep.append(i)
    if len(X) < 10:
        return None
    try:
        beta, *_ = np.linalg.lstsq(np.asarray(X), np.asarray(Y), rcond=None)
    except np.linalg.LinAlgError:
        return None
    resid = np.asarray(Y) - np.asarray(X) @ beta
    out = [float("nan")] * n
    for idx, r in zip(keep, resid):
        out[idx] = float(r)
    return out


# ---------------------------------------------------------------- 主流程

def build_factor(cfg: dict, code: str, p: int, M: dict) -> float | None:
    if code not in M:
        return None
    mm = M[code]
    if p not in mm:
        return None
    rzye, rzmre, rzche, rqye = mm[p]
    kind = cfg["kind"]
    try:
        if kind == "chg5":
            if p - 5 not in mm:
                return None
            r0 = mm[p - 5][0]
            return math.log(rzye) - math.log(r0) if r0 > 0 and rzye > 0 else None
        if kind == "chg20":
            if p - 20 not in mm:
                return None
            r0 = mm[p - 20][0]
            return math.log(rzye) - math.log(r0) if r0 > 0 and rzye > 0 else None
        if kind == "netbuy":
            if p - 1 not in mm:
                return None
            r0 = mm[p - 1][0]
            return (rzmre - rzche) / r0 if r0 > 0 else None
        if kind == "grossbuy":
            if p - 1 not in mm:
                return None
            r0 = mm[p - 1][0]
            return rzmre / r0 if r0 > 0 else None
        if kind == "shortr":
            tot = rzye + rqye
            return rqye / tot if tot > 0 else None
    except (TypeError, ZeroDivisionError):
        return None
    return None


def _summary(ics: list[float], dates: list[str], Ks: list[int], horizon: int) -> dict:
    mean_ic = float(np.mean(ics))
    sd_ic = float(np.std(ics, ddof=1))
    ph_t = phase_tstats(ics, horizon)
    n = len(ics)
    se_eff = sd_ic / math.sqrt(n)
    yr = yearly_stats(ics, dates)
    yrs_pos = sum(1 for v in yr.values() if v["mean"] > 0)
    r_sd, r_th = rand_x_sanity(Ks)
    return {
        "n_dates": n,
        "mean_ic": mean_ic,
        "sd_ic": sd_ic,
        "phase_t_median": float(np.median(ph_t)) if ph_t else float("nan"),
        "nw_t": nw_t(ics, min(horizon - 1, 10)),
        "abs_mde_ratio": abs(mean_ic) / (1.96 * se_eff) if se_eff > 0 else float("nan"),
        "rand_sd": r_sd, "rand_theory_sd": r_th,
        "rand_ratio": r_sd / r_th if r_th and r_th > 0 else float("nan"),
        "yearly_pos_share": yrs_pos / len(yr) if yr else float("nan"),
        "yearly_n": len(yr),
        "yearly": yr,
        "mean_K": float(np.mean(Ks)) if Ks else float("nan"),
    }


def run_horizon(dates: list[str], close: dict, M: dict, codes: list[str],
                horizon: int, factors: list[dict], start_year: str = "2014") -> dict:
    n_d = len(dates)
    fwd, ret5, ret20, ret60 = {}, {}, {}, {}
    for code, arr in close.items():
        f = np.full(n_d, np.nan)
        r5 = np.full(n_d, np.nan)
        r20 = np.full(n_d, np.nan)
        r60 = np.full(n_d, np.nan)
        for p in range(n_d - horizon):
            c0, ch = arr[p], arr[p + horizon]
            if _f(c0) and _f(ch):
                f[p] = ch / c0 - 1.0
        for p in range(horizon, n_d):
            c0, c5 = arr[p], arr[p - 5]
            if _f(c0) and _f(c5):
                r5[p] = c0 / c5 - 1.0
        for p in range(20, n_d):
            c0, c20 = arr[p], arr[p - 20]
            if _f(c0) and _f(c20):
                r20[p] = c0 / c20 - 1.0
        for p in range(60, n_d):
            c0, c60 = arr[p], arr[p - 60]
            if _f(c0) and _f(c60):
                r60[p] = c0 / c60 - 1.0
        fwd[code], ret5[code], ret20[code], ret60[code] = f, r5, r20, r60

    t0 = time.time()
    out = {"horizon": horizon, "factors": {}}
    for fcfg in factors:
        fkey = fcfg["key"]
        # 主口径（控制集 {lnbal,ret20,ret5}，raw 与 orth 严格同样本）
        ic3_raw, ic3_orth, d3, k3 = [], [], [], []
        # 稳健性（控制集 +ret60，raw60 与 orth60 严格同样本）
        ic4_raw, ic4_orth, d4, k4 = [], [], [], []
        for p in range(60, n_d - horizon):
            dstr = dates[p]
            if dstr < start_year + "-01-01":
                continue
            rows = []
            for code in codes:
                if code not in M or code not in close:
                    continue
                fv = build_factor(fcfg, code, p, M)
                if fv is None:
                    continue
                fr = fwd[code][p]
                if not _f(fr):
                    continue
                lnb = math.log(M[code][p][0])   # p 在 M ⇒ 有余额 >0
                r20v, r5v = ret20[code][p], ret5[code][p]
                if not (_f(r20v) and _f(r5v)):   # 主口径样本：三控制必须齐全
                    continue
                rows.append((fv, fr, lnb, r20v, r5v, ret60[code][p]))
            K = len(rows)
            if K < 30:
                continue
            xs = [r[0] for r in rows]
            ys = [r[1] for r in rows]
            ic_r = _spearman(xs, ys)
            if not math.isfinite(ic_r):
                continue
            resid = residualize([[r[2] for r in rows], [r[3] for r in rows],
                                 [r[4] for r in rows]], xs)
            if resid is None:
                continue
            ic_o = _spearman(resid, ys)   # 控制齐全 ⇒ resid 无 NaN
            ic3_raw.append(ic_r); ic3_orth.append(ic_o)
            d3.append(dstr); k3.append(K)
            # 扩展控制集：同批行中 ret60 也有限者
            sub = [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows if _f(r[5])]
            if len(sub) >= 30:
                xs4 = [r[0] for r in sub]
                ys4 = [r[1] for r in sub]
                ic_r4 = _spearman(xs4, ys4)
                resid4 = residualize(
                    [[r[2] for r in sub], [r[3] for r in sub],
                     [r[4] for r in sub], [r[5] for r in sub]], xs4)
                if resid4 is not None and math.isfinite(ic_r4):
                    ic_o4 = _spearman(resid4, ys4)
                    ic4_raw.append(ic_r4); ic4_orth.append(ic_o4)
                    d4.append(dstr); k4.append(K)
        if len(ic3_raw) < 30:
            out["factors"][fkey] = {"error": "insufficient dates", "n": len(ic3_raw)}
            continue
        s3 = _summary(ic3_raw, d3, k3, horizon)
        o3 = _summary(ic3_orth, d3, k3, horizon)
        s4 = _summary(ic4_raw, d4, k4, horizon) if len(ic4_raw) >= 30 else None
        o4 = _summary(ic4_orth, d4, k4, horizon) if len(ic4_orth) >= 30 else None
        out["factors"][fkey] = {
            "raw3": {k: s3[k] for k in
                     ("n_dates", "mean_ic", "phase_t_median", "nw_t",
                      "abs_mde_ratio", "rand_ratio", "yearly_pos_share",
                      "yearly_n", "mean_K")},
            "raw3_yearly": s3["yearly"],
            "orth3": {k: o3[k] for k in
                      ("n_dates", "mean_ic", "phase_t_median", "nw_t")},
            "sign_flip3": (o3["mean_ic"] < 0) != (s3["mean_ic"] < 0),
            "raw4": None if s4 is None else {k: s4[k] for k in
                     ("n_dates", "mean_ic", "phase_t_median")},
            "orth4": None if o4 is None else {k: o4[k] for k in
                     ("n_dates", "mean_ic", "phase_t_median")},
            "sign_flip4": None if (o4 is None or s4 is None) else
                          (o4["mean_ic"] < 0) != (s4["mean_ic"] < 0),
        }
    out["elapsed_s"] = time.time() - t0
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", default="5,20")
    ap.add_argument("--outdir", default="outputs/2026-09-04/altdata_preflight")
    args = ap.parse_args()
    horizons = [int(x) for x in args.horizons.split(",") if x]
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    base = Path(__file__).resolve().parent
    altdb = str(base / ALTDB) if not Path(ALTDB).exists() else ALTDB
    pxdb = str(base / PXDB) if not Path(PXDB).exists() else PXDB

    factors = [
        {"key": "chg5", "kind": "chg5", "primary": True},
        {"key": "netbuy", "kind": "netbuy", "primary": True},
        {"key": "chg20", "kind": "chg20", "primary": False},
        {"key": "grossbuy", "kind": "grossbuy", "primary": False},
        {"key": "shortr", "kind": "shortr", "primary": False},
    ]

    print("[1/3] 装载价格日历与收盘...")
    dates, pos, close = load_calendar_and_close(pxdb)
    codes = sorted(close.keys())
    print(f"      日历 {dates[0]}~{dates[-1]} {len(dates)} 日, 有价格标的 {len(codes)} 只")
    print("[2/3] 装载两融...")
    M = load_margin_map(altdb, "2013-06-01", pos)
    print(f"      两融有数据标的 {len([c for c in codes if c in M])} 只")
    all_out = {"factors": factors, "horizons": {}}
    for h in horizons:
        print(f"[3/3] h={h} 全套检验(同样本 v2)...")
        r = run_horizon(dates, close, M, codes, h, factors)
        all_out["horizons"][str(h)] = r
        print(f"      h={h} 完成 耗时 {r['elapsed_s']:.0f}s")
        for fkey, st in r["factors"].items():
            if "error" in st:
                print(f"      {fkey}: {st['error']}")
                continue
            r3, o3, r4, o4 = st["raw3"], st["orth3"], st["raw4"], st["orth4"]
            r4s = f"raw4_t={r4['phase_t_median']:+.2f}" if r4 else "raw4=None"
            o4s = f"orth4_t={o4['phase_t_median']:+.2f}" if o4 else "orth4=None"
            print(
                f"  {fkey}: n={r3['n_dates']} raw3={r3['mean_ic']:+.4f}"
                f"(t={r3['phase_t_median']:+.2f},nw={r3['nw_t']:+.1f}) "
                f"orth3={o3['mean_ic']:+.4f}(t={o3['phase_t_median']:+.2f})"
                f" flip3={st['sign_flip3']} | "
                f"raw3={r3['mean_ic']:+.4f} → "
                f"{r4s} {o4s} flip4={st['sign_flip4']} | "
                f"|效|/MDE={r3['abs_mde_ratio']:.2f} 年正={r3['yearly_pos_share']:.0%}"
                f"({r3['yearly_n']}年) rand={r3['rand_ratio']:.2f} K={r3['mean_K']:.0f}")
    outpath = outdir / "preflight_result.json"
    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(all_out, f, ensure_ascii=False, indent=1, default=str)
    print(f"\n结果 → {outpath}")


if __name__ == "__main__":
    main()
