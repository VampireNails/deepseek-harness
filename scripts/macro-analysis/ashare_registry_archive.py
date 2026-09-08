#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ashare_registry_archive.py —— 判死结论留档导出（删除前的强制前置工序）。

用途
----
物理删除注册表判死条目**之前**，必须先把它们的完整结论、三关判据、边界与证据
导出为永久留档。否则阴性结论一旦删除，同一条路会被重挖（2026-09-04 上午已实证：
一份要求"重做海龟/双均线/MACD/KDJ/放量突破"的蓝图，正是靠注册表里 5 个因子的
non_robust 记录挡住的）。

纪律
----
1. **数字零手抄**：全部字段从 sqlite 直接读出，不经过人工。
2. **未登记条目只指路**：没有进注册表的判死结论（波动率择时 / 行业择时 / 港股线），
   留档里只记录结论文件路径并复制原文，不人工转写数字。
3. **先留档、后删除**：本脚本输出的 md 与 json 校验无误后，才允许执行删除。

分类判据（与 ashare_registry_summary.py 一致，禁止各写一套）
------------------------------------------------------------
- DELIVERABLE  可交付：verdict 含「可交付」且不含「不可交付」
- RISK_LABEL   风险标签·在用：verdict 含 risk_signal_not_alpha
- DEAD         判死：其余全部（含信息性零结果、功效不足、无效、regime 依赖转负）

用法
----
    python ashare_registry_archive.py                 # 导出到 outputs/_archive/<今日>/
    python ashare_registry_archive.py --out DIR       # 指定目录
    python ashare_registry_archive.py --no-copy       # 不复制原文附件
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# 工作区根的唯一判据 = 含 outputs/ashare_strategy_registry.sqlite。
# 不能只用 "有 outputs/ 目录" —— 任何一层误建 outputs/ 都会把探测截在半路
# （2026-09-04 实测：my-deepseek-harness/outputs/ 空目录导致 ROOT 判错）。
_MARKER = "outputs/ashare_strategy_registry.sqlite"
ROOT = _HERE
for _p in _HERE.parents[:6]:
    if (_p / _MARKER).exists():
        ROOT = _p
        break
else:
    raise RuntimeError("未找到工作区根（向上 6 层内无 %s）" % _MARKER)
OUT = ROOT / "outputs"

ASHARE_DB = OUT / "ashare_strategy_registry.sqlite"
MACRO_DB = OUT / "macro_strategy_registry.sqlite"

# 未登记进注册表、但有完整结题报告的判死结论。
# 只记录结论文件路径 —— 数字一律不人工转写，避免抄错。
UNREGISTERED_DEAD = [
    dict(
        key="vol_targeting_forecast",
        label="A股波动率预测 → 目标波动率择时（方向 1）",
        verdict="判死·功效不足（关卡①②全过、关卡③挂）",
        summary=("关卡①可预测性 |效应|/MDE 1.87~5.22 逐年 13/13 = GO；"
                 "关卡② QLIKE 降 24.4%~38.9%、CW/NW-DM p≈0 = 通过；"
                 "关卡③ 换成仓位后风险调整收益仅 +0.20%~+1.52%/年、"
                 "|效应|/MDE 0.30~1.05 ⇒ 功效不足。统计算出大胜，经济上等于零。"),
        artifacts=[
            "outputs/2026-09-04/方向1-预测风险-结题报告.md",
            "outputs/2026-09-04/vol_forecast_h{5,20,60}.json",
            "outputs/2026-09-04/vol_targeting_h{5,20,60}*.json",
        ],
        scripts=[
            "scripts/macro-analysis/ashare_risk_preflight.py",
            "scripts/macro-analysis/ashare_vol_forecast.py",
            "scripts/macro-analysis/ashare_vol_targeting.py",
        ],
    ),
    dict(
        key="macro_sector_timing",
        label="宏观 → 行业择时（口径 A 横截面轮动 / 口径 B 纯时序择时）",
        verdict="判死·结构性（口径B）＋ 功效不足（口径A）",
        summary=("口径B 纯时序择时：MDE(r)=0.18~0.26 而现实 r 仅 0.05~0.10，"
                 "N 被月频锁死，扩数据无解 ⇒ 结构性判死。"
                 "口径A 行业横截面轮动：样本分割后 IC +0.0102、t(NW) 0.68、"
                 "逐年 3/6、MDE=0.0470 ⇒ 点效应过小，不值得继续。"
                 "宏观交易化落点整体关闭，nowcast 重新定位为宏观状态监测。"),
        artifacts=[
            "outputs/2026-09-03/宏观行业择时结题报告.md",
            "outputs/2026-09-03/sector_timing/industry_monthly_panel.json",
        ],
        scripts=[
            "scripts/macro-analysis/macro_sector_timing.py",
            "scripts/macro-analysis/macro_sector_rotation.py",
        ],
    ),
    dict(
        key="hk_equity_line",
        label="港股线（equity_* 系列，110 只全 hk 开头、未复权）",
        verdict="判死·无稳定 alpha + 功效不足（**仅交易信号层**）",
        summary=("【交易信号层】判死：无稳定 alpha + 功效不足。equity_* / quant_engine "
                 "系列连的是这条线，未做后复权、无 QC 门禁，不得据此产出交易信号。"
                 "结题结论见 .workbuddy/memory/MEMORY.md §3。"
                 "【2026-09-06 修正 · 判死≠禁采】港股是本 agent 的**投研标的**"
                 "（workflow §6 标的池 / universe 108 只），日K、公告、基本面照常采集维护，"
                 "用于描述性体检。此前据「判死」停掉港股日K 采集属口径误用，已回滚恢复。"),
        artifacts=[
            ".workbuddy/memory/MEMORY.md  §3「已结题结论（不再重开）」",
        ],
        scripts=[
            "scripts/macro-analysis/equity_*.py",
        ],
    ),
]


