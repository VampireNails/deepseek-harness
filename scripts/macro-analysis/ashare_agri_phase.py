#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A股农业池：【调仓网格相位敏感性】扫描 —— 判定 IR 是否依赖于"运气好的网格"。

## 为什么必须做这个

`ashare_agri_buffer.py` 跑出全样本「合成·低换手+反转 H=20 缓冲15%」：
净超额 +5.34%、IR 0.588、分年 11/13 为正 → **判定 PASS**。
但同一配置在 `--start 2018-01-01` 子窗口下：净超额 +1.93%、IR 0.191。

**IR 差 3 倍。** 两种解释：

- (a) 策略真的不稳 —— 2017 年后 A 股机构化，反转效应衰减（regime shift）。
- (b) **网格相位假象** —— 非重叠调仓的网格是 `range(t0, T-h, h)`，窗口起点变了，
      整个调仓日期序列整体平移，跑的是**另一条完全不同的路径**。

(b) 不排除就不能对 (a) 下结论。而 (b) 的检验很干净：
**H=20 一共只有 20 个不同的网格**（相位 0..19），完整枚举即可，无需抽样。
若 20 个网格的 IR 均值接近 0.588 且分布窄 → (b) 排除，IR 可信；
若均值远低、分布很宽（比如 min 0.1 / max 0.6）→ 0.588 是踩点运气，**不可交付**。

## 判定口径（比 buffer.py 更严）

四项全过才算 PASS（缺一不可）：
1. 净超额 > 0
2. IR ≥ 0.50
3. 分年度为正 ≥ 2/3
4. 相位稳健：≥ 80% 的相位满足上述 1~3（**本脚本新增**）

## 用法
    python ashare_agri_phase.py --min-cross 30
    python ashare_agri_phase.py --min-cross 30 --holdings 20
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]
sys.path.insert(0, str(_HERE))

from ashare_agri_backtest import (  # noqa: E402
    DB, STRATEGIES, load_panel, compute_factors, build_signal, board_of, _roll_mean,
)
from ashare_agri_buffer import BUFFERS, run_buffer  # noqa: E402

OUT_DIR = ROOT / "outputs" / datetime.now().strftime("%Y-%m-%d")

# 子周期拆分：2019 年底是 A 股机构化定价的分水岭（外资+公募定价权上升）
SUBPERIODS = [
    ("2014~2019", lambda y: y <= 2019),
    ("2020~2026", lambda y: y >= 2020),
]


