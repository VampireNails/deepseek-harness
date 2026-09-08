#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A股农业池：第三关 IR 不达标的【根因归因】诊断。

背景：第三关 0/28 通过，前 5 名的「超额>0 / 分年度为正 / 五分位单调」三项全满足，
      唯一卡点是 IR（最高 0.39 < 门槛 0.50）。在决定「换因子类型」之前必须先回答：

      **这 +3.89% 的超额究竟是选股 alpha，还是伪装成 alpha 的风格押注？**

## 为什么不用「回归截距 α」做归因

第一版我跑 `excess_t = α + Σ β_k · Δexposure_k,t`，结果 α 年化出现 −257%、+1161%
这类荒谬值。根因是**外推谬误**：α 的定义是「风格暴露全部为零时的预测超额」，
但 Q5 组合按构造就带着 −1.03σ 的换手倾斜、−0.94σ 的动量倾斜，
样本里根本不存在零暴露的观测点 → α 是对样本外的 extrapolation，毫无意义。

## 改用【正交化检验】（不外推，完全在样本内）

逐期把信号对其它风格特征做**截面回归取残差**，得到一个与这些风格正交的信号，
再用残差信号重跑回测，比较超额与 IR 的衰减幅度：

  ① `orth_resid`：只对【该策略未交易的特征】正交化 → 剔除"搭便车"的风格共线性。
     若衰减到 ≈ 0，说明收益完全来自与其它已知风格的共线性 → 换个价量因子也一样。
  ② `orth_all`：对【全部 9 个风格】正交化 → 极端破坏性测试（会把信号几乎清零），
     用于确认上限，不作为主判据。

判据：
  - 正交化后超额保留 > 50%  → 存在独立选股成分，值得继续深挖
  - 保留 20%~50%            → 部分独立，但主体是风格暴露
  - 保留 < 20%              → 基本是伪装的风格押注，换价量因子无意义

## 同时输出（描述性，不外推）
  - 风格暴露差异 Δ(Q5 − 池内等权基准)：组合到底押注了什么
  - 分年度净超额与 IR：定位拖累年份
  - 最差年度诊断

用法：
  python ashare_agri_attrib.py --min-cross 30
  python ashare_agri_attrib.py --min-cross 30 --holding 20
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.stats import rankdata

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]
sys.path.insert(0, str(_HERE))

from ashare_agri_backtest import (  # noqa: E402
    DB, STRATEGIES,
    load_panel, compute_factors, build_signal, board_of, run_backtest,
    _roll_mean,
)

OUT_DIR = ROOT / "outputs" / datetime.now().strftime("%Y-%m-%d")
COST_MID = 0.005  # 中成本档，与第三关主口径一致

STYLE_FACTORS = [
    "momentum_20d", "momentum_60d", "reversal_5d", "volatility_20d",
    "price_to_ma20", "volume_ratio_20d", "turnover_level_20d",
    "turnover_ratio_20d", "amihud_20d",
]
LABEL = {
    "momentum_20d": "20日动量", "momentum_60d": "60日动量", "reversal_5d": "5日反转",
    "volatility_20d": "20日波动", "price_to_ma20": "价格/MA20",
    "volume_ratio_20d": "20日量比", "turnover_level_20d": "20日换手水平",
    "turnover_ratio_20d": "20日换手比", "amihud_20d": "Amihud非流动性",
}


# ---------------------------------------------------------------- 工具
def cross_z(fm_row, mask_row, min_cross):
    """单行截面 z-score（按秩标准化，与 build_signal 口径一致）。只作用于 mask 内。"""
    m = np.isfinite(fm_row) & mask_row
    if int(m.sum()) < min_cross:
        return None
    idx = np.where(m)[0]
    r = rankdata(fm_row[idx]).astype(np.float64)
    s = r.std()
    z = np.full(len(fm_row), np.nan)
    z[idx] = (r - r.mean()) / (s if s > 0 else 1.0)
    return z


def precompute_z(factors, tradable, min_cross):
    """预计算全部风格特征的逐期截面 z。"""
    T = next(iter(factors.values())).shape[0]
    Z = {fk: np.full_like(factors[fk], np.nan) for fk in STYLE_FACTORS}
    for t in range(T):
        for fk in STYLE_FACTORS:
            z = cross_z(factors[fk][t], tradable[t], min_cross)
            if z is not None:
                Z[fk][t] = z
    return Z


