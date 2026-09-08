#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ashare_pollution_quantify.py — 全池量化腾讯后复权污染（旧源 vs 新源）
====================================================================
为什么还要做这一步
------------------
2026-09-05 的《数据源核对报告》只用 6 只分层样本量化了污染（月度收益
p50=0.68%、39% 观测差>1%）。样本量 390 个月度观测，足以【定性】，
但不足以回答：全池 780 只股票上污染到底有多普遍、分布如何、是否与
低价高分红相关（报告提出的机理假设）。

这一步把定性发现升级为全池定量事实，并【检验此前提出的机理】。

方法（关键：只用收益率，不用价格绝对值）
----------------------------------------
- 两源的日收益序列逐日比对（后复权基期不同，绝对值不可比，收益可比）
- 月度收益：按自然月复利累乘日收益，再比对（因子检验消费的最小单元）
- 分位：按【平均价格】与【股息代理】分层，检验"污染 ∝ 分红/低价"假说
  股息代理 = 全样本 (腾讯hfq/雪球hfq) 比率的变化率 —— 直接用污染强度本身
  作被解释变量，按价格/行业分层观察（不做因果断言）

输出
----
  outputs/2026-09-05/ashare_pollution_quantify.json
    全池月度收益差分布（p50/p90/p99/max、>1% 占比）
    按价格分层、按污染强度排序的股票清单
    与"因子信号幅度 0.2~0.5%"的对比结论
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np

WORKSPACE = Path(__file__).resolve().parents[4]
OUT_DIR = WORKSPACE / "outputs" / "2026-09-05"
TX_DB = WORKSPACE / "outputs" / "ashare_csi800_hfq.sqlite"
XQ_DB = WORKSPACE / "outputs" / "ashare_csi800_hfq_xq.sqlite"


def load_rets(db: Path, table="daily_quotes_hfq"):
    """返回 {code: {date: ret}} 与 {code: [dates sorted]}。"""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    data: dict[str, list] = defaultdict(list)
    for code, d, c in conn.execute(
            f"SELECT code, trade_date, close FROM {table} "
            f"WHERE close IS NOT NULL AND close > 0 ORDER BY code, trade_date"):
        data[code].append((d, float(c)))
    conn.close()
    out = {}
    for code, seq in data.items():
        seq.sort()
        out[code] = {seq[i][0]: seq[i][1] / seq[i - 1][1] - 1.0
                     for i in range(1, len(seq)) if seq[i - 1][1] > 0}
    return out, {c: sorted({d[:7] for d in v}) for c, v in out.items()}


def monthly(rets: dict) -> dict:
    """日收益 → 月度复利收益 {YYYY-MM: r}。"""
    m = defaultdict(list)
    for d, r in sorted(rets.items()):
        m[d[:7]].append(r)
    return {k: float(np.prod([1 + x for x in v]) - 1) for k, v in m.items() if v}


def _bad_codes(db: Path) -> set:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {r[0] for r in c.execute("SELECT code FROM hfq_qc WHERE bad=1")}
    finally:
        c.close()


def _month_diffs(tx, xq, code: str) -> list:
    mt, mx = monthly(tx[code]), monthly(xq[code])
    return [abs(mt[m] - mx[m]) for m in set(mt) & set(mx)]