def _stats(vals):
    a = np.asarray(vals, dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return {}
    return {
        "mean": round(float(a.mean()), 4),
        "median": round(float(np.median(a)), 4),
        "min": round(float(a.min()), 4),
        "max": round(float(a.max()), 4),
        "std": round(float(a.std(ddof=1)) if a.size > 1 else 0.0, 4),
        "n": int(a.size),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--min-cross", type=int, default=30)
    ap.add_argument("--holdings", default="5,10,20",
                    help="要扫的持有期，逗号分隔；每个 H 会完整枚举 H 个相位")
    ap.add_argument("--cost", type=float, default=0.005,
                    help="往返费率（0.005 = 中成本主口径）")
    ap.add_argument("--tag", default="", help="输出文件名后缀")
    args = ap.parse_args()

    db = Path(args.db)
    conn = sqlite3.connect(db, timeout=30)
    try:
        qc_bad = {r[0] for r in conn.execute("SELECT code FROM hfq_qc WHERE bad = 1")}
    except sqlite3.OperationalError:
        qc_bad = set()
    dates, codes, open_, close, volume, amount, turn = load_panel(conn)
    conn.close()

    T, M = close.shape
    print(f"面板: {T} 日 × {M} 只   {dates[0]} ~ {dates[-1]}")

    # 可交易掩码：与 ashare_agri_buffer.py 完全一致的构造，不得手写
    lim_per_col = np.array([board_of(c)[1] for c in codes])
    tradable = (volume > 0) & np.isfinite(close)
    is_b = np.array([c.startswith(("200", "900")) for c in codes])
    tradable[:, is_b] = False
    a20 = _roll_mean(amount, 20)
    tradable &= np.isfinite(a20) & (a20 > 0)
    if qc_bad:
        tradable[:, np.array([c in qc_bad for c in codes])] = False
    # ★ 剔涨停：2026-09-02 修正。此前本脚本把 lim_per_col 传给 run_buffer，
    #   但那是个死参数（函数体内从未使用）→ 已生成的
    #   ashare_agri_phase.json / _ext.json 都是【未剔涨停】的口径。
    #   ashare_agri_final.py 实测剔涨停影响很小（IR 0.523 → 0.511，仅 −0.012），
    #   故那些文件的结论方向仍然成立，但绝对值应按此打一点折。
    chg = np.full_like(close, np.nan)
    chg[1:] = close[1:] / close[:-1] - 1.0
    tradable &= ~(np.isfinite(chg) & (chg >= lim_per_col * 0.95))
    print(f"可交易单元占比: {tradable.mean():.1%}（已剔涨停日）")

    factors = compute_factors(close, volume, amount, turn)
    holdings = [int(x) for x in args.holdings.split(",") if x.strip()]

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": f"{dates[0]} ~ {dates[-1]}（全样本）",
        "db": str(db),
        "cost_rate": args.cost,
        "method": {
            "phase_scan": "每个 H 完整枚举 H 个相位（t0 = 0..H-1），非抽样",
            "why": "非重叠调仓网格随窗口起点整体平移 → 不同窗口跑的是不同路径，"
                   "必须先排除相位假象才能谈策略稳定性",
            "pass_criteria": "净超额>0 且 IR>=0.50 且 分年为正>=2/3；"
                             "且 >=80% 的相位满足前三条（相位稳健）",
        },
        "strategies": {},
    }

    for skey, comps, slabel in STRATEGIES:
        signal = build_signal(factors, comps, tradable, args.min_cross)
        print(f"\n{'=' * 118}")
        print(f"【{slabel}】  相位总数 = sum(H) = {sum(holdings)}")
        report["strategies"][skey] = {"label": slabel, "by_holding": {}}

        for h in holdings:
            print(f"\n  --- H={h}（枚举 {h} 个相位）---")
            print(f"  {'缓冲':<12}{'IR均值':>8}{'IR中位':>8}{'IR最小':>8}{'IR最大':>8}"
                  f"{'IR标准差':>9}{'净超额均值':>11}{'达标相位':>10}  判定")
            blk = {}
            for buy_thr, sell_thr, blabel in BUFFERS:
                irs, nets, passes, yr_acc = [], [], [], {}
                for ph in range(h):
                    r = run_buffer(signal, close, dates, h, args.cost, tradable,
                                   args.min_cross, buy_thr, sell_thr, t0=ph)
                    if not r:
                        continue
                    irs.append(r["ir"])
                    nets.append(r["net_excess_ann"])
                    ok = (r["net_excess_ann"] > 0 and r["ir"] >= 0.50
                          and r["pos_years"] / max(r["n_years"], 1) >= 2 / 3)
                    passes.append(bool(ok))
                    for ye in r.get("yearly_excess", []):
                        yr_acc.setdefault(int(ye["year"]), []).append(ye["excess_ann"])

                if not irs:
                    continue
                frac = float(np.mean(passes))
                robust = frac >= 0.80
                st = _stats(irs)
                ns = _stats(nets)
                verdict = "★ 相位稳健" if robust else ("部分稳健" if frac >= 0.50 else "不稳健")

                # 子周期拆分（按相位平均后的分年度超额聚合）
                sub = {}
                for sname, pred in SUBPERIODS:
                    ys = [float(np.mean(v)) for k, v in yr_acc.items() if pred(k)]
                    if ys:
                        sub[sname] = {
                            "n_years": len(ys),
                            "mean_excess_ann": round(float(np.mean(ys)), 4),
                            "pos_years": int(sum(1 for x in ys if x > 0)),
                        }

                block = {
                    "ir": st, "net_excess_ann": ns,
                    "pass_fraction": round(frac, 3),
                    "phase_robust": robust, "verdict": verdict,
                    "subperiods": sub,
                    "yearly_excess_phase_avg": {
                        str(k): round(float(np.mean(v)), 4)
                        for k, v in sorted(yr_acc.items())},
                }
                blk[blabel] = block
                print(f"  {blabel:<12}{st['mean']:>8.3f}{st['median']:>8.3f}"
                      f"{st['min']:>8.3f}{st['max']:>8.3f}{st['std']:>9.3f}"
                      f"{ns['mean']:>10.2%}{frac:>9.0%}  {verdict}")
            report["strategies"][skey]["by_holding"][str(h)] = blk

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / (f"ashare_agri_phase{('_' + args.tag) if args.tag else ''}.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n报告已写入: {out}")


if __name__ == "__main__":
    main()
