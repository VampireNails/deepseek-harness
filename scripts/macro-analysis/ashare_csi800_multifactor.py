# -*- coding: utf-8 -*-
"""CSI800 基本面多因子（未检验因子）第①②关验证。

背景（2026-09-04 对齐后补测）：
    此前 ashare_fund_validate.py 在 CSI800 已测 6 个单因子（ROE 相对/绝对、营收同比、
    毛利率绝对、净利同比缩尾、ROE+营收合成、规模 log营收），月度重叠口径 N=150、
    MDE≈0.03，全部 UNDERPOWERED（最优营收同比 |IC|/MDE=0.61，无一显著）。
    本脚本补测 3 个【从未检验】的因子，用同口径（月度、h=60、NW lags、MCC 校正）：

      1. quality_ocf_np  —— 盈利质量 = 每股经营现金流 / 每股收益（ocf_ps/eps，eps>0.05）
      2. growth_combo    —— 成长双增 = 截面 z(营收同比) 与 z(净利同比) 的均值
      3. gm_change       —— 毛利率变化（YoY）= 本期毛利率 − 4 期前（1 年）毛利率

    估值 PB 因后复权价会污染截面排序（后复权累计复权因子与分红高度相关 → 高分红低 PB 股
    被系统性抬高），本脚本【不测】，待补原始（未复权）收盘价后再测，见报告说明。

    vintage 纪律与 IC 口径与 ashare_fund_validate.py 完全一致（生效日=公告日 T+1，
    按报告期去重取中位数生效日，月度口径允许重叠 + Newey-West 调整）。
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.stats import norm

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]
sys.path.insert(0, str(_HERE))

from ashare_agri_backtest import load_panel, build_tradable, fwd  # noqa: E402
from ashare_fund_validate import (  # noqa: E402
    effective_date, cross_section_z, spearman_ic, newey_west_t,
)

FUND_DB = ROOT / "outputs" / "ashare_csi800_fund.sqlite"
PRICE_DB = ROOT / "outputs" / "ashare_csi800_hfq_xq.sqlite"
PRICE_TABLE = "daily_quotes_hfq"
OUT_DIR = ROOT / "outputs" / "2026-09-04"

FACTORS = [
    ("quality", "盈利质量·OCF/每股收益"),
    ("growth_combo", "成长双增·营收+净利同比"),
    ("gm_change", "毛利率变化·YoY"),
]


def load_fund(conn):
    rows = conn.execute(
        "SELECT code, report_date, notice_date, roe, rev_yoy, np_yoy, bps,"
        "       ocf_ps, gross_margin, revenue, net_profit, eps "
        "FROM fund_reports ORDER BY code, report_date").fetchall()
    return rows


def winsor(x, q=0.05):
    v = np.asarray([v for v in x if v is not None and np.isfinite(v)], float)
    if len(v) < 100:
        return None, None
    return np.percentile(v, [q * 100, (1 - q) * 100])


def build_panel(dates, codes, fund_rows, wins_q=0.05):
    T, M = len(dates), len(codes)
    ci = {c: j for j, c in enumerate(codes)}
    raw = {k: np.full((T, M), np.nan) for k in
           ["quality", "rev_yoy_w", "np_yoy_w", "gm_change"]}

    # 全样本缩尾界
    rl, rh = winsor([r[4] for r in fund_rows])       # rev_yoy
    nl, nh = winsor([r[5] for r in fund_rows])       # np_yoy
    ql, qh = winsor([r[7] / r[11] for r in fund_rows
                     if r[7] is not None and r[11] and r[11] > 0.05])  # ocf_ps/eps

    # 毛利率变化：按 (code, report_date) 排序后 diff 4 期（1 年）
    by_code = {}
    for c, rd, nd, roe, ry, ny, bps, ocf, gm, rev, npr, eps in fund_rows:
        by_code.setdefault(c, []).append((rd, nd, ry, ny, ocf, gm, eps))
    gm_change_map = {}
    for c, lst in by_code.items():
        lst.sort(key=lambda x: x[0])
        gms = [(i, x[5]) for i, x in enumerate(lst) if x[5] is not None]
        gm_series = {i: g for i, g in gms}
        for i in range(4, len(lst)):
            cur = gm_series.get(i)
            prev = gm_series.get(i - 4)
            if cur is not None and prev is not None:
                gm_change_map[(c, lst[i][0])] = cur - prev

    by_code2 = {}
    for c, rd, nd, roe, ry, ny, bps, ocf, gm, rev, npr, eps in fund_rows:
        by_code2.setdefault(c, []).append((rd, nd, ry, ny, ocf, gm, eps))

    for code, lst in by_code2.items():
        j = ci.get(code)
        if j is None:
            continue
        lst.sort(key=lambda x: x[0])  # 按报告期升序（nd 是公告日，仅用于排序稳定）
        for rd, nd, ry, ny, ocf, gm, eps in lst:
            eff = effective_date(rd, nd)
            idx = int(np.searchsorted(dates, eff, side="right"))
            if idx >= T:
                continue
            if ry is not None and rl is not None:
                raw["rev_yoy_w"][idx:, j] = float(np.clip(ry, rl, rh))
            if ny is not None and nl is not None:
                raw["np_yoy_w"][idx:, j] = float(np.clip(ny, nl, nh))
            if ocf is not None and eps and eps > 0.05 and ql is not None:
                raw["quality"][idx:, j] = float(np.clip(ocf / eps, ql, qh))
            gc = gm_change_map.get((code, rd))
            if gc is not None:
                raw["gm_change"][idx:, j] = float(gc)

    # 成长双增 = 截面 z(营收同比) 与 z(净利同比) 的均值
    za = cross_section_z(raw["rev_yoy_w"], np.isfinite(raw["rev_yoy_w"]))
    zb = cross_section_z(raw["np_yoy_w"], np.isfinite(raw["np_yoy_w"]))
    raw["growth_combo"] = np.nanmean(np.stack([za, zb]), axis=0)
    return raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holding", type=int, default=60)
    ap.add_argument("--min-cross", type=int, default=20)
    ap.add_argument("--fund-db", default=str(FUND_DB))
    ap.add_argument("--price-db", default=str(PRICE_DB))
    ap.add_argument("--start", default="2014-01-01")
    ap.add_argument("--min-periods", type=int, default=20)
    ap.add_argument("--exclude-financial", action="store_true")
    ap.add_argument("--min-history", type=int, default=250)
    ap.add_argument("--out", default="ashare_csi800_multifactor.json")
    # 2026-09-05 新增：换源复测时产物目录必须可指定（原硬编码 2026-09-04，
    # 直接覆盖会毁掉旧源结论，无法做前后对照）
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    args = ap.parse_args()

    print("=" * 78)
    print("CSI800 基本面多因子（未检验 3 因子）第①②关验证")
    print("=" * 78)

    fconn = sqlite3.connect(args.fund_db, timeout=30)
    fund_rows = load_fund(fconn)
    fconn.close()

    pconn = sqlite3.connect(args.price_db, timeout=30)
    try:
        qc_bad = {r[0] for r in pconn.execute("SELECT code FROM hfq_qc WHERE bad=1")}
    except sqlite3.OperationalError:
        qc_bad = set()
    dates, codes, _o, close, volume, amount, turn = load_panel(pconn, PRICE_TABLE)
    pconn.close()

    tradable = build_tradable(close, volume, amount, codes, qc_bad,
                              exclude_b=True, exclude_limit=False,
                              min_history=args.min_history)

    if args.exclude_financial:
        have_gm = {r[0] for r in fund_rows if r[8] is not None}
        fin = [j for j, c in enumerate(codes) if c not in have_gm]
        if fin:
            tradable[:, fin] = False
        print(f"已剔除金融/类金融股（毛利率全期缺失）: {len(fin)} 只")

    t0 = int(np.where(dates >= args.start)[0][0]) if args.start else 0
    T, M = close.shape
    print(f"价格面板 {T} 天 × {M} 只   起始 {dates[t0]}")
    print(f"财报记录 {len(fund_rows)} 条   可交易单元 {tradable.mean():.1%}")

    F = build_panel(dates, codes, fund_rows)
    ret_h = fwd(close, args.holding)

    # 月度口径（与单因子同口径）
    by_month = {}
    for i in range(t0, T - args.holding):
        by_month[str(dates[i])[:7]] = i
    obs_idx = sorted(by_month.values())
    nw_lags = max(int(math.ceil(len(obs_idx) ** 0.25)),
                  int(math.ceil(args.holding / 21.0)) + 1)
    print(f"月度口径有效观测 {len(obs_idx)} 期   Newey-West lags={nw_lags}")

    if len(obs_idx) < args.min_periods:
        raise SystemExit(f"有效观测期数 {len(obs_idx)} < {args.min_periods}")

    alpha = 0.05
    n_tests = len(FACTORS)
    t_thr = float(norm.ppf(1 - alpha / (2 * max(n_tests, 1))))
    print(f"MCC 阈值（n_tests={n_tests}, α=0.05）: |t| ≥ {t_thr:.3f}\n")

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "holding_days": args.holding,
        "start": dates[t0],
        "n_observations": len(obs_idx),
        "mcc_threshold": t_thr,
        "note": "PB 因子因后复权价污染截面排序，本次未测（待补原始收盘价）",
        "factors": {},
    }

    print(f"  {'因子':<24}{'N':>5}{'IC均值':>9}{'σ_true':>8}{'MDE':>8}"
          f"{'|IC|/MDE':>10}{'t(HAC)':>9}{'显著':>6}")
    print("  " + "-" * 88)

    n_pass = 0
    for fk, flabel in FACTORS:
        X = cross_section_z(F[fk], tradable)
        ics, ns = spearman_ic(X[obs_idx], ret_h[obs_idx], tradable[obs_idx])
        if len(ics) < 8:
            print(f"  {flabel:<24}  观测不足（{len(ics)}）")
            continue
        n_typ = int(np.median(ns))
        var_noise = 1.0 / max(n_typ - 1, 1)
        var_obs = float(ics.var(ddof=1))
        sigma_true = math.sqrt(max(var_obs - var_noise, 0.0))
        mde = 2.8006 * math.sqrt(var_obs) / math.sqrt(len(ics))
        ic_mean = float(ics.mean())
        t_stat, _ = newey_west_t(ics, lags=nw_lags)
        ratio = abs(ic_mean) / mde if mde > 0 else float("nan")
        sig = bool(abs(t_stat) >= t_thr)
        if sig:
            n_pass += 1
        print(f"  {flabel:<24}{n_typ:>5}{ic_mean:>+9.4f}"
              f"{sigma_true:>8.4f}{mde:>8.4f}{ratio:>10.2f}{t_stat:>+9.2f}"
              f"{'  ✓' if sig else '    '}")
        report["factors"][fk] = {
            "label": flabel, "n_periods": int(len(ics)),
            "n_cross_typical": n_typ, "ic_mean": round(ic_mean, 5),
            "sigma_true": round(sigma_true, 5), "mde": round(mde, 5),
            "ratio_ic_mde": round(ratio, 3), "t_hac": round(t_stat, 3),
            "significant_mcc": sig,
            "ic_series": [round(float(v), 5) for v in ics],
        }

    print(f"\n  通过 MCC: {n_pass}/{len(FACTORS)}")

    best = max(report["factors"].items(),
               key=lambda kv: kv[1]["ratio_ic_mde"], default=(None, None))
    if best[0]:
        b = best[1]
        if b["ratio_ic_mde"] >= 1 and b["significant_mcc"]:
            verdict = "PASS_STAGE12"
        elif b["significant_mcc"] and b["ratio_ic_mde"] < 1:
            verdict = "SIGNIFICANT_BUT_UNDERPOWERED"
        elif b["ratio_ic_mde"] >= 1:
            verdict = "EFFECT_BUT_NOT_SIGNIFICANT"
        else:
            verdict = "UNDERPOWERED"
        report["verdict"] = verdict
        print(f"\n判定: {verdict}   （最优 {b['label']}: |IC|/MDE={b['ratio_ic_mde']:.2f}）")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    op = out_dir / args.out
    op.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"后复权库: {args.price_db}")
    print(f"\n已写出: {op}")


if __name__ == "__main__":
    main()
