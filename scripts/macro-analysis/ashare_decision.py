# -*- coding: utf-8 -*-
"""个股决策层（§5 落地 · headless 单 agent 模式）—— 把「判据 + 名单」合成「明确决策」。

定位：这是把 Route V（风险判决书）的分析结果**转成决策**的那一层，对应 workflow §5
「决策层（headless 单 agent 模式：分析师→风控→PM 终审）」的 PM 终审角色。

★ 当前决策空间（诚实边界，硬写死）：
    正向「买入」alpha = 0（价量 5 因子 + 基本面 14 因子全判死/不足/边界，另类数据 3 源
    未产出可交付标签）。⇒ **本层只输出三态：回避 / 观察 / 中性，不输出「买入」。**
    正向信号进来后再扩展（届时须重跑三关，不得直接套本层三态）。

决策合成规则（确定性，无主观加权）：
    1. 数据质量 FAIL                     → 无法判定（数据自证错误，先修数据）
    2. 任一【已验证】bad 风险标签命中     → 回避 AVOID
       （两融 CROWDED / 农业池 AVOID）
    3. 有 warn 警示（含边界外推/描述性）  → 观察 WATCH
       （两融 ELEVATED / 农业 COLLAPSE / 深度回撤 / 波动率高位 / qc 未判定）
    4. 否则                               → 中性 NEUTRAL（附「无 ≠ 安全」）

用法：
  python ashare_decision.py --code 600519
  python ashare_decision.py --code 300871 --asof 2026-09-02 --json-only
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import ashare_risk_verdict as rv  # noqa: E402

OUT = rv.OUT

# 决策档位（顺序即优先级）
DECISION = {
    "INDETERMINATE": "无法判定",
    "AVOID": "回避",
    "WATCH": "观察",
    "NEUTRAL": "中性",
}
# 触发「回避」的已验证风险标签 key（数据质量 FAIL 单独走 INDETERMINATE）
AVOID_KEYS = {"margin", "agri_np_yoy"}


def decide(v: dict) -> dict:
    """把 verdict 合成决策。返回 {decision, level, reasons, buy_signal}。"""
    # 1. 数据质量
    if v["quality"]["verdict"] == "FAIL":
        return dict(
            decision="INDETERMINATE", level="fail",
            reasons=["数据质量判坏（hfq_qc 自证错误）→ 分析不可信，先修数据再决策"],
            buy_signal=False)

    hits = v["hits"]
    # 2. 已验证 bad 风险标签（回避依据）
    bad = [h for h in hits
           if h["severity"] == "bad" and h["validated"] and h["key"] in AVOID_KEYS]
    if bad:
        return dict(
            decision="AVOID", level="bad",
            reasons=[f"{h['label']}（{h['basis']}）" for h in bad],
            buy_signal=False)

    # 3. warn 警示（边界外推 / 描述性）
    warn = [h for h in hits if h["severity"] == "warn"]
    if warn:
        return dict(
            decision="WATCH", level="warn",
            reasons=[f"{h['label']}：{h['reason']}" for h in warn],
            buy_signal=False)

    # 4. 中性
    return dict(
        decision="NEUTRAL", level="ok",
        reasons=["无任何已验证风险标签命中"],
        buy_signal=False)


def render_decision(code: str, name: str, v: dict, d: dict) -> str:
    L = []
    title = f"# 个股决策 · {code}{(' ' + name) if name else ''}"
    L.append(title)
    L.append("")
    L.append(f"- 生成时间：{v['generated_at']}　行情库：`{v['price_db']}`")
    L.append(f"- 参照系：`{v['coverage']['tier']}` → {v['coverage']['text']}")
    L.append("")
    L.append(f"## 决策结论　→　**{DECISION[d['decision']]}**（`{d['decision']}`）")
    L.append("")
    for r in d["reasons"]:
        L.append(f"- {r}")
    L.append("")
    L.append("> ⚠ **本决策层不产出「买入」**。正向 alpha = 0（价量 5 因子为风格马甲、"
             "基本面 14 因子全判死/不足/边界、另类数据 3 源未产出可交付标签）。"
             "「回避 / 观察 / 中性」是当前唯一可交付的决策空间。")
    L.append("")
    L.append("### 决策依据（详见风险判决书）")
    L.append("")
    if v["hits"]:
        L.append("| 标签 | 档位 | 已验证 | 依据 |")
        L.append("|---|---|---|---|")
        for h in v["hits"]:
            L.append(f"| {h['label']} | {h['severity']} | "
                     f"{'✓' if h['validated'] else '—'} | {h['basis'][:60]} |")
    else:
        L.append("无命中标签。")
    L.append("")
    L.append("### 边界声明")
    L.append("")
    L.append("- 「中性」≠ 安全：现有风险清单只有两融拥挤、农业池净利同比两条，覆盖面极窄。")
    L.append("- 「观察」多为边界外推或描述性警示，未经回测验证，不得当作信号。")
    L.append("- 幸存者偏差：中证 800 成分快照回看，绝对收益水平不可信，差分类判据免疫。")
    L.append("- 本输出不构成投资建议，人工 gate 前不可作为决策依据。")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--asof", default=None)
    ap.add_argument("--price-db", default=None)
    ap.add_argument("--json-only", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    try:
        v = rv.verdict(a.code, a.name, a.asof, a.price_db)
    except rv.VerdictError as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 2

    d = decide(v)
    result = dict(code=a.code, name=a.name,
                  generated_at=datetime.now().isoformat(timespec="seconds"),
                  tier=v["coverage"]["tier"], decision=d["decision"],
                  decision_text=DECISION[d["decision"]],
                  reasons=d["reasons"], buy_signal=d["buy_signal"],
                  verdict=v)

    if not a.json_only:
        print(render_decision(a.code, a.name, v, d))

    out = Path(a.out) if a.out else (
        OUT / datetime.now().strftime("%Y-%m-%d")
        / f"decision_{a.code}_{Path(v['price_db']).stem}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    if not a.json_only:
        print(f"\n[JSON] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