def orthogonalize(signal, Z, tradable, min_cross, ortho_keys):
    """逐期把 signal 对 ortho_keys 的截面 z 做回归，返回 (残差信号, 平均截面规模, 平均R²)。

    R² = 信号被【风格空间】解释的比例。这是最直观的指标：
        R² ≈ 0.8 意味着信号的八成只是风格的线性组合，残差那两成才是"新东西"。
        配合正交化后的超额是否归零，就能判定 alpha 到底住在哪一半。

    ⚠️ 只在「signal 与全部 ortho 特征都有限」的股票上回归，避免 NaN 污染。
       这是正确的取舍（样本略减），不能用 nan→0 填充（会引入系统性偏差）。
    """
    T, M = signal.shape
    out = np.full_like(signal, np.nan)
    n_used, r2s = [], []
    for t in range(T):
        m = np.isfinite(signal[t]) & tradable[t]
        for fk in ortho_keys:
            m &= np.isfinite(Z[fk][t])
        idx = np.where(m)[0]
        if len(idx) < min_cross:
            continue
        cols = [Z[fk][t][idx] for fk in ortho_keys]
        Xd = np.column_stack([np.ones(len(idx))] + cols)
        y = signal[t][idx]
        beta, *_ = np.linalg.lstsq(Xd, y, rcond=None)
        resid = y - Xd @ beta
        out[t, idx] = resid
        n_used.append(len(idx))
        ss_tot = float(((y - y.mean()) ** 2).sum())
        if ss_tot > 0:
            r2s.append(1.0 - float((resid ** 2).sum()) / ss_tot)
    return out, (int(np.mean(n_used)) if n_used else 0), (float(np.mean(r2s)) if r2s else np.nan)