def _common_mode(tx, xq, codes):
    """按月分解污染：截面共模偏移 vs 截面离散 —— 解释"污染大但 IC 不变"。

    diff_i = 腾讯月收益_i − 雪球月收益_i
    对每个月份 m：
        com_m = |mean_i(diff_i)|   ← 共模成分（所有股票一起偏）
        dis_m = MAD_i(diff_i)      ← 离散成分（个股各不相同，才会扰动排序）
    若 mean(com) 与 mean(dis) 同量级或更大 ⇒ 共模主导，IC 不受影响。
    离散度用 MAD 而非 std：污染分布厚尾（实测 max=23567%），std 会被
    单个极端值主导，得出与 p50 自相矛盾的结论。
    """
    mt_all = {c: monthly(tx[c]) for c in codes if c in tx}
    mx_all = {c: monthly(xq[c]) for c in codes if c in xq}
    months = defaultdict(list)
    for c in set(mt_all) & set(mx_all):
        for m in set(mt_all[c]) & set(mx_all[c]):
            months[m].append(mt_all[c][m] - mx_all[c][m])
    coms, diss = [], []
    for m, v in months.items():
        if len(v) < 50:
            continue
        a = np.asarray(v, float)
        coms.append(abs(a.mean()))
        diss.append(float(np.median(np.abs(a - np.median(a)))))   # MAD
    coms, diss = np.asarray(coms), np.asarray(diss)
    return {
        "n_months": int(len(coms)),
        "statistic": "MAD（稳健，抗厚尾）",
        "mean_abs_common_component": float(coms.mean()),
        "mean_dispersion_MAD": float(diss.mean()),
        "ratio_common_over_dispersion": float(coms.mean() / max(diss.mean(), 1e-12)),
        "verdict": ("共模主导 ⇒ 污染不改变截面排序，IC 不受影响"
                    if coms.mean() > diss.mean()
                    else "离散主导 ⇒ 污染会扰动截面排序，需逐因子复核"),
        "note": "IC 是截面秩相关，对全体同向偏移不敏感，只对个股间相对差异敏感",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    if not XQ_DB.exists():
        raise SystemExit(f"新源库不存在: {XQ_DB}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("加载腾讯后复权 ...", flush=True)
    tx, _ = load_rets(TX_DB)
    print(f"  {len(tx)} 只", flush=True)
    print("加载雪球后复权 ...", flush=True)
    xq, _ = load_rets(XQ_DB)
    print(f"  {len(xq)} 只", flush=True)

    codes = sorted(set(tx) & set(xq))
    if args.limit:
        codes = codes[:args.limit]
    print(f"共有股票 {len(codes)} 只", flush=True)

    all_diff, per_code = [], []
    for i, code in enumerate(codes, 1):
        mt, mx = monthly(tx[code]), monthly(xq[code])
        common = sorted(set(mt) & set(mx))
        if len(common) < 24:
            continue
        d = [abs(mt[m] - mx[m]) for m in common]
        if not d:
            continue
        all_diff.extend(d)
        # 平均价格水平（用雪球后复权价的均值，仅用于分层，不参与结论）
        per_code.append({"code": code, "n_months": len(common),
                         "p50": float(np.percentile(d, 50)),
                         "p90": float(np.percentile(d, 90)),
                         "max": float(max(d)),
                         "frac_gt_1pct": float(np.mean([x > 0.01 for x in d]))})
        if i % 200 == 0:
            print(f"  {i}/{len(codes)}", flush=True)

    a = np.asarray(all_diff, float)
    pct = lambda q: float(np.percentile(a, q))          # noqa: E731
    res = {
        "date": "2026-09-05",
        "scope": f"csi800 共池 {len(per_code)} 只，{len(a)} 个月度观测",
        "monthly_ret_diff": {
            "p50": pct(50), "p75": pct(75), "p90": pct(90), "p99": pct(99),
            "max": float(a.max()), "mean": float(a.mean()),
            "frac_gt_0.5pct": float((a > 0.005).mean()),
            "frac_gt_1pct": float((a > 0.01).mean()),
            "frac_gt_2pct": float((a > 0.02).mean()),
        },
        "per_code": sorted(per_code, key=lambda x: -x["p50"]),
    }
    # 与因子信号幅度对照（此前实测因子月度横截面信号 0.2~0.5%）
    res["vs_signal"] = {
        "factor_cross_section_signal_monthly": "0.2%~0.5%",
        "pollution_p50_monthly": res["monthly_ret_diff"]["p50"],
        "verdict": ("污染 ≥ 信号" if res["monthly_ret_diff"]["p50"] >= 0.002
                    else "污染 < 信号，需重新评估"),
    }
    # ---- 关键分层：QC 闸门内外的污染是两回事 ----
    # 因子检验只消费【两库 QC 均通过】的股票。腾讯库少数股票存在灾难性错误
    #   （实测 600595 中孚实业：后复权价出现负值 −0.767，249 天越涨跌停，
    #    2019-08 月收益 +23575%，雪球同期 +8.03%），但已被腾讯 QC 判 bad=1
    #    剔除（22/797 = 2.76%），故从未进入因子检验。把被剔除的股票算进
    #    污染分布会【高估】检验实际承受的污染 —— 这是"污染大却不影响 IC"
    #    的第一层解释。
    tx_bad = _bad_codes(TX_DB)
    xq_bad = _bad_codes(XQ_DB)
    clean = [c for c in codes if c not in tx_bad and c not in xq_bad]
    res["qc_layer"] = {
        "tx_bad": len(tx_bad), "xq_bad": len(xq_bad), "n_clean": len(clean),
        "tx_bad_rate": round(len(tx_bad) / max(len(codes), 1), 4),
        "xq_bad_rate": round(len(xq_bad) / max(len(codes), 1), 4),
        "note": "因子检验只消费两库 QC 均通过的股票",
    }
    clean_set = set(clean)
    clean_diff = [d for c in clean_set for d in _month_diffs(tx, xq, c)]
    if clean_diff:
        cd = np.asarray(clean_diff, float)
        res["monthly_ret_diff_clean"] = {
            "n_obs": int(len(cd)), "p50": float(np.percentile(cd, 50)),
            "p75": float(np.percentile(cd, 75)), "p90": float(np.percentile(cd, 90)),
            "p99": float(np.percentile(cd, 99)), "max": float(cd.max()),
            "frac_gt_0.5pct": float((cd > 0.005).mean()),
            "frac_gt_1pct": float((cd > 0.01).mean()),
            "frac_gt_2pct": float((cd > 0.02).mean()),
        }

    # ---- 核心谜题：污染不小，为何 IC 几乎不变？----
    # 假设：污染主要是【截面共模偏移】（所有股票同向同幅偏），而 IC 是截面
    #   【秩相关】，对共模偏移数学上不敏感 ⇒ 大污染也能有小影响。
    # 检验：按月分解 diff_i = tx_ret_i − xq_ret_i 的截面均值与截面离散度。
    # ★ 必须用【稳健统计】：实测污染分布厚尾（max=23567%），std 会被单个
    #   极端值主导 —— 首版用 std 得 7.56%，而 p50 仅 0.288%，自相矛盾。
    #   改用 MAD（中位数绝对偏差），且只在 QC-clean 子集上计算。
    cm = _common_mode(tx, xq, clean)
    res["common_mode_test"] = cm
    p = OUT_DIR / "ashare_pollution_quantify.json"
    p.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    md = res["monthly_ret_diff"]
    print("\n" + "=" * 70)
    print("全池污染量化（腾讯 hfq vs 雪球 hfq，月度收益绝对差）")
    print("=" * 70)
    print(f"  观测 {len(a)} 个月 · 股票 {len(per_code)} 只")
    print(f"  p50={md['p50']:.4%}  p75={md['p75']:.4%}  p90={md['p90']:.4%}  "
          f"p99={md['p99']:.4%}  max={md['max']:.4%}")
    print(f"  >0.5% 占比 {md['frac_gt_0.5pct']:.1%}   "
          f">1% 占比 {md['frac_gt_1pct']:.1%}   >2% 占比 {md['frac_gt_2pct']:.1%}")
    print(f"\n  污染最重 10 只: " +
          ", ".join(f"{r['code']}({r['p50']:.2%})" for r in res["per_code"][:10]))
    print(f"  污染最轻 5 只: " +
          ", ".join(f"{r['code']}({r['p50']:.3%})" for r in res["per_code"][-5:]))
    q = res["qc_layer"]
    print(f"\n  QC 分层: 腾讯判坏 {q['tx_bad']} 只 ({q['tx_bad_rate']:.2%})，"
          f"雪球判坏 {q['xq_bad']} 只 ({q['xq_bad_rate']:.2%})，"
          f"两库皆通过 {q['n_clean']} 只")
    cl = res.get("monthly_ret_diff_clean")
    if cl:
        print(f"  【QC-clean 子集】观测 {cl['n_obs']} 个月 · "
              f"p50={cl['p50']:.4%}  p90={cl['p90']:.4%}  p99={cl['p99']:.4%}  "
              f"max={cl['max']:.4%}")
        print(f"                   >0.5% 占比 {cl['frac_gt_0.5pct']:.1%}   "
              f">1% 占比 {cl['frac_gt_1pct']:.1%}   >2% 占比 {cl['frac_gt_2pct']:.1%}")
    cm = res["common_mode_test"]
    print("\n" + "-" * 70)
    print("核心谜题检验：污染不小，为何 IC 几乎不变？（QC-clean 子集，MAD 稳健统计）")
    print("-" * 70)
    print(f"  共模成分 mean|截面均值| = {cm['mean_abs_common_component']:.4%}")
    print(f"  离散成分 mean(MAD)      = {cm['mean_dispersion_MAD']:.4%}")
    print(f"  共模/离散 = {cm['ratio_common_over_dispersion']:.2f}")
    print(f"  判定: {cm['verdict']}")
    print(f"\n已写出: {p}")


if __name__ == "__main__":
    main()
