# -*- coding: utf-8 -*-
"""CSI800 估值 PB 因子第①②关验证（用原始未复权价）。

背景：后复权价会污染 PB 截面排序（复权因子与分红相关 → 高分红低PB股被系统性抬高，
实测 600519 后复权/原始 = 6.85x）。故 PB 必须用 raw_close / bps。
本脚本读 ashare_csi800_raw.sqlite（原始收盘价）+ ashare_csi800_fund.sqlite（bps），
前向收益仍用后复权价（收益口径必须复权，因子口径用原始价）。

口径与 ashare_csi800_multifactor.py 完全一致：月度、h=60、NW lags、MCC。
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
from ashare_csi800_multifactor import load_fund, winsor  # noqa: E402

FUND_DB = ROOT / "outputs" / "ashare_csi800_fund.sqlite"
PRICE_DB = ROOT / "outputs" / "ashare_csi800_hfq_xq.sqlite"
RAW_DB = ROOT / "outputs" / "ashare_csi800_raw.sqlite"
OUT_DIR = ROOT / "outputs" / "2026-09-04"


def load_raw_close(raw_db, dates):
    """加载原始收盘价矩阵 (T, M) 对齐 dates。"""
    c = sqlite3.connect(str(raw_db))
    codes = [r[0] for r in c.execute(
        "SELECT DISTINCT code FROM daily_quotes_raw ORDER BY code")]
    ci = {cd: j for j, cd in enumerate(codes)}
    di = {d: i for i, d in enumerate(dates)}
    T, M = len(dates), len(codes)
    raw = np.full((T, M), np.nan)
    for code, d, px in c.execute(
            "SELECT code, trade_date, close FROM daily_quotes_raw ORDER BY code, trade_date"):
        if px is None or not np.isfinite(px) or px <= 0:
            continue
        if code in ci and d in di:
            raw[di[d], ci[code]] = float(px)
    c.close()
    return codes, raw


def build_pb_panel(dates, hfq_codes, raw_codes, raw_close, fund_rows):
    """PB 面板：bps 按生效日前向填充（对齐 hfq_codes），raw_close 对齐日期。"""
    T = len(dates)
    ci = {c: j for j, c in enumerate(hfq_codes)}
    rci = {c: j for j, c in enumerate(raw_codes)}
    bps_panel = np.full((T, len(hfq_codes)), np.nan)
    for code, rd, nd, roe, ry, ny, bps, ocf, gm, rev, npr, eps in fund_rows:
        if bps is None or bps <= 0 or code not in ci:
            continue
        eff = effective_date(rd, nd)
        idx = int(np.searchsorted(dates, eff, side="right"))
        if idx < T:
            bps_panel[idx:, ci[code]] = float(bps)
    pb = np.full((T, len(hfq_codes)), np.nan)
    for j, code in enumerate(hfq_codes):
        if code not in rci:
            continue
        rj = rci[code]
        with np.errstate(invalid="ignore", divide="ignore"):
            pb[:, j] = raw_close[:, rj] / bps_panel[:, j]
    return pb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holding", type=int, default=60)
    ap.add_argument("--start", default="2014-01-01")
    ap.add_argument("--out", default="ashare_csi800_pb.json")
    ap.add_argument("--exclude-financial", action="store_true")
    ap.add_argument("--min-history", type=int, default=250)
    # 2026-09-05 新增：换源复测需要切换后复权库（腾讯→雪球），
    # 且产物目录必须可指定（原硬编码 2026-09-04，会覆盖历史结论产物）。
    ap.add_argument("--price-db", default=str(PRICE_DB),
                    help="后复权行情库（前向收益口径）")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    args = ap.parse_args()

    fconn = sqlite3.connect(str(FUND_DB))
    fund_rows = load_fund(fconn)
    fconn.close()

    pconn = sqlite3.connect(str(args.price_db))
    print(f"后复权库: {args.price_db}")
    qc_bad = {r[0] for r in pconn.execute("SELECT code FROM hfq_qc WHERE bad=1")}
    dates, codes, _o, close, volume, amount, turn = load_panel(pconn, "daily_quotes_hfq")
    pconn.close()

    raw_codes, raw_close = load_raw_close(RAW_DB, dates)
    print(f"后复权 {len(codes)} 只   原始价 {len(raw_codes)} 只")

    tradable = build_tradable(close, volume, amount, codes, qc_bad,
                              exclude_b=True, exclude_limit=False,
                              min_history=args.min_history)
    if args.exclude_financial:
        have_gm = {r[0] for r in fund_rows if r[8] is not None}
        fin = [j for j, c in enumerate(codes) if c not in have_gm]
        if fin:
            tradable[:, fin] = False
        print(f"剔除金融股 {len(fin)} 只")

    t0 = int(np.where(dates >= args.start)[0][0])
    pb = build_pb_panel(dates, codes, raw_codes, raw_close, fund_rows)
    ret_h = fwd(close, args.holding)

    by_month = {}
    for i in range(t0, len(dates) - args.holding):
        by_month[str(dates[i])[:7]] = i
    obs_idx = sorted(by_month.values())
    nw_lags = max(int(math.ceil(len(obs_idx) ** 0.25)),
                  int(math.ceil(args.holding / 21.0)) + 1)
    print(f"月度口径 N={len(obs_idx)}  NW lags={nw_lags}")

    X = cross_section_z(pb, tradable)
    ics, ns = spearman_ic(X[obs_idx], ret_h[obs_idx], tradable[obs_idx])
    n_typ = int(np.median(ns))
    var_noise = 1.0 / max(n_typ - 1, 1)
    var_obs = float(ics.var(ddof=1))
    sigma_true = math.sqrt(max(var_obs - var_noise, 0.0))
    mde = 2.8006 * math.sqrt(var_obs) / math.sqrt(len(ics))
    ic_mean = float(ics.mean())
    t_stat, _ = newey_west_t(ics, lags=nw_lags)
    t_thr = float(norm.ppf(1 - 0.05 / 2))
    ratio = abs(ic_mean) / mde
    sig = abs(t_stat) >= t_thr

    print("=" * 70)
    print(f"估值 PB 因子  IC={ic_mean:+.4f}  σ_true={sigma_true:.4f}  "
          f"MDE={mde:.4f}  |IC|/MDE={ratio:.2f}  t(HAC)={t_stat:+.2f}  "
          f"{'显著✓' if sig else '不显著'}")
    print(f"n_cross_typical={n_typ}  n_periods={len(ics)}")
    print("=" * 70)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "factor": "pb", "holding": args.holding, "start": dates[t0],
        "n_observations": len(obs_idx), "n_cross_typical": n_typ,
        "ic_mean": round(ic_mean, 5), "sigma_true": round(sigma_true, 5),
        "mde": round(mde, 5), "ratio_ic_mde": round(ratio, 3),
        "t_hac": round(t_stat, 3), "significant": sig,
        "ic_series": [round(float(v), 5) for v in ics],
    }
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    op = out_dir / args.out
    op.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果 → {op}")


if __name__ == "__main__":
    main()