def classify(verdict: str) -> str:
    """分类判据。与 ashare_registry_summary.py 必须保持一致。"""
    v = (verdict or "").strip()
    if "risk_signal_not_alpha" in v:
        return "RISK_LABEL"
    if "可交付" in v and "不可交付" not in v:
        return "DELIVERABLE"
    return "DEAD"


def dump_table(db: Path, table: str) -> list:
    """读出整表，返回 dict 列表。"""
    if not db.exists():
        return []
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute("select * from %s" % table).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        c.close()


def collect() -> dict:
    """汇总两个注册表的全部内容 + 未登记判死条目。"""
    data = dict(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        sources=[],
        entries=[],
        factors=[],
        pools=[],
        unregistered_dead=UNREGISTERED_DEAD,
    )
    for db, scope in ((ASHARE_DB, "A股"), (MACRO_DB, "宏观")):
        if not db.exists():
            continue
        data["sources"].append(dict(db=str(db.relative_to(ROOT)), scope=scope,
                                    exists=True))
        for r in dump_table(db, "strategy_registry"):
            v = r.get("verdict") or ""
            data["entries"].append(dict(
                scope=scope, db=db.name, key=r.get("strategy_key"),
                label=r.get("label"), pool=r.get("pool_key"),
                verdict=v, category=classify(v),
                validated_at=r.get("validated_at"),
                boundary=r.get("boundary"),
                raw=r,
            ))
        for r in dump_table(db, "ashare_factor_registry"):
            v = r.get("verdict") or ""
            data["factors"].append(dict(scope=scope, db=db.name,
                                        key=r.get("factor_key"),
                                        label=r.get("label"),
                                        verdict=v, category=classify(v), raw=r))
        for r in dump_table(db, "pool_registry"):
            data["pools"].append(dict(scope=scope, db=db.name, raw=r))
    # evidence 单独挂到条目上
    for db in (ASHARE_DB, MACRO_DB):
        if not db.exists():
            continue
        evs = dump_table(db, "strategy_evidence")
        by_key: dict = {}
        for e in evs:
            k = e.get("strategy_key") or e.get("factor_key")
            by_key.setdefault(k, []).append(e)
        for ent in data["entries"]:
            if ent["db"] == db.name:
                ent["evidence"] = by_key.get(ent["key"], [])
        for f in data["factors"]:
            if f["db"] == db.name:
                f["evidence"] = by_key.get(f["key"], [])
    return data


CAT_TITLE = {
    "DELIVERABLE": "可交付（保留）",
    "RISK_LABEL": "风险标签·在用（保留，不计入策略）",
    "DEAD": "判死（本轮删除对象）",
}


