#!/usr/bin/env python3
"""Capability-layer contract test for the equity-research preset.

WHY THIS EXISTS
---------------
Every previous round validated the preset by *running the whole agent* and then
grep-ing the report (14-18 min per run). That is a regression test with a
14-minute feedback loop, so nobody runs it after small changes — exactly how
the cash-flow capability stayed missing for months while the workflow grew.

This script tests the CAPABILITY LAYER directly: for each tool, does it return
the fields the workflow's 必答维度清单 depends on? Seconds, not minutes.

WHY MULTIPLE SAMPLE CODES (lesson, 2026-09-08)
----------------------------------------------
Three real bugs were found ONLY by running a second / edge-case code:
  1. "read 必须含'背离'字样"      → false alarm on 000651 (OCF +188亿, same sign)
  2. 货币资金方向词写死"降至"      → 601398 输出"33990亿降至35177亿"(实为上升)
  3. PE 负值                     → 000002 输出"约需 -0.4 年回本"(无意义句)
A single-code test cannot detect over-fitted contracts. Hence the pool.

Usage:
    python ashare_mcp_selftest.py                 # default code 600416
    python ashare_mcp_selftest.py --code 000651
    python ashare_mcp_selftest.py --pool          # multi-code stress (default)
    python ashare_mcp_selftest.py --quick         # single code, fast

Exit code 0 = all contracts hold; 1 = at least one FAIL.
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CODE = "600416"

# Boundary-covering pool. Each entry is (code, why-this-code-is-here).
POOL = [
    ("600416", "制造业·亏损：OCF 为负、扣非为负、PE 失真"),
    ("000651", "白马：OCF 与净利同为正（防'背离'契约过拟合）"),
    ("601398", "银行：无毛利率/存货、负债率 92%（防误判为高风险）"),
    ("000002", "地产·亏损：PE 为负（防输出'负几年回本'）"),
    ("600519", "高毛利·现金充裕"),
    ("688981", "科创板"),
    ("300750", "创业板"),
    ("002027", "传媒：主营构成分段多（27 段）"),
]

# Negative cases: these MUST fail loudly, and the error must be actionable.
# An un-actionable fail-loud is how the agent ends up writing "数据缺失" —
# which is precisely the failure mode this whole capability layer exists to kill.
NEGATIVE_CASES = [
    ("600122", "ST 股（*ST宏图）：必须 fail-loud，且须识别为 ST 覆盖盲区",
     ("ST", "能力边界", "risk_scan")),
    ("300108", "ST 股（*ST吉药）：同上，验证非单只偶然",
     ("ST", "能力边界")),
]


def _load():
    spec = importlib.util.spec_from_file_location(
        "ashare_research_mcp", HERE / "ashare_research_mcp.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# contract: tool -> list of (label, predicate)
def _checks():
    def s0(r):
        return (r.get("series") or [{}])[0]

    return {
        "ashare_income": [
            ("有 series 且非空", lambda r: bool(r.get("series"))),
            ("含扣非净利字段", lambda r: "扣非净利_亿" in s0(r)),
            ("含非经常占比", lambda r: "非经常占净利_%" in s0(r)),
            ("扣非为推算值时已标注",
             lambda r: (s0(r).get("扣非净利_亿") is None)
             or bool(s0(r).get("扣非推算方式"))),
        ],
        "ashare_cashflow": [
            ("有 series 且非空", lambda r: bool(r.get("series"))),
            ("含经营净现金流", lambda r: "经营净现金流_亿" in s0(r)),
            # 契约：OCF 与净利**异号**时才必须有背离提示；同号时不得有。
            ("净利背离检验与数据一致", lambda r: (
                s0(r).get("经营净现金流_亿") is None
                or s0(r).get("归母净利_亿") is None
                or (s0(r).get("经营净现金流_亿") >= 0) == (s0(r).get("归母净利_亿") >= 0)
                or any("背离" in x for x in r.get("read", [])))),
        ],
        "ashare_balance": [
            ("有 series 且非空", lambda r: bool(r.get("series"))),
            ("含货币资金(非0)", lambda r: bool(s0(r).get("货币资金_亿"))),
            ("含应收账款", lambda r: "应收账款_亿" in s0(r)),
            # 负债率可以是任意正数（资不抵债 >100 是真实情况），不能卡 0-100
            ("负债率是有效数值", lambda r: isinstance(s0(r).get("资产负债率_%"), (int, float))),
            # 新增：方向词必须与数值一致（修 601398「33990降至35177」反向 bug）
            ("货币资金方向词与数值一致", lambda r: _direction_ok(r)),
            # 新增：None 科目必须给解释，不能让 agent 当"数据缺失"
            ("None 科目已解释", lambda r: (
                all(s0(r).get(k) for k in ("应收账款_亿", "存货_亿"))
                or any("None" in x for x in r.get("read", [])))),
        ],
        "ashare_segments": [
            ("有 segments", lambda r: bool(r.get("segments"))),
            ("收入占比已换算为百分数(>1)", lambda r: any(
                (s.get("收入占比_%") or 0) > 1 for s in r.get("segments", []))),
        ],
        "ashare_valuation": [
            ("有总市值", lambda r: bool(r.get("总市值_亿"))),
            ("有 PB", lambda r: bool(r.get("PB"))),
            # 新增：PE 失真 = 过高 **或为负**。首版只判 >100，漏掉负 PE。
            ("PE 失真(过高或为负)时有护盾", lambda r: (
                r.get("PE_TTM") is None
                or (0 < (r.get("PE_TTM") or 0) <= 100)
                or any("失真" in x or "亏损" in x for x in r.get("read", [])))),
            # 新增：绝不输出「约需 -0.4 年回本」这类无意义句。
            # 注意只匹配"真的给出了负年数结论"，不能笼统匹配含"-"且含"年回本"——
            # 否则会命中"「负几年回本」是无意义的说法"这句教育性说明（已误报过一次）。
            ("无'负年数回本'表述", lambda r: not any(
                re.search(r"(约需|需要|需)\s*-\s*\d", x) for x in r.get("read", []))),
        ],
        "ashare_risk_scan": [
            ("status 非 UNAVAILABLE", lambda r: r.get("status") != "UNAVAILABLE"),
            ("扫描条目 > 0", lambda r: (r.get("scanned") or 0) > 0),
            ("零命中时给出盲区警告", lambda r: bool(r.get("hits")) or any(
                "盲区" in x for x in r.get("read", []))),
        ],
    }


def _direction_ok(r) -> bool:
    """货币资金 read 里的方向词必须与数值一致。

    抓的是 2026-09-08 在 601398 上发现的 bug：方向词写死"降至"，
    而实际 33990→35177 是上升，导致 agent 会写出反向结论。
    """
    series = r.get("series") or []
    if len(series) < 2:
        return True
    cash, prev = series[0].get("货币资金_亿"), series[1].get("货币资金_亿")
    if not cash or not prev:
        return True
    for line in r.get("read", []):
        if "货币资金从上一期" not in line:
            continue
        rose = cash >= prev
        if rose and "降至" in line:
            return False
        if (not rose) and "升至" in line:
            return False
    return True


def _run_negative(mod, code: str, why: str, must_contain=("退市", "代码写错", "行情源")
                  ) -> int:
    """Negative case: an unusable code MUST fail loud AND say why."""
    print(f"\n=== 负样本 · code={code}（{why}）===")
    try:
        mod._DISPATCH["ashare_income"]({"code": code})
        print("  [FAIL] 本应抛异常（fail-loud），却静默返回成功")
        return 1
    except Exception as e:
        msg = str(e)
        missing = [k for k in must_contain if k not in msg]
        if not missing:
            hit = [x for x in msg.split("。") if "诊断" in x]
            print(f"  [ OK ] fail-loud 且含可操作诊断：{(hit[0] if hit else msg)[:96]}…")
            return 0
        print(f"  [FAIL] 抛了异常但诊断缺少 {missing}：{msg[:120]}")
        return 1


def run(mod, code: str, tag: str = "") -> int:
    failures = 0
    print(f"\n=== 能力层自检 · code={code}{(' · ' + tag) if tag else ''} ===")
    for tool, checks in _checks().items():
        fn = mod._DISPATCH[tool]
        try:
            result = fn({"code": code})
        except Exception as e:
            print(f"  [FAIL] {tool}: 调用异常 {type(e).__name__}: {str(e)[:80]}")
            failures += 1
            continue
        bad = []
        for label, predicate in checks:
            try:
                ok = predicate(result)
            except Exception as e:
                ok, label = False, f"{label} (判定异常 {type(e).__name__})"
            if not ok:
                bad.append(label)
        if bad:
            failures += 1
            print(f"  [FAIL] {tool}: " + "; ".join(bad))
        else:
            extra = ""
            if tool == "ashare_risk_scan":
                extra = f" (扫描 {result.get('scanned')} 条, 命中 {len(result.get('hits', {}))} 类)"
            elif tool == "ashare_cashflow":
                extra = f" (OCF {s0v(result)} 亿)"
            elif tool == "ashare_valuation":
                extra = f" (PE {result.get('PE_TTM')} PB {result.get('PB')})"
            elif tool == "ashare_balance":
                extra = f" (负债率 {s0v(result, '资产负债率_%')}%)"
            print(f"  [ OK ] {tool}{extra}")
    print(f"  → {'全部通过' if failures == 0 else str(failures) + ' 项未通过'}")
    return failures


def s0v(r, key="经营净现金流_亿"):
    return (r.get("series") or [{}])[0].get(key)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--code", default=DEFAULT_CODE)
    ap.add_argument("--pool", action="store_true",
                    help="多样本压力测试（默认，覆盖银行/亏损/科创等边界）")
    ap.add_argument("--quick", action="store_true",
                    help="只跑 --code 指定的单只（最快）")
    a = ap.parse_args()

    mod = _load()
    total = 0
    if a.quick:
        total += run(mod, a.code)
    else:
        for code, tag in POOL:
            total += run(mod, code, tag)
        for code, why, must in NEGATIVE_CASES:
            total += _run_negative(mod, code, why, must)

    print(f"\n{'=' * 56}")
    print(f"总计：{'全部通过 ✓' if total == 0 else f'{total} 项未通过 ✗'}")
    print("=" * 56)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
