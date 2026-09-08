# -*- coding: utf-8 -*-
"""
ashare_qc_impact.py — 数据质量对回测结论的【实际影响】定量评估

背景（2026-09-03 用户质询）：
    「数据质量是量化 agent 的基石，如果数据都无法保证，那所有策略和回测结论也无效。」

这句话的成立与否取决于一个可测的事实：
    坏数据在池中的占比有多高？剔除它们后，结论会不会变？

本脚本用中证800 池（780 只，QC 判 22 只 bad）做对照实验：
    同一段数据、同一个因子（20 日动量 / 20 日反转），
    分别算「含坏数据」与「剔除坏数据」的截面 rank IC 与分层多空收益，
    直接量化 QC 对结论的影响幅度。

判据
----
- 若剔除前后 mean IC / t 值 / 多空收益差异 < 效应量本身的量级 → 坏数据污染可忽略，
  「结论无效」是过度声明；正确表述是「结论对 2.8% 的样本污染稳健」。
- 若差异与效应量同量级甚至反号 → 结论确实被数据质量绑架，必须先修数据。

用法
----
  python ashare_qc_impact.py --db outputs/ashare_csi800_hfq_xq.sqlite --since 2021-01-01
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
while not (ROOT / "outputs").is_dir() and ROOT != ROOT.parent:
    ROOT = ROOT.parent


def load_panel(db: Path, since: str):
    """返回 (dates, codes, close 矩阵 MxT)"""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT code, trade_date, close FROM daily_quotes_hfq "
        "WHERE trade_date >= ? AND close IS NOT NULL AND close > 0 "
        "ORDER BY trade_date, code", (since,)).fetchall()
    dates = sorted({r["trade_date"] for r in rows})
    codes = sorted({r["code"] for r in rows})
    di = {d: i for i, d in enumerate(dates)}
    ci = {c: i for i, c in enumerate(codes)}
    M = np.full((len(codes), len(dates)), np.nan)
    for r in rows:
        M[ci[r["code"]], di[r["trade_date"]]] = r["close"]
    bad = {r[0] for r in conn.execute("SELECT code FROM hfq_qc WHERE bad = 1")}
    conn.close()
    return dates, codes, M, bad


def rank_ic(f: np.ndarray, r: np.ndarray) -> float:
    """截面 Spearman rank IC（忽略 NaN）"""
    ok = np.isfinite(f) & np.isfinite(r)
    if ok.sum() < 20:
        return float("nan")
    a = _rank(f[ok]); b = _rank(r[ok])
    a = a - a.mean(); b = b - b.mean()
    if a.std() == 0 or b.std() == 0:
        return float("nan")
    return float((a * b).sum() / np.sqrt((a * a).sum() * (b * b).sum()))


def _rank(x: np.ndarray) -> np.ndarray:
    order = np.argsort(np.argsort(x))
    return order.astype(float)


def ic_series(M: np.ndarray, keep: np.ndarray, h: int = 20, step: int = 5,
              sign: int = 1) -> dict:
    """非重叠（step=h）20 日动量/反转的 rank IC 序列

    sign=+1 动量（过去 h 日收益 → 未来 h 日收益）
    sign=-1 反转
    """
    X = M[keep]
    T = X.shape[1]
    ics, ls = [], []
    for t in range(h, T - h, h):
        past = X[:, t] / X[:, t - h] - 1.0
        fwd = X[:, t + h] / X[:, t] - 1.0
        ic = rank_ic(sign * past, fwd)
        if np.isfinite(ic):
            ics.append(ic)
            # 分层多空：Q5 - Q1（按因子排序，5 分位等权）
            ok = np.isfinite(past) & np.isfinite(fwd)
            if ok.sum() >= 50:
                p, fw = past[ok], fwd[ok]
                q = np.quantile(p, [0.2, 0.4, 0.6, 0.8])
                g = np.digitize(p, q)
                ls.append(float(fw[g == 4].mean() - fw[g == 0].mean()))
    ics = np.array(ics, float)
    ls = np.array(ls, float)
    n = len(ics)
    if n < 5:
        return {"n": n}
    m, sd = float(ics.mean()), float(ics.std(ddof=1))
    # Newey-West（滞后 h/5 阶）对 mean IC 的 se
    L = max(1, h // 5)
    x = ics - m
    g0 = float((x * x).sum() / n)
    gam = sum(float((x[L + i:] * x[:n - L - i]).sum() / n) for i in range(1, L + 1))
    var = (g0 + 2 * gam) / n
    t = m / np.sqrt(var) if var > 0 else float("nan")
    lm, lsd = float(ls.mean()), float(ls.std(ddof=1))
    return {"n": n, "ic_mean": m, "ic_sd": sd, "icir": m / sd if sd else float("nan"),
            "t_nw": float(t),
            "ls_mean": lm, "ls_sd": lsd,
            "ls_t": float(lm / (lsd / np.sqrt(len(ls)))) if lsd > 0 else float("nan"),
            "pos_rate": float((ics > 0).mean())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "outputs" / "ashare_csi800_hfq_xq.sqlite"))
    ap.add_argument("--since", default="2021-01-01")
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    db = Path(args.db)
    print(f"[1/3] 载入面板 {db.name} since={args.since}")
    dates, codes, M, bad = load_panel(db, args.since)
    print(f"      日期 {dates[0]} ~ {dates[-1]}  ({len(dates)} 交易日)")
    print(f"      股票 {len(codes)} 只，其中 QC 判坏 {len(bad)} 只 "
          f"({len(bad)/len(codes)*100:.2f}%)")

    bad_idx = np.array([c in bad for c in codes])
    all_idx = np.ones(len(codes), bool)

    print(f"\n[2/3] 对照实验：horizon={args.horizon} 日，非重叠分期")
    out = {"db": db.name, "since": args.since, "n_codes": len(codes),
           "n_bad": int(bad_idx.sum()), "bad_codes": sorted(bad),
           "dates": [dates[0], dates[-1]], "horizon": args.horizon,
           "results": {}}
    for name, sign in (("momentum_20d", 1), ("reversal_20d", -1)):
        a = ic_series(M, all_idx, h=args.horizon, sign=sign)
        b = ic_series(M, ~bad_idx, h=args.horizon, sign=sign)
        out["results"][name] = {"with_bad": a, "clean_only": b}
        print(f"\n  ── {name} ──")
        print(f"    {'':12} {'n':>5} {'IC均值':>10} {'ICIR':>8} {'t(NW)':>9} "
              f"{'多空年化%':>10} {'多空t':>8}")
        for tag, d in (("含坏数据", a), ("剔除坏数据", b)):
            print(f"    {tag:12} {d['n']:>5} {d['ic_mean']:>10.4f} {d['icir']:>8.3f} "
                  f"{d['t_nw']:>9.2f} {d['ls_mean']*244/args.horizon*100:>10.2f} "
                  f"{d['ls_t']:>8.2f}")
        dic = abs(b["ic_mean"] - a["ic_mean"])
        dls = abs(b["ls_mean"] - a["ls_mean"])
        print(f"    ΔIC = {dic:.5f}   Δ多空(每期) = {dls*100:.4f} 个百分点")
        out["results"][name]["delta_ic"] = dic
        out["results"][name]["delta_ls"] = float(dls)

    print(f"\n[3/3] 坏数据个股的极端程度（为什么不剔除就很危险）")
    X = M[bad_idx]
    r = X[:, 1:] / X[:, :-1] - 1.0
    ext = np.nanmax(np.abs(r))
    print(f"      坏数据股最大单日收益绝对值：{ext*100:.1f}%  "
          f"（正常 A 股上限 10%/20%）")
    out["bad_max_abs_daily_ret"] = float(ext)
    Xc = M[~bad_idx]
    rc = Xc[:, 1:] / Xc[:, :-1] - 1.0
    print(f"      干净股最大单日收益绝对值：{np.nanmax(np.abs(rc))*100:.1f}%")
    out["clean_max_abs_daily_ret"] = float(np.nanmax(np.abs(rc)))

    if args.out:
        p = Path(args.out); p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  结果 -> {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