def render_md(d: dict) -> str:
    L = []
    A = L.append
    A("# 判死结论归档记录")
    A("")
    A("> **本文件是删除操作的凭证。** 删除注册表判死条目**之前**先生成它，")
    A("> 之后即使注册表清干净了，也能凭本文件回答「这条路走过没有、结论是什么」。")
    A("> 数字全部由 `ashare_registry_archive.py` 从 sqlite 直接读出，**零人工转写**。")
    A("")
    A("- 生成时间：%s" % d["generated_at"])
    A("- 数据源：%s" % "、".join(s["db"] for s in d["sources"]))
    A("- 配套机器可读档：`已判死策略归档记录.json`")
    A("")

    # 计数总览
    from collections import Counter
    cc = Counter(e["category"] for e in d["entries"])
    fc = Counter(f["category"] for f in d["factors"])
    A("## 一、分类总览")
    A("")
    A("| 分类 | 策略条目 | 因子条目 | 处置 |")
    A("|---|---|---|---|")
    for cat in ("DELIVERABLE", "RISK_LABEL", "DEAD"):
        A("| %s | %d | %d | %s |" % (CAT_TITLE[cat], cc.get(cat, 0),
                                     fc.get(cat, 0),
                                     "保留" if cat != "DEAD" else "**删除**"))
    A("")

    # 逐类明细
    for cat in ("DELIVERABLE", "RISK_LABEL", "DEAD"):
        ents = [e for e in d["entries"] if e["category"] == cat]
        facs = [f for f in d["factors"] if f["category"] == cat]
        if not ents and not facs:
            continue
        A("## %s" % CAT_TITLE[cat])
        A("")
        if cat == "DEAD":
            A("> ⚠ **下列条目将被物理删除。** 保留本文件即保留其结论。")
            A("> 重开这些方向的判据：先在本文件里找到它，确认结论与边界，")
            A("> **再决定是否有新的信息源/规格能绕过原判据** —— 否则就是重挖。")
            A("")
        for e in ents:
            A("### `%s` · %s" % (e["key"], e["scope"]))
            A("")
            A("- **label**：%s" % (e.get("label") or "—"))
            A("- **pool**：%s" % (e.get("pool") or "—"))
            A("- **verdict**：`%s`" % e["verdict"])
            A("- **validated_at**：%s" % (e.get("validated_at") or "—"))
            raw = e["raw"]
            gates = {k: v for k, v in raw.items()
                     if k.startswith(("gate1_", "gate2_", "gate3_")) and v is not None}
            if gates:
                A("- **三关/检验字段**：")
                for k in sorted(gates):
                    A("    - `%s` = %s" % (k, gates[k]))
            if e.get("boundary"):
                A("- **boundary（原文）**：")
                A("")
                for line in str(e["boundary"]).splitlines():
                    A("    > %s" % line)
                A("")
            evs = e.get("evidence") or []
            if evs:
                A("- **evidence（%d 条）**：" % len(evs))
                for ev in evs:
                    A("    - %s" % json.dumps(
                        {k: v for k, v in ev.items() if v is not None},
                        ensure_ascii=False)[:600])
            A("")
        if facs:
            A("### 因子条目")
            A("")
            A("| factor_key | label | verdict | wide IR | wide 净收益 |")
            A("|---|---|---|---|---|")
            for f in facs:
                raw = f["raw"]
                A("| `%s` | %s | `%s` | %s | %s |" % (
                    f["key"], f.get("label") or "—", f["verdict"],
                    raw.get("wide_pool_ir"), raw.get("wide_pool_net")))
            A("")

    # 未登记的判死结论
    A("## 未进注册表的判死结论（只指路，不转写数字）")
    A("")
    A("> 这些方向从未登记进注册表，因此**不在删除范围内**。")
    A("> 但它们是重挖高发区，故在此并列，原文见下方路径。")
    A("")
    for u in d["unregistered_dead"]:
        A("### `%s` — %s" % (u["key"], u["label"]))
        A("")
        A("- **verdict**：%s" % u["verdict"])
        A("- **结论**：%s" % u["summary"])
        A("- **原文**：")
        for p in u["artifacts"]:
            A("    - `%s`" % p)
        A("- **脚本**：")
        for p in u["scripts"]:
            A("    - `%s`" % p)
        A("")

    A("## 本文件的用途与纪律")
    A("")
    A("1. **删除前**生成，删除后长期保留。它是防重挖的唯一凭证。")
    A("2. **重开任何方向前**，先在这里查一遍；若已有判死结论，")
    A("   必须指出「新的信息源 / 规格」能绕过哪一条原判据，否则不启动。")
    A("3. **数字以本文件为准**，不以记忆为准。")
    A("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="判死结论留档导出（删除前置工序）")
    ap.add_argument("--out", default=None, help="输出目录，默认 outputs/_archive/<今日>")
    ap.add_argument("--no-copy", action="store_true", help="不复制原文附件")
    args = ap.parse_args()

    out_dir = Path(args.out) if args.out else OUT / "_archive" / datetime.now().strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    attach = out_dir / "原文附件"
    if not args.no_copy:
        attach.mkdir(exist_ok=True)

    d = collect()
    md = render_md(d)

    md_path = out_dir / "已判死策略归档记录.md"
    js_path = out_dir / "已判死策略归档记录.json"
    md_path.write_text(md, encoding="utf-8")
    js_path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

    # 复制未登记条目的原始报告
    n_copy = 0
    if not args.no_copy:
        for u in d["unregistered_dead"]:
            for p in u["artifacts"]:
                p2 = p.split()[0]          # 去掉 §3 之类的行内定位
                src = ROOT / p2
                if src.exists() and src.is_file():
                    dst = attach / src.name
                    shutil.copy2(src, dst)
                    n_copy += 1

    from collections import Counter
    cc = Counter(e["category"] for e in d["entries"])
    fc = Counter(f["category"] for f in d["factors"])
    print("留档目录     : %s" % out_dir)
    print("策略条目     : 可交付 %d / 风险标签 %d / 判死 %d"
          % (cc.get("DELIVERABLE", 0), cc.get("RISK_LABEL", 0), cc.get("DEAD", 0)))
    print("因子条目     : %s" % dict(fc))
    print("未登记判死   : %d 项" % len(d["unregistered_dead"]))
    print("原文附件复制 : %d 个" % n_copy)
    print("输出         : %s" % md_path.name)
    print("              %s" % js_path.name)
    print()
    print("下一步：核对上面计数与预期一致后，才允许执行删除。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
