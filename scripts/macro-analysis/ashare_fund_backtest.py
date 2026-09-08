# -*- coding: utf-8 -*-
"""A股基本面因子 · 第③关（经济可行性，扣成本回测）。

【为什么必须单独写，不能复用 run_backtest】
run_backtest 是「每隔 h 天非重叠再平衡」，而基本面策略的真实形态是
**只在财报生效日调仓**（一年 2 次：年报 4 月、半年报 8 月）。
用每 60 天再平衡会变成 51 期，同一份财报被交易两次，虚增换手与期数。
本脚本按【实际生效日】调仓，持有到下一个生效日。

口径与 ashare_agri_backtest.run_backtest 严格一致（保证可比）：
    换手   turn = 0.5 × Σ|w_new − w_old|        （单边）
    成本   net = r − turn × cost_rate           （cost_rate 为往返费率）
    基准   当期全部可交易股等权
    年化   ppy = 243 / 持有交易日
    IR     = 年化超额 / 年化波动

【第③关四项判定】
    ① 净超额 > 0   ② IR ≥ 0.50   ③ 分年度为正 ≥ 2/3   ④ 五分位单调

【★ 幸存者偏差的敏感性分析】
股票池是 2026-09-02 从东财板块拉的【当前】成分股 → 2014~2025 间退市或
被调出农业板块的股票不在池内。这对手头策略尤其致命：我们要买的是
**净利润同比最差**的那一组，而它们恰恰最可能退市 → 偏差与因子相关，会
系统性高估 Q1 的收益。本脚本用「补回 k 只退市股、每只 −90%」做敏感性，
给出结论翻转所需的退市数量阈值。
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
    load_panel, build_tradable, compute_factors, newey_west_t,
)
from ashare_hfq_access import qc_bad_codes, QcMissing  # noqa: E402
from ashare_fund_validate import (  # noqa: E402
    FUND_DB, OUT_DIR, FACTORS,
    load_fund, build_panel, cross_section_z, effective_date,
)
from ashare_fund_robust import STYLE_KEYS, ortho_residual, summarize  # noqa: E402

COSTS = [("低 0.30%", 0.003), ("主 0.50%", 0.005), ("高 0.80%", 0.008)]
N_Q = 5


def run_fund_bt(sig, close, dates, reb_idx, cost_rate, tradable, min_cross):
    """按【显式调仓日】的纯多头回测。sig 已是「越大越买」的信号。"""
    M = close.shape[1]
    w_prev = np.zeros(M)
    long_r, bench_r, turns, dts, q_ret = [], [], [], [], [[] for _ in range(N_Q)]

    for k, t in enumerate(reb_idx):
        t_end = reb_idx[k + 1] if k + 1 < len(reb_idx) else min(t + 60, close.shape[0] - 1)
        if t_end <= t:
            continue
        s_row = sig[t]
        m = tradable[t] & np.isfinite(s_row) & np.isfinite(close[t]) & np.isfinite(close[t_end])
        n = int(m.sum())
        if n < min_cross:
            continue
        # 持有期收益（等权组合在 t 买入、t_end 卖出）
        with np.errstate(divide="ignore", invalid="ignore"):
            r_all = close[t_end] / close[t] - 1.0
        idx = np.where(m)[0]
        sv, rv = s_row[idx], r_all[idx]

        order = np.argsort(sv)
        qsize = len(order) / N_Q
        cuts = [int(round(q * qsize)) for q in range(N_Q + 1)]
        for q in range(N_Q):
            sel = order[cuts[q]:cuts[q + 1]]
            if len(sel):
                q_ret[q].append(float(rv[sel].mean()))

        hi = idx[order[cuts[-2]:cuts[-1]]]        # Q5 = 信号最高 20%
        w = np.zeros(M)
        w[hi] = 1.0 / len(hi)
        turn = 0.5 * float(np.abs(w - w_prev).sum())
        w_prev = w

        long_r.append(float(rv[order[cuts[-2]:cuts[-1]]].mean()))
        bench_r.append(float(np.nanmean(rv)))
        turns.append(turn)
        dts.append(str(dates[t])[:10])

    if len(long_r) < 20:
        return None

    L = np.asarray(long_r)
    B = np.asarray(bench_r)
    tv = np.asarray(turns)
    spans = [reb_idx[k + 1] - reb_idx[k] if k + 1 < len(reb_idx) else 60
             for k in range(len(reb_idx))][:len(L)]
    ppy = 243.0 / float(np.median(spans))

    net_long = L - tv * cost_rate
    exc = net_long - B

    mu = float(exc.mean())
    vol = float(exc.std(ddof=1)) * math.sqrt(ppy)
    ann = mu * ppy
    ir = ann / vol if vol > 0 else float("nan")
    # ⚠️ newey_west_t 有两个同名版本，返回值不同：
    #    ashare_agri_backtest 引入的（本脚本用的）返回 (mu, se, t)；
    #    ashare_fund_validate 里自定义的返回 (t, n)。别混用。
    _mu, _se, t = newey_west_t(exc, lags=max(int(math.ceil(len(exc) ** 0.25)), 1))
    eq = np.cumprod(1 + exc)
    dd = float((eq / np.maximum.accumulate(eq) - 1).min())

    yearly = defaultdict(list)
    for v, d in zip(exc, dts):
        yearly[d[:4]].append(v)
    pos_y = sum(1 for v in yearly.values() if np.mean(v) > 0)

    qm = [float(np.mean(q)) if q else float("nan") for q in q_ret]
    # ⚠️ 单调性方向：传入的 sig 已经是「越大越买」（IC 为负的因子已取负号），
    #    所以正确判据是【收益随分位递增而递增】。第一版写成 qm[i] > qm[i+1]，
    #    方向反了，把实际单调的因子（Q1→Q5 = +0.034/+0.042/+0.055/+0.069/+0.083）
    #    误判成不单调 —— 这类符号错误会让一个本来能过的判据静默失败。
    mono = all(qm[i] < qm[i + 1] for i in range(N_Q - 1)) if all(np.isfinite(qm)) else False

    return {
        "n_periods": int(len(L)),
        "ppy": round(ppy, 3),
        "net_excess_ann": round(ann, 5),
        "ann_vol": round(vol, 5),
        "ir": round(float(ir), 3),
        "t_excess": round(float(t), 3),
        "max_dd_excess": round(dd, 4),
        "annual_turnover_x": round(float(tv.mean() * ppy), 2),
        "gross_excess_ann": round(float((L - B).mean() * ppy), 5),
        "cost_ann": round(float(tv.mean() * cost_rate * ppy), 5),
        "pos_years": int(pos_y),
        "n_years": int(len(yearly)),
        "quintile_returns": [round(x, 5) for x in qm],
        "monotonic": bool(mono),
        "yearly": {y: round(float(np.mean(v)) * ppy, 5) for y, v in sorted(yearly.items())},
    }


def survivorship_sensitivity(L, B, tv, n_stocks_q, ppy, cost_rate):
    """补回退市股的敏感性（按【全样本累计退市数】参数化）。

    ⚠️ 第一版把参数写成「每期退市 k 只」，那是错的 —— 26 期 × k=1 意味着
       全样本退市 26 只，远超现实，导致 k=1 就把 +6.24% 打成 −7.21%，
       敏感性被夸大了 26 倍。正确参数化：D = 全样本累计退市数，摊到各期。
    """
    L = np.asarray(L)
    B = np.asarray(B)
    tv = np.asarray(tv)
    n_per = len(L)
    out = []
    for D in range(0, 25):
        d = D / n_per                       # 每期平均补回的退市股数
        adj = (L * n_stocks_q + d * (-0.90)) / (n_stocks_q + d)
        net = adj - tv * cost_rate
        exc = net - B
        ann = float(exc.mean()) * ppy
        out.append({"D_total_delisted": D, "per_period": round(d, 3),
                    "net_excess_ann": round(ann, 5)})
    return out


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
    ap.add_argument("--no-qc", action="store_true",
                    help="关闭质检闸门（默认开启）。仅用于无 hfq_qc 的库；"
                         "报告会标注 qc_applied=false，不得与开闸结果混用")
    ap.add_argument("--start", default="2014-01-01")
    ap.add_argument("--top", type=float, default=0.20)
    args = ap.parse_args()

    print("=" * 78)
    print("A股基本面因子 · 第③关（经济可行性，扣成本回测）")
    print("=" * 78)

    fconn = sqlite3.connect(str(args.fund_db), timeout=30)
    fund_rows, med = load_fund(fconn)
    fconn.close()
    pconn = sqlite3.connect(str(args.price_db), timeout=30)
    # ★ 坑⑰（2026-09-03）：原 `except sqlite3.OperationalError: qc_bad = set()`
    #   会在 price-db 选错 / hfq_qc 缺失时静默放行全部数据，跑出的结论看不出
    #   有没有过质检。改为 fail-loud；确需放行须显式 --no-qc 并在报告标注。
    if args.no_qc:
        qc_bad = set()
    else:
        try:
            qc_bad = qc_bad_codes(Path(args.price_db), "hfq_qc", pconn)
        except QcMissing as e:
            raise SystemExit(f"{e}\n\n如确需在无质检的库上运行，请显式加 --no-qc，"
                             f"报告中会标注 qc_applied=false。")
    dates, codes, _o, close, volume, amount, turn = load_panel(pconn, PRICE_TABLE)
    pconn.close()

    tradable = build_tradable(close, volume, amount, codes, qc_bad,
                              exclude_b=True, exclude_limit=False,
                              min_history=args.min_history)
    t0 = int(np.where(dates >= args.start)[0][0])
    T, M = close.shape
    print(f"价格面板 {T} 天 × {M} 只   起始 {dates[t0]}   可交易单元 {tradable.mean():.1%}")

    F = build_panel(dates, codes, fund_rows, med)

    # 调仓日（财报生效日，非重叠）
    by_period = defaultdict(list)
    for r in fund_rows:
        by_period[r[1]].append(effective_date(r[1], r[2]))
    periods = []
    for rd in sorted(by_period):
        effs = sorted(e for e in by_period[rd] if e)
        if effs:
            periods.append((rd, effs[len(effs) // 2]))
    obs = []
    for rd, eff in periods:
        i = int(np.searchsorted(dates, eff, side="right"))
        if t0 <= i < T - args.holding:
            obs.append(i)
    obs = sorted(set(obs))
    keep = []
    for i in obs:
        if not keep or i - keep[-1] >= args.holding * 0.8:
            keep.append(i)
    reb_idx = keep
    print(f"调仓日 {len(reb_idx)} 个  [{dates[reb_idx[0]][:10]} ~ {dates[reb_idx[-1]][:10]}]\n")

    # 信号：IC 为负 → 取负号，买「最差」
    Z = {fk: cross_section_z(F[fk], tradable) for fk, _ in FACTORS}
    STY = compute_factors(close, volume, amount, turn)
    with np.errstate(divide="ignore", invalid="ignore"):
        ff = np.where((turn > 0) & np.isfinite(amount),
                      amount * 100.0 / np.where(turn > 0, turn, np.nan), np.nan)
    STY["log_size"] = np.log(np.where(ff > 0, ff, np.nan))
    SZ = {k: cross_section_z(STY[k], tradable) for k in STYLE_KEYS + ["log_size"]}

    signals = {}
    for fk in ("np_yoy_wins", "roe_rel", "comp_roe_rev"):
        signals[f"{fk}(买最差)"] = -Z[fk]
    # 全中性化版本：对 9 个价量风格 + 规模正交化
    basis = [SZ[k] for k in STYLE_KEYS + ["log_size"]]
    for fk in ("np_yoy_wins", "roe_rel"):
        Zr, _ = ortho_residual(Z[fk], basis, tradable)
        signals[f"{fk}·中性化(买最差)"] = -Zr

    report = {"generated_at": datetime.now().isoformat(timespec="seconds"),
              "holding": args.holding, "n_rebalance": len(reb_idx), "results": {}}

    for sname, sig in signals.items():
        print("=" * 78)
        print(f"信号: {sname}")
        print("=" * 78)
        print(f"  {'费率':<10}{'毛超额':>9}{'成本':>8}{'净超额':>9}{'年化波动':>9}"
              f"{'IR':>7}{'t':>7}{'换手':>7}{'正年':>7}{'单调':>6}")
        print("  " + "-" * 78)
        best = None
        rows = []
        for clabel, crate in COSTS:
            r = run_fund_bt(sig, close, dates, reb_idx, crate, tradable, args.min_cross)
            if r is None:
                print(f"  {clabel:<10}  期数不足")
                continue
            rows.append((clabel, crate, r))
            print(f"  {clabel:<10}{r['gross_excess_ann']:>+9.2%}{r['cost_ann']:>8.2%}"
                  f"{r['net_excess_ann']:>+9.2%}{r['ann_vol']:>9.2%}"
                  f"{r['ir']:>7.2f}{r['t_excess']:>7.2f}"
                  f"{r['annual_turnover_x']:>6.2f}x"
                  f"{r['pos_years']:>4}/{r['n_years']:<3}"
                  f"{'  是' if r['monotonic'] else '  否'}")
            if clabel.startswith("主"):
                best = (crate, r)
        if rows:
            q = rows[1][2]["quintile_returns"]
            print(f"  五分位收益(Q1→Q5, 每期): "
                  + "  ".join(f"{v:+.4f}" for v in q))
            print(f"  分年度净超额: " + "  ".join(
                f"{y[:4]}:{v:+.1%}" for y, v in list(rows[1][2]["yearly"].items())[:14]))
        report["results"][sname] = {cl: r for cl, cr, r in rows}

        # ---- 第③关四项判定（主费率 0.50%）----
        if best:
            crate, r = best
            c1 = r["net_excess_ann"] > 0
            c2 = r["ir"] >= 0.50
            c3 = r["pos_years"] >= max(1, math.ceil(r["n_years"] * 2 / 3))
            c4 = r["monotonic"]
            verdict = "PASS" if (c1 and c2 and c3 and c4) else "FAIL"
            print(f"\n  第③关判定（主费率 0.50%）: {verdict}")
            print(f"    ① 净超额 > 0        {r['net_excess_ann']:+.2%}   {'✓' if c1 else '✗'}")
            print(f"    ② IR ≥ 0.50         {r['ir']:.2f}        {'✓' if c2 else '✗'}")
            print(f"    ③ 分年度为正 ≥ 2/3   {r['pos_years']}/{r['n_years']}       "
                  f"{'✓' if c3 else '✗'}")
            print(f"    ④ 五分位单调         {'是' if r['monotonic'] else '否'}         "
                  f"{'✓' if c4 else '✗'}")
            report["results"][sname]["verdict"] = {
                "result": verdict,
                "c1_net_excess_pos": bool(c1), "c2_ir_ge_050": bool(c2),
                "c3_pos_years": bool(c3), "c4_monotonic": bool(c4),
            }
        print()

    # ---- 幸存者偏差敏感性（主信号）----
    print("=" * 78)
    print("幸存者偏差敏感性：补回 k 只退市股（每只 −90%）后的净超额")
    print("=" * 78)
    sig = signals["np_yoy_wins(买最差)"]
    n_q = max(int(np.nansum(tradable[reb_idx[0]]) * args.top), 3)
    # 取出毛收益序列
    Ls, Bs, Tv = [], [], []
    for k, t in enumerate(reb_idx):
        t_end = reb_idx[k + 1] if k + 1 < len(reb_idx) else min(t + 60, T - 1)
        if t_end <= t:
            continue
        s_row = sig[t]
        m = tradable[t] & np.isfinite(s_row) & np.isfinite(close[t]) & np.isfinite(close[t_end])
        if int(m.sum()) < args.min_cross:
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            r_all = close[t_end] / close[t] - 1.0
        idx = np.where(m)[0]
        sv, rv = s_row[idx], r_all[idx]
        order = np.argsort(sv)
        cuts = [int(round(q * len(order) / N_Q)) for q in range(N_Q + 1)]
        sel = order[cuts[-2]:cuts[-1]]
        Ls.append(float(rv[sel].mean()))
        Bs.append(float(np.nanmean(rv)))
        n_q = len(sel)
    spans = [reb_idx[k + 1] - reb_idx[k] if k + 1 < len(reb_idx) else 60
             for k in range(len(reb_idx))][:len(Ls)]
    ppy = 243.0 / float(np.median(spans))
    # 换手近似：每期约 35% 权重变化（由实测 turnover 均值反推）
    r_main = report["results"]["np_yoy_wins(买最差)"]["主 0.50%"]
    tv_mean = r_main["annual_turnover_x"] / ppy
    Tv = np.full(len(Ls), tv_mean)
    sens = survivorship_sensitivity(Ls, Bs, Tv, n_q, ppy, 0.005)
    print(f"  组合规模 {n_q} 只/期   年化因子 ppy={ppy:.2f}   实测单期换手 {tv_mean:.3f}")
    print("  假设：退市股属于『净利润同比最差』组（会被选入组合），持有期内收益 −90%，"
          "均匀摊在各期。")
    print(f"  {'全样本累计退市数 D':<20}{'每期摊到':>10}{'净超额/年':>12}")
    print("  " + "-" * 44)
    for s in sens:
        flag = "  ← 归零" if s["net_excess_ann"] <= 0 and s["D_total_delisted"] > 0 else ""
        if s["D_total_delisted"] % 2 == 0 or s["net_excess_ann"] <= 0:
            print(f"  {s['D_total_delisted']:<20}{s['per_period']:>10.3f}"
                  f"{s['net_excess_ann']:>+12.2%}{flag}")
    zero = next((s["D_total_delisted"] for s in sens if s["net_excess_ann"] <= 0), None)
    print(f"  → 结论翻转阈值：全样本累计退市 {zero} 只"
          f"（2014~2025 的 12 年里，农业板块退市股数量很可能就在这个量级）")
    report["survivorship_sensitivity"] = {"n_stocks_per_period": int(n_q),
                                          "ppy": round(ppy, 3), "rows": sens,
                                          "flip_threshold_D": zero}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # ★ 坑⑱（2026-09-03）：原文件名只带 holding，不带 price_db ⇒ 同一 holding 换池跑会互相覆盖。
    #   现把 price_db 的库名并入文件名；旧产物保留不删（新跑的写到新名）。
    out = OUT_DIR / f"ashare_fund_backtest_{Path(args.price_db).stem}_h{args.holding}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出: {out}")


if __name__ == "__main__":
    main()
