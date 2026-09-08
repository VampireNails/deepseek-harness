# -*- coding: utf-8 -*-
"""QC 闸门开/关对照：量化「剔除坏数据」对农业池回测结论的实际影响。"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
V = ROOT / "outputs" / "2026-09-03" / "_verify"
on = json.loads((V / "agri_bt_qc_on.json").read_text(encoding="utf-8"))
off = json.loads((V / "agri_bt_qc_off.json").read_text(encoding="utf-8"))

print("=" * 76)
print("农业池 `ashare_agri_backtest.py`：质检闸门 开 vs 关")
print("=" * 76)
print(f"  闸门开启: qc={json.dumps(on.get('qc'), ensure_ascii=False)}")
print(f"  闸门关闭: qc={json.dumps(off.get('qc'), ensure_ascii=False)}")
print(f"  面板只数: 开 {on['panel']['tickers']}  vs  关 {off['panel']['tickers']}")

# 逐策略 × 持有期 × 成本档：比较关键指标
COST = "中 0.50%（含常规滑点）"
rows = []
for skey in sorted(set(on["strategies"]) | set(off["strategies"])):
    so = on["strategies"].get(skey, {}) or {}
    sf = off["strategies"].get(skey, {}) or {}
    for h in sorted(set(so.get("by_holding", {})) | set(sf.get("by_holding", {})),
                    key=lambda x: int(x)):
        bo = so.get("by_holding", {}).get(h, {}) or {}
        bf = sf.get("by_holding", {}).get(h, {}) or {}
        co = (bo.get(COST) or {})
        cf = (bf.get(COST) or {})
        if not co or not cf:
            continue
        for metric in ("excess_net", "long_abs", "long_short_ref"):
            mo = (co.get(metric) or {})
            mf = (cf.get(metric) or {})
            for k in ("ann_return", "information_ratio", "t", "max_drawdown"):
                a, b = mo.get(k), mf.get(k)
                if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                    rows.append((abs(a - b), skey, h, metric, k, a, b))

rows.sort(reverse=True)
print()
print(f"可比指标共 {len(rows)} 项（策略×持有期×{COST}×多空口径×指标）")
print()
print("  差异最大的 12 项：")
print(f"  {'Δ':>10}  {'策略':<22} {'H':>3} {'口径':<15} {'指标':<18} {'开闸':>10} {'关闸':>10}")
for d, s, h, m, k, a, b in rows[:12]:
    print(f"  {d:>10.5f}  {s:<22} {h:>3} {m:<15} {k:<18} {a:>10.4f} {b:>10.4f}")

identical = sum(1 for r in rows if r[0] < 1e-9)
print()
print(f"  完全一致(Δ<1e-9)的指标: {identical} / {len(rows)}  "
      f"({identical/len(rows)*100:.1f}%)")
print(f"  最大绝对差异: {rows[0][0]:.6f}" if rows else "  无可比指标")

# 结论判定是否改变
print()
print("  结论判定对比（第三关口径：中成本多头超额>0 且 IR≥0.5）：")
diff_verdict = 0
for skey in sorted(set(on["strategies"]) | set(off["strategies"])):
    so = on["strategies"].get(skey, {}) or {}
    sf = off["strategies"].get(skey, {}) or {}
    for h in sorted(set(so.get("by_holding", {})) | set(sf.get("by_holding", {})),
                    key=lambda x: int(x)):
        co = (so.get("by_holding", {}).get(h, {}) or {}).get(COST) or {}
        cf = (sf.get("by_holding", {}).get(h, {}) or {}).get(COST) or {}
        eo = (co.get("excess_net") or {})
        ef = (cf.get("excess_net") or {})
        if not eo or not ef:
            continue
        vo = eo.get("ann_return", 0) > 0 and eo.get("information_ratio", 0) >= 0.5
        vf = ef.get("ann_return", 0) > 0 and ef.get("information_ratio", 0) >= 0.5
        if vo != vf:
            diff_verdict += 1
            print(f"    ⚠ {skey} H={h}: 开闸={'通过' if vo else '不通过'} "
                  f"→ 关闸={'通过' if vf else '不通过'}")
print(f"  判定翻转的策略×持有期组合: {diff_verdict} 个")
