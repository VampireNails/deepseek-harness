#!/usr/bin/env python3
"""Cross-period data-quality cross-checks (E11-E13)：量级 / 单位 / 同比离群。

背景（2026-09-01）：跨期量级审计发现两类致命错误，而既有 E01-E10 全部放过：
  ① 实体错配：万洲国际 2024H2 抽到子公司「河南雙匯」数据（FY 595 亿 vs 母公司 1900 亿量级）；
  ② 行取错：中国平安 2026H1 半年营收抽成 249,000 mCNY，原文半年营收 5,751 亿元
     （年报 1,028,925 mCNY 却完全正确）——金融类报表科目极多，LLM 易取错行。

检查项：
  E11 跨期量级：H2（全年）营收 / 相邻 H1（半年）营收 应 ≈ 2（带通 1.3~3.5）
  E12 单位一致性：同一 fact_key 跨期 unit 若不一致 → 币种/单位混合（第 8 轮中芯同款）
  E13 同比离群：|revenue_yoy| > 200% 视为可疑（少数高增长/反转标的需人工确认）

用法:
  equity_crosscheck.py [audit|self-test] [--db path] [--json out]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[4] / "outputs" / "equity_fundamental.sqlite"

# E11 合理区间：全年营收 / 半年营收。中位数实测 1.95；
# 下限放宽到 1.3（高增长股 H1 追平全年，如泡泡玛特），上限 3.5（H2 权重极高的季节性股）。
RATIO_LO, RATIO_HI = 1.3, 3.5
YOY_ABS_MAX = 2.0  # E13：|revenue_yoy| 上限（200%）

# 已人工核实的「合理高增长」标的（H1/FY 比值天然低于 1.3，非数据错误，2026-09-01 核实）：
# 泡泡玛特 0.94（IP 放量）、理想 0.95-1.10（初创放量）、小鹏 1.20、长实 1.16（结转波动）、
# 中海油 1.22（油价）、阜博 1.06-1.23（小市值高增）、药明生物 2020-21 1.27、百济 1.28（放量）。
KNOWN_GROWTH = {"hk09992", "hk02015", "hk09868", "hk01113", "hk00883",
                "hk03738", "hk02269", "hk06160"}


def run_audit(db: Path) -> dict:
    con = sqlite3.connect(db)
    out: dict[str, list] = {"e11_magnitude": [], "e12_unit_mix": [], "e13_yoy_outlier": []}

    # ---- E11 跨期量级 ----
    rows = con.execute(
        "SELECT h2.ticker, u.name_zh, h2.value, h1.value, h2.unit "
        "FROM company_facts h2 "
        "JOIN company_facts h1 ON h1.ticker=h2.ticker AND h1.fact_key='revenue' "
        "JOIN universe u ON u.ticker=h2.ticker "
        "WHERE h2.fact_key='revenue' AND h2.period LIKE '%H2' "
        "  AND h1.period LIKE '%H1' AND h1.value > 0 AND h2.value > 0 "
        "  AND (CAST(substr(h2.period,1,4) AS INT)*2 + (substr(h2.period,5)='H2')) "
        "      - (CAST(substr(h1.period,1,4) AS INT)*2 + (substr(h1.period,5)='H2')) IN (-1,0,1) "
        "ORDER BY (h2.value*1.0/h1.value)"
    ).fetchall()
    for tk, name, fy, h1, unit in rows:
        r = fy / h1
        if (r < RATIO_LO or r > RATIO_HI) and tk not in KNOWN_GROWTH:
            out["e11_magnitude"].append({
                "ticker": tk, "name_zh": name, "fy": fy, "h1": h1,
                "ratio": round(r, 3), "unit": unit})

    # ---- E12 单位一致性 ----
    for tk, key in con.execute(
            "SELECT ticker, fact_key FROM company_facts "
            "WHERE fact_key IN ('revenue','net_profit','net_profit_attributable','total_assets') "
            "GROUP BY ticker, fact_key HAVING COUNT(DISTINCT unit) > 1"):
        units = [r[0] for r in con.execute(
            "SELECT DISTINCT unit FROM company_facts WHERE ticker=? AND fact_key=?", (tk, key))]
        out["e12_unit_mix"].append({"ticker": tk, "fact_key": key, "units": units})

    # ---- E13 同比离群（去重：同一 (ticker,period) 多批次行只计一次）----
    for tk, period, val in con.execute(
            "SELECT DISTINCT ticker, period, ROUND(value,3) FROM derived_factors "
            "WHERE factor_key='revenue_yoy' AND value IS NOT NULL AND ABS(value) > ? "
            "ORDER BY ABS(value) DESC", (YOY_ABS_MAX,)):
        out["e13_yoy_outlier"].append({"ticker": tk, "period": period, "yoy": round(val, 3)})

    con.close()
    return out


def self_test() -> bool:
    """已知答案：构造 FY=200 / H1=100（ratio 2，应放行）；FY=200 / H1=1000（应告警）。"""
    import tempfile
    con = sqlite3.connect(":memory:")
    con.executescript("""
    CREATE TABLE company_facts (ticker TEXT, fact_key TEXT, period TEXT, freq TEXT,
        value REAL, unit TEXT, source TEXT, source_url TEXT, release_date TEXT,
        collected_at TEXT);
    CREATE TABLE derived_factors (ticker TEXT, factor_key TEXT, period TEXT, value REAL,
        unit TEXT, transform TEXT, transform_version TEXT, source TEXT);
    CREATE TABLE universe (ticker TEXT PRIMARY KEY, name_zh TEXT, name_en TEXT, sector TEXT,
        currency TEXT, fiscal_year_end TEXT, included INTEGER, reason TEXT, collected_at TEXT,
        shares_outstanding REAL);
    """)
    con.execute("INSERT INTO universe VALUES ('hkA','A','','','kCNY','12',1,'','',NULL)")
    con.execute("INSERT INTO universe VALUES ('hkB','B','','','kCNY','12',1,'','',NULL)")
    con.execute("INSERT INTO universe VALUES ('hkC','C','','','kCNY','12',1,'','',NULL)")
    # A：正常（FY 200 vs H1 100 → 2.0，放行）
    con.execute("INSERT INTO company_facts VALUES ('hkA','revenue','2024H2','A',200,'mCNY','s','','','')")
    con.execute("INSERT INTO company_facts VALUES ('hkA','revenue','2025H1','A',100,'mCNY','s','','','')")
    # B：异常（FY 200 vs H1 1000 → 0.2，告警）
    con.execute("INSERT INTO company_facts VALUES ('hkB','revenue','2024H2','A',200,'mCNY','s','','','')")
    con.execute("INSERT INTO company_facts VALUES ('hkB','revenue','2025H1','A',1000,'mCNY','s','','','')")
    # C：单位混合（mCNY vs mUSD，告警）
    con.execute("INSERT INTO company_facts VALUES ('hkC','revenue','2024H2','A',200,'mCNY','s','','','')")
    con.execute("INSERT INTO company_facts VALUES ('hkC','revenue','2025H1','A',100,'mUSD','s','','','')")
    con.execute("INSERT INTO derived_factors VALUES ('hkC','revenue_yoy','2024H2',5.0,'','','','s')")
    tmp = Path(tempfile.mkdtemp()) / "t.sqlite"
    con.commit()
    data = sqlite3.connect(tmp)
    con.backup(data)
    con.close()

    res = run_audit(tmp)
    checks = [
        ("E11 只告警异常标的", [r["ticker"] for r in res["e11_magnitude"]] == ["hkB"]),
        ("E12 单位混合告警", [r["ticker"] for r in res["e12_unit_mix"]] == ["hkC"]),
        ("E12 混入字段正确", (res["e12_unit_mix"][0]["fact_key"] if res["e12_unit_mix"] else None) == "revenue"),
        ("E13 同比离群告警", len(res["e13_yoy_outlier"]) == 1 and res["e13_yoy_outlier"][0]["ticker"] == "hkC"),
    ]
    ok = all(c for _, c in checks)
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print(f"[self-test] {'ALL PASS' if ok else 'FAILED'} ({sum(c for _, c in checks)}/{len(checks)})")
    try:
        tmp.unlink(missing_ok=True)
    except (PermissionError, OSError):
        pass  # Windows：sqlite 连接尚未释放，忽略即可（临时目录）
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", nargs="?", default="audit", choices=["audit", "self-test"])
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--json", default=None, help="结果落盘路径")
    args = ap.parse_args()
    if args.cmd == "self-test":
        raise SystemExit(0 if self_test() else 1)

    res = run_audit(Path(args.db))
    print(f"== E11 跨期量级（全年/半年 应≈2，区间 {RATIO_LO}~{RATIO_HI}）==")
    for r in res["e11_magnitude"]:
        print(f"  [WARN] {r['ticker']} {r['name_zh']:10s} FY={r['fy']:>12,.0f} "
              f"H1={r['h1']:>12,.0f} ratio={r['ratio']:.2f} {r['unit']}")
    print(f"== E12 单位/币种混合（同 ticker+字段 跨期 unit 不一致）==")
    for r in res["e12_unit_mix"]:
        print(f"  [WARN] {r['ticker']} {r['fact_key']} units={r['units']}")
    print(f"== E13 同比离群（|revenue_yoy| > {YOY_ABS_MAX:.0%}）==")
    for r in res["e13_yoy_outlier"]:
        print(f"  [WARN] {r['ticker']} {r['period']} yoy={r['yoy']:+.2%}")
    n = sum(len(v) for v in res.values())
    print(f"\n[crosscheck] {n} 条告警（E11={len(res['e11_magnitude'])} "
          f"E12={len(res['e12_unit_mix'])} E13={len(res['e13_yoy_outlier'])}）")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
             "ratio_band": [RATIO_LO, RATIO_HI], "results": res},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[crosscheck] 已落盘 {args.json}")


if __name__ == "__main__":
    main()
