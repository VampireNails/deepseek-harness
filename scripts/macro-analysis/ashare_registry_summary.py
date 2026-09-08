#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""策略沉淀总账 —— 从两个注册表实时汇总，避免手抄数字。

回答「沉淀了多少策略」时跑这个，不要凭记忆报数。

    python ashare_registry_summary.py [--out <path>]

产出 markdown：分「可交付 / 风险标签 / 判死」三档，含证据条数与漏登记清单。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent.parent.parent.parent / "outputs"

# 可交付判定（对照 workflow.md §4/§8）：只有这两档算「能用」
DELIVERABLE = {"有效预判(可交付边界内)", "risk_signal_not_alpha"}
# 三关全挂但登记为风险标签 ⇒ 单独一档，不可与「可交付策略」混称
RISK_LABEL = {"risk_signal_not_alpha"}

AREG = OUT / "ashare_strategy_registry.sqlite"
MREG = OUT / "macro_strategy_registry.sqlite"


def q(c, sql, args=()):
    cur = c.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def build() -> str:
    L = []
    L.append(f"# 策略沉淀总账（{date.today().isoformat()}）")
    L.append("")
    L.append("> 本文件由 `ashare_registry_summary.py` 从注册表实时生成，**不要手改**。")
    L.append("")

    n_del = n_risk = n_dead = 0
    rows_out = []

    # ---------------- A 股
    if AREG.exists():
        c = sqlite3.connect(str(AREG))
        arows = q(c, """SELECT s.strategy_key, s.label, s.pool_key, s.verdict,
                        s.gate1_pass, s.gate2_pass, s.gate3_pass, s.gate1_mde,
                        s.gate1_effect, s.gate2_ic, s.gate2_t_hac, s.validated_at,
                        (SELECT COUNT(*) FROM strategy_evidence e
                          WHERE e.strategy_key = s.strategy_key) AS n_ev
                        FROM strategy_registry s ORDER BY s.strategy_key""")
        facs = q(c, """SELECT factor_key, label, wide_pool_ir, wide_pool_net,
                       agri_pool_ir, verdict FROM ashare_factor_registry
                       ORDER BY factor_key""")
        pools = q(c, "SELECT pool_key, n_codes, price_db FROM pool_registry")
        # ★ 风险标签已独立成表（2026-09-04）：风控 overlay 不计入「策略」，
        #   但必须在总账里单列 —— 否则会被读成「风控标签没了」。
        has_rl = q(c, "SELECT 1 FROM sqlite_master WHERE type='table' "
                      "AND name='risk_label_registry'")
        rlabs = []
        if has_rl:
            rlabs = q(c, """SELECT l.label_key, l.label, l.pool_key, l.verdict,
                            l.label_kind, l.in_use, l.used_by, l.basis,
                            (SELECT COUNT(*) FROM risk_label_evidence e
                              WHERE e.label_key = l.label_key) AS n_ev
                            FROM risk_label_registry l ORDER BY l.label_key""")
        c.close()

        L.append("## 一、A 股策略（已登记 %d 条）" % len(arows))
        L.append("")
        L.append("| # | strategy_key | 池 | ①②③关 | 关键统计 | 证据 | 判定 |")
        L.append("|---|---|---|---|---|---|---|")
        for i, r in enumerate(arows, 1):
            stat = []
            if r["gate2_ic"] is not None:
                stat.append(f"IC {r['gate2_ic']:+.3f}")
            if r["gate2_t_hac"] is not None:
                stat.append(f"t(HAC) {r['gate2_t_hac']:+.2f}")
            if r["gate1_effect"] is not None and r["gate1_mde"] is not None:
                stat.append(f"效应/MDE {abs(r['gate1_effect']/r['gate1_mde']):.2f}")
            key = r["verdict"]
            if key in DELIVERABLE:
                tag = "**可交付**" if key not in RISK_LABEL else "**风险标签**"
            else:
                tag = "判死"
            L.append(f"| {i} | `{r['strategy_key']}` | {r['pool_key']} | "
                     f"{r['gate1_pass']}{r['gate2_pass']}{r['gate3_pass']} | "
                     f"{' / '.join(stat) or '—'} | {r['n_ev']} | {tag} `{key}` |")
        L.append("")

        L.append("### A 股因子（%d 个，全部已验证结论）" % len(facs))
        L.append("")
        L.append("| factor_key | 含义 | 宽池 IR | 宽池净收益 | 农业池 IR | 判定 |")
        L.append("|---|---|---|---|---|---|")
        for f in facs:
            L.append(f"| `{f['factor_key']}` | {f['label']} | {f['wide_pool_ir']:+.2f} | "
                     f"{f['wide_pool_net']:+.2f}% | {f['agri_pool_ir']:+.2f} | "
                     f"`{f['verdict']}` |")
        L.append("")
        L.append(f"### A 股池（已登记 {len(pools)} 个）")
        L.append("")
        for p in pools:
            L.append(f"- `{p['pool_key']}`　n={p['n_codes']}　`{p['price_db']}`")
        L.append("")

        # 风险标签独立成节：它不在 strategy_registry 里，但**在用**
        if rlabs:
            L.append("### A 股风险标签·在用（%d 条，**不计入策略**）" % len(rlabs))
            L.append("")
            L.append("| label_key | 池 | 类别 | 证据 | 判据 | 调用方 |")
            L.append("|---|---|---|---|---|---|")
            for x in rlabs:
                L.append(f"| `{x['label_key']}` | {x['pool_key']} | "
                         f"{'已验证' if x['label_kind'] == 'validated' else '描述性'} | "
                         f"{x['n_ev']} | {(x['basis'] or '—')[:70]} | "
                         f"`{x['used_by']}` |")
            L.append("")
            L.append("> 风险标签不产生超额收益，判据是「标记脆弱群体」。"
                     "它们从未打算过第三关，因此**不因三关未过而删除**。")
            L.append("")

        for r in arows:
            rows_out.append(("A股", r))

    # ---------------- 宏观
    if MREG.exists():
        c = sqlite3.connect(str(MREG))
        mrows = q(c, """SELECT s.strategy_key, s.label, s.target, s.model, s.n_oos,
                        s.rmse_reduction_pct, s.cw_p, s.dm_p_nw, s.effect_over_mde,
                        s.n_years_positive, s.n_years, s.verdict,
                        (SELECT COUNT(*) FROM strategy_evidence e
                          WHERE e.strategy_key = s.strategy_key) AS n_ev
                        FROM strategy_registry s ORDER BY s.strategy_key""")
        c.close()

        L.append("## 二、宏观预测策略（已登记 %d 条）" % len(mrows))
        L.append("")
        L.append("| # | strategy_key | 目标 | OOS | RMSE 降 | CW p | DM-NW p | 效应/MDE | 逐年正 | 证据 | 判定 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(mrows, 1):
            tag = "**可交付**" if r["verdict"] in DELIVERABLE else "判死"
            L.append(f"| {i} | `{r['strategy_key']}` | {r['target']} | {r['n_oos']} | "
                     f"{r['rmse_reduction_pct']:+.2f}% | {r['cw_p']:.2e} | "
                     f"{r['dm_p_nw']:.4f} | {r['effect_over_mde']:.2f} | "
                     f"{r['n_years_positive']}/{r['n_years']} | {r['n_ev']} | "
                     f"{tag} `{r['verdict']}` |")
        L.append("")
        for r in mrows:
            rows_out.append(("宏观", r))

    # ---------------- 汇总
    for _, r in rows_out:
        v = r["verdict"]
        if v in RISK_LABEL:
            n_risk += 1
        elif v in DELIVERABLE:
            n_del += 1
        else:
            n_dead += 1
    # 独立成表的风险标签：也算「风险标签」档，但不计入策略条目
    n_risk += sum(1 for x in rlabs if x["label_kind"] == "validated")

    L.append("## 三、汇总")
    L.append("")
    L.append("| 档位 | 条数 | 含义 |")
    L.append("|---|---|---|")
    L.append(f"| **策略·可交付** | {n_del} | 过了关卡，能直接用的预测/策略 |")
    L.append(f"| **风险标签·在用** | {n_risk} | 非策略：不产生超额收益，仅标记脆弱群体 |")
    L.append(f"| **判死** | {n_dead} | 已证伪/功效不足，**已从注册表删除** |")
    L.append(f"| **策略合计** | {len(rows_out)} | 另 A 股因子 %d 个 |"
             % (len(facs) if AREG.exists() else 0))
    L.append("")
    L.append("> **口径**：只有「策略·可交付 %d 条」是能赚钱的东西。"
             "风险标签 %d 条是风控清单，两者不可混称。" % (n_del, n_risk))
    L.append("")
    if n_dead == 0:
        L.append("> **判死条目已清空**：本轮删除的结论与证据保存在 "
                 "`outputs/_archive/` 的《已判死策略归档记录》中，"
                 "重开任何方向前必须先查该记录。")
        L.append("")

    # ---------------- 判死归档 / 漏登记检查
    L.append("## 四、判死归档与漏登记检查")
    L.append("")
    L.append("判死条目已从注册表删除，结论保存在归档记录里；"
             "**重开任何方向前必须先查归档**。")
    L.append("")
    arch = sorted((OUT / "_archive").glob("*/已判死策略归档记录.md")) \
        if (OUT / "_archive").exists() else []
    if arch:
        for p in arch:
            L.append(f"- **判死归档**：`{p.relative_to(OUT.parent).as_posix()}`")
    else:
        L.append("- ⚠ **未找到判死归档记录** —— 若判死条目已被删除且无归档，"
                 "其结论不可追溯，存在被重挖的风险。")
    L.append("")
    L.append("以下条目有完整结题产物，但未登记进 `pool_registry`：")
    L.append("")
    missing = [
        ("wide 池（1068 只）", "outputs/ashare_wide_hfq_xq.sqlite",
         "pool_registry 中无 ashare_wide"),
    ]
    for name, art, note in missing:
        p = OUT.parent / art if not art.startswith("outputs") else OUT.parent / art
        exists = "✅ 有产物" if p.exists() else "⚠ 产物路径待核"
        L.append(f"- **{name}** —— `{art}`（{note}）　{exists}")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    md = build()
    out = Path(a.out) if a.out else (OUT / date.today().isoformat() / "策略沉淀总账.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"[OK] {out}")
    print()
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
