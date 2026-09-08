#!/usr/bin/env python3
"""Measure the real coverage of the eastmoney capability layer.

WHY THIS EXISTS
---------------
A capability layer that fails on 17% of the market is fine — as long as you
KNOW it, and know *which* 17%. An unmeasured capability layer eventually gets
trusted on inputs where it silently doesn't work, and the agent writes
"数据缺失" instead of "this is a known gap".

First measurement (2026-09-08, n=24): 83% hit rate, and every miss was an
ST/退市 name. That is a *structural* boundary, not random flakiness.

Usage:
    python ashare_coverage_probe.py                 # n=200 from local pool
    python ashare_coverage_probe.py --n 500
    python ashare_coverage_probe.py --codes 600416,000651

Exit code 0 = probe completed (regardless of the rate); 1 = probe itself broke.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sqlite3
import ssl
import sys
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ssl._create_default_https_context = ssl._create_unverified_context

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://data.eastmoney.com/",
    "Accept": "*/*",
}
EM = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# Reports the workflow's 必答维度清单 actually depends on.
REPORTS = {
    "income": ("RPT_LICO_FN_CPD", "REPORTDATE"),
    "cashflow": ("RPT_DMSK_FN_CASHFLOW", "REPORT_DATE"),
    "balance": ("RPT_DMSK_FN_BALANCE", "REPORT_DATE"),
    "segments": ("RPT_F10_FN_MAINOP", "REPORT_DATE"),
}

# Default n is large enough that the 83% estimate stops moving at ±3pp.
DEFAULT_N = 200


def _candidate_dbs() -> list[str]:
    """Every outputs/*.sqlite on the way up.

    Two traps hit here, both worth remembering:
      1. Do NOT hard-code parents[N] — it breaks when the script moves.
      2. Do NOT stop at the *first* outputs/ — `my-deepseek-harness/outputs/`
         also exists (1 sqlite, no `code` column) and sits BELOW the real
         workspace root, so a first-match walk returns an empty pool.
    Scan all of them and union; harmless if some contain no codes.
    """
    found: list[str] = []
    p = Path(__file__).resolve().parent
    for _ in range(8):
        found.extend(glob.glob(str(p / "outputs" / "*.sqlite")))
        p = p.parent
    return sorted(set(found))


MAX_DB_BYTES = 400 * 1024 * 1024  # skip the 1.2GB quote DBs; distinct-code is slow there


def local_codes() -> list[str]:
    """Distinct 6-digit codes from the project's sqlite pool."""
    out: set[str] = set()
    for db in _candidate_dbs():
        try:
            if os.path.getsize(db) > MAX_DB_BYTES:
                continue
            con = sqlite3.connect(db)
            for (t,) in con.execute(
                    "select name from sqlite_master where type='table'"):
                cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
                if "code" in cols:
                    out |= {str(r[0]).zfill(6)
                            for r in con.execute(f"select distinct code from {t} limit 5000")}
            con.close()
        except Exception:
            continue
    return sorted(c for c in out if c.isdigit() and len(c) == 6)


def _quote(code: str) -> tuple[str, float]:
    """(name, volume) via the Tencent quote endpoint.

    volume == 0 is the reliable "no longer traded" signal — better than
    pattern-matching the name. Verified 2026-09-08: 600068 葛洲坝 /
    600723 首商股份 / 900935 阳晨B股 / 600102 莱钢股份 all carry normal-looking
    names (no ST/退) but return volume 0, because they were absorbed or
    delisted. A live comparison point, 600416, returns volume 71456.
    """
    try:
        pfx = "sh" if code[0] in "69" else ("sz" if code[0] in "03" else "sh")
        req = urllib.request.Request(
            f"https://qt.gtimg.cn/q={pfx}{code}", headers=UA)
        with urllib.request.urlopen(req, timeout=8) as resp:
            parts = resp.read().decode("gbk", "ignore").split("~")
        if len(parts) <= 10 or not parts[1].strip():
            return "", 0.0
        try:
            vol = float(parts[6]) if len(parts) > 6 else 0.0
        except ValueError:
            vol = 0.0
        return parts[1].strip(), vol
    except Exception:
        return "", 0.0


def _hit(code: str, report: str, date_col: str) -> bool | None:
    q = (f"columns=ALL&filter=(SECURITY_CODE%3D%22{code}%22)&pageSize=2"
         f"&sortColumns={date_col}&sortTypes=-1&source=WEB&client=WEB"
         f"&reportName={report}")
    try:
        req = urllib.request.Request(f"{EM}?{q}", headers=UA)
        with urllib.request.urlopen(req, timeout=12) as resp:
            payload = json.loads(resp.read().decode("utf-8", "ignore"))
        return bool(((payload or {}).get("result") or {}).get("data"))
    except Exception:
        return None  # network-ish, not "no data"


def probe_one(code: str, reports: dict) -> dict:
    res = {k: _hit(code, rn, dc) for k, (rn, dc) in reports.items()}
    res["code"] = code
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=DEFAULT_N)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--codes", help="comma-separated override")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--json", help="write raw result to this path")
    a = ap.parse_args()

    if a.codes:
        codes = [c.strip().zfill(6) for c in a.codes.split(",") if c.strip()]
    else:
        pool = local_codes()
        if not pool:
            print("[FAIL] 本地代码池为空，无法抽样")
            return 1
        random.seed(a.seed)
        codes = random.sample(pool, min(a.n, len(pool)))

    selected = {k: v for k, v in REPORTS.items()}
    with ThreadPoolExecutor(a.workers) as ex:
        rows = list(ex.map(lambda c: probe_one(c, selected), codes))

    print(f"\n=== 能力层覆盖率探测 · n={len(codes)} ===")
    per_report = {}
    for key in selected:
        ok = sum(1 for r in rows if r.get(key) is True)
        empty = sum(1 for r in rows if r.get(key) is False)
        err = sum(1 for r in rows if r.get(key) is None)
        rate = ok / len(rows) * 100
        per_report[key] = {"ok": ok, "empty": empty, "err": err, "rate": round(rate, 1)}
        print(f"  {key:9s} 命中 {ok:4d} / 空 {empty:3d} / 异常 {err:3d}  => {rate:5.1f}%")

    # Which codes miss *everything*? Those are the structural boundary.
    all_miss = [r["code"] for r in rows
                if all(r.get(k) is False for k in selected)]
    any_miss = [r["code"] for r in rows
                if any(r.get(k) is False for k in selected)]
    print(f"\n  全部维度都查不到: {len(all_miss)} 只 ({len(all_miss)/len(rows)*100:.1f}%)")
    print(f"  至少一个维度查不到: {len(any_miss)} 只 ({len(any_miss)/len(rows)*100:.1f}%)")

    # Classify the misses: are they all "no longer traded" (structural), or is
    # something genuinely broken for live stocks?
    dead, alive_missing = [], []
    for c in all_miss:
        nm, vol = _quote(c)
        dead.append(f"{c}({nm or '无名称'})") if (
            vol <= 0 or "ST" in nm.upper() or "退" in nm) else alive_missing.append(
            f"{c}({nm or '无名称'}, vol={vol:g})")
    print(f"\n  --- 全部维度失效的样本分类（{len(all_miss)} 只）---")
    print(f"  已停止交易/ST/退市（结构性边界）: {len(dead)} 只")
    print(f"    → {dead[:14]}")
    if alive_missing:
        print(f"  ⚠ 仍在交易却查不到（真问题，需排查）: {len(alive_missing)} 只")
        print(f"    → {alive_missing[:14]}")
    else:
        print("  ✓ 仍在交易却查不到的: 0 只 —— 对正常交易股票覆盖完整")

    if a.json:
        Path(a.json).write_text(json.dumps(
            {"n": len(codes), "per_report": per_report,
             "all_miss": all_miss, "any_miss": any_miss,
             "not_traded": dead, "alive_but_missing": alive_missing},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  原始结果已写入: {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