def exposure_delta(signal, Z, ret_h, tradable, min_cross, h, t0=0):
    """逐再平衡期：Q5 组合与池内等权基准的【风格暴露差异】+ 毛超额。

    ⚠️ 对 NaN 用「有限值子集 + 权重重归一」处理，不能用 nan→0
       （第一版用了 `isfinite(...).all()` 直接丢弃整期，154 期只剩 35 期）。
    """
    T, M = signal.shape
    reb = list(range(t0, T - h, h))
    from ashare_agri_backtest import N_QUANTILES
    recs = []
    for t in reb:
        s_row, r_row = signal[t], ret_h[t]
        m0 = tradable[t]
        m = np.isfinite(s_row) & np.isfinite(r_row) & m0
        if int(m.sum()) < min_cross:
            continue
        idx = np.where(m)[0]
        sv, rv = s_row[idx], r_row[idx]
        order = np.argsort(sv)
        qsize = len(order) / N_QUANTILES
        cuts = [int(round(k * qsize)) for k in range(N_QUANTILES + 1)]
        hi = idx[order[cuts[-2]:cuts[-1]]]
        if len(hi) < 3:
            continue

        excess = float(rv[order[cuts[-2]:cuts[-1]]].mean() - rv.mean())
        delta = {}
        for fk in STYLE_FACTORS:
            zt = Z[fk][t]
            sub = hi[np.isfinite(zt[hi])]
            allsub = idx[np.isfinite(zt[idx])]
            if len(sub) < 3 or len(allsub) < 3:
                delta[fk] = np.nan
            else:
                delta[fk] = float(zt[sub].mean() - zt[allsub].mean())
        recs.append({"t": t, "excess": excess, "delta": delta, "n_q5": len(hi)})
    return recs


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--min-cross", type=int, default=30)
    ap.add_argument("--holding", type=int, default=0, help="0=全部持有期")
    ap.add_argument("--start", default=None)
    ap.add_argument("--drop1", action="store_true",
                    help="逐个风格剔除检验：确认衰减不是「一正交就归零」的方法artifact")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"库不存在：{db}")
    conn = sqlite3.connect(db, timeout=30)
    try:
        qc_bad = {r[0] for r in conn.execute("SELECT code FROM hfq_qc WHERE bad = 1")}
    except sqlite3.OperationalError:
        qc_bad = set()
    dates, codes, open_, close, volume, amount, turn = load_panel(conn)
    conn.close()

    T, M = close.shape
    print(f"面板: {T} 日 × {M} 只   {dates[0]} ~ {dates[-1]}")
    if qc_bad:
        print(f"QC 黑名单: {sorted(qc_bad)}")

    lim_per_col = np.array([board_of(c)[1] for c in codes])
    tradable = (volume > 0) & np.isfinite(close)
    is_b = np.array([c.startswith(("200", "900")) for c in codes])
    tradable[:, is_b] = False
    a20 = _roll_mean(amount, 20)
    tradable &= np.isfinite(a20) & (a20 > 0)
    if qc_bad:
        tradable[:, np.array([c in qc_bad for c in codes])] = False
    print(f"可交易单元占比: {tradable.mean():.1%}")

    factors = compute_factors(close, volume, amount, turn)
    from ashare_agri_backtest import fwd

    t0 = 0
    if args.start:
        hit = np.where(dates >= args.start)[0]
        if len(hit) == 0:
            raise SystemExit("--start 超出数据范围")
        t0 = int(hit[0])
        print(f"起始日 {args.start} → {dates[t0]}")

    print("预计算风格截面 z ...")
    Z = precompute_z(factors, tradable, args.min_cross)

    holdings = [args.holding] if args.holding else [1, 5, 10, 20]
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "panel": {"days": int(T), "tickers": int(M), "start": str(dates[0]), "end": str(dates[-1])},
        "window": f"{dates[t0]} ~ {dates[-1]}" + (f"（--start {args.start}）" if args.start else "（全样本）"),
        "cost_rate": COST_MID,
        "method": {
            "test": "正交化检验：signal 对风格 z 逐期截面回归取残差，用残差信号重跑回测",
            "why_not_alpha": "回归截距 α 是「零暴露时的预测超额」，而 Q5 构造上带大倾斜，"
                             "样本内无零暴露观测点 → α 是外推，实测给出 −257%~+1161% 荒谬值，故弃用",
            "orth_resid": "只对【未交易特征】正交化 → 剔除风格共线性（主判据）",
            "orth_all": "对【全部 9 个风格】正交化 → 极端破坏性测试（上限，非主判据）",
            "verdict_rule": "超额保留率 >50% 独立 / 20~50% 部分独立 / <20% 纯风格押注",
        },
        "strategies": {},
    }

    for skey, comps, slabel in STRATEGIES:
        traded = {fk for fk, _ in comps}
        resid_f = [f for f in STYLE_FACTORS if f not in traded]
        print(f"\n{'=' * 104}")
        print(f"【{slabel}】  交易特征: {[LABEL[f] for f in traded]}")
        signal = build_signal(factors, comps, tradable, args.min_cross)

        sig_resid, n_r, r2_r = orthogonalize(signal, Z, tradable, args.min_cross, resid_f)
        sig_all, n_a, r2_a = orthogonalize(signal, Z, tradable, args.min_cross, STYLE_FACTORS)
        print(f"  正交化截面规模: orth_resid={n_r}  orth_all={n_a}")
        print(f"  ★ 信号被风格空间解释的比例 R²:  未交易7风格={r2_r:.3f}   全部9风格={r2_a:.3f}")

        report["strategies"][skey] = {
            "label": slabel, "traded_factors": sorted(traded),
            "residual_factors": resid_f,
            "orth_cross_section": {"resid": n_r, "all": n_a},
            "r2_in_style_space": {"resid7": round(r2_r, 4), "all9": round(r2_a, 4)},
            "by_holding": {},
        }

        for h in holdings:
            base = run_backtest(signal, close, dates, h, COST_MID, tradable,
                                args.min_cross, t0=t0, lim_per_col=lim_per_col)
            o_r = run_backtest(sig_resid, close, dates, h, COST_MID, tradable,
                               args.min_cross, t0=t0, lim_per_col=lim_per_col)
            o_a = run_backtest(sig_all, close, dates, h, COST_MID, tradable,
                               args.min_cross, t0=t0, lim_per_col=lim_per_col)
            if not base:
                print(f"  H={h}: 样本不足")
                continue

            e0 = base["excess_net"]["ann_return"]
            ir0 = base["excess_net"]["information_ratio"]
            blk = {"baseline": {"excess_ann": e0, "ir": ir0,
                                "turnover_x": base["annual_turnover_x"],
                                "n_periods": base["excess_net"]["n_periods"]}}

            def _cmp(tag, r):
                if not r:
                    return None
                e = r["excess_net"]["ann_return"]
                ir = r["excess_net"]["information_ratio"]
                keep = (e / e0) if e0 not in (0, None) and e0 != 0 else np.nan
                return {"excess_ann": e, "ir": ir, "keep_ratio": round(float(keep), 3),
                        "turnover_x": r["annual_turnover_x"],
                        "monotone": all(
                            r["quantile_ann_return"][i] < r["quantile_ann_return"][i + 1]
                            for i in range(4)),
                        "pos_years": sum(1 for y in r["yearly_excess"] if y["excess_ann"] > 0),
                        "n_years": len(r["yearly_excess"]),
                        "yearly": r["yearly_excess"]}

            blk["orth_resid"] = _cmp("resid", o_r)
            blk["orth_all"] = _cmp("all", o_a)

            print(f"\n  --- H={h}  期数={base['excess_net']['n_periods']}  年换手={base['annual_turnover_x']}x ---")
            print(f"    基准        超额={e0:+.2%}  IR={ir0:+.2f}")
            for tag in ("orth_resid", "orth_all"):
                d = blk[tag]
                if not d:
                    print(f"    {tag:<12} 样本不足")
                    continue
                print(f"    {tag:<12} 超额={d['excess_ann']:+.2%}  IR={d['ir']:+.2f}  "
                      f"保留率={d['keep_ratio']:.0%}  单调={'✓' if d['monotone'] else '✗'}")

            # ---- 稳健性：逐个风格剔除（drop-1），确认不是"一正交就归零"的方法artifact
            if args.drop1:
                d1 = {}
                for fk in resid_f:
                    s1, _, _ = orthogonalize(signal, Z, tradable, args.min_cross, [fk])
                    r1 = run_backtest(s1, close, dates, h, COST_MID, tradable,
                                      args.min_cross, t0=t0, lim_per_col=lim_per_col)
                    if r1:
                        d1[LABEL[fk]] = {"excess_ann": r1["excess_net"]["ann_return"],
                                         "ir": r1["excess_net"]["information_ratio"],
                                         "keep_ratio": round(
                                             float(r1["excess_net"]["ann_return"] / e0)
                                             if e0 else np.nan, 3)}
                blk["drop1"] = d1
                print("    drop-1（只剔除单个风格）保留率: " + "  ".join(
                    f"{k}{v['keep_ratio']:.0%}" for k, v in d1.items()))

            # 分年度
            ys = base["yearly_excess"]
            print("    分年度净超额: " + "  ".join(
                f"{y['year']}:{y['excess_ann']:+.1%}" for y in ys))
            worst = min(ys, key=lambda d: d["excess_ann"]) if ys else None
            if worst:
                print(f"    最差年: {worst['year']}  {worst['excess_ann']:+.1%}")

            # 风格暴露差异（描述性）
            recs = exposure_delta(signal, Z, fwd(close, h), tradable,
                                  args.min_cross, h, t0)
            if recs:
                with np.errstate(invalid="ignore"):
                    expo = {f: float(np.nanmean([r["delta"][f] for r in recs]))
                            for f in STYLE_FACTORS}
                blk["mean_exposure_delta"] = {LABEL[f]: round(expo[f], 3)
                                              for f in STYLE_FACTORS}
                blk["n_exposure_periods"] = len(recs)
                print("    风格暴露差 Δ(Q5−基准): " + "  ".join(
                    f"{LABEL[f]}{expo[f]:+.2f}" for f in STYLE_FACTORS
                    if abs(expo[f]) > 0.05))

            report["strategies"][skey]["by_holding"][str(h)] = blk

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "ashare_agri_attrib.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n报告已写入: {out}")


if __name__ == "__main__":
    main()
