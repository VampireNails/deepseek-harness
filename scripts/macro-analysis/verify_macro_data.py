#!/usr/bin/env python3
"""Independent Proof gate for the macro-analysis Agent."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
from pathlib import Path

DB_NAME = "macro_indicators.sqlite"
REQUIRED = {"cpi_yoy", "ppi_yoy", "manufacturing_pmi", "nonfarm_payroll_level", "nonfarm_payroll_change", "unemployment_rate"}


def norm_period(value: object) -> str:
    text = str(value or "").strip()
    m = re.search(r"(20\d{2})\D{0,5}(1[0-2]|0?[1-9])", text)
    return f"{m.group(1)}-{int(m.group(2)):02d}" if m else text[:10]


def raw_period(raw: dict) -> str | None:
    if raw.get("observation_date"):
        return norm_period(raw["observation_date"])
    if raw.get("TIME"):
        return norm_period(raw["TIME"])
    if raw.get("REPORT_DATE"):
        return norm_period(raw["REPORT_DATE"])
    if raw.get("year") and raw.get("period"):
        return norm_period(f"{raw['year']}-{str(raw['period']).replace('M', '')}")
    if raw.get("period"):
        return norm_period(raw["period"])
    return None


# --------------------------------------------------------------------------
# 校验智慧飞轮（轮 A）：失败案例库 + 模式标签 + 历史命中。
# 借鉴 Great Expectations 的「失败留档(run history)」思路，用本地 sqlite 实现
# 迷你版，不引入 GE 重依赖。失败案例写入独立库 outputs/macro_verify.sqlite，
# 绝不回写数据层（原始库 / 清洗库）。
# --------------------------------------------------------------------------

def fail_tag(message: str) -> str:
    """把校验失败文案归类为稳定模式标签，用于跨日期的「已知坏法」命中。"""
    m = message.lower()
    if "table missing" in m:
        return "missing_table"
    if "has no rows" in m:
        return "empty_db"
    if "empty indicator/value" in m:
        return "bad_row"
    if "required indicators missing" in m:
        return "missing_indicator"
    if "no collection_checks" in m:
        return "missing_checks"
    if "neither bls nor fred" in m:
        return "us_source_down"
    if "raw/period mismatch" in m:
        return "period_mismatch"
    if "duplicate vintage-key" in m:
        return "dup_vintage"
    if "revision rows without original" in m:
        return "rev_no_original"
    if "invalid raw_json" in m:
        return "bad_raw_json"
    if "macro_collection_report.md" in m and "not found" in m:
        return "report_missing"
    if "!=" in m:
        return "report_mismatch"
    return "other"


def record_failures(root: Path, date: str, failures: list[str]) -> list[dict]:
    """把本批失败 append 到独立失败案例库，并返回每条的「是否历史已知模式」。"""
    if not failures:
        return []
    verify_db = root / "macro_verify.sqlite"
    checked_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    conn = sqlite3.connect(str(verify_db))
    catalog: list[dict] = []
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS verify_failures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL, date TEXT NOT NULL,
            fail_tag TEXT NOT NULL, message TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vf_tag ON verify_failures(fail_tag)")
        for msg in failures:
            tag = fail_tag(msg)
            prior = conn.execute(
                "SELECT COUNT(*), MIN(date) FROM verify_failures WHERE fail_tag=?", (tag,)).fetchone()
            conn.execute("INSERT INTO verify_failures(checked_at,date,fail_tag,message) VALUES(?,?,?,?)",
                         (checked_at, date, tag, msg))
            catalog.append({
                "tag": tag, "message": msg,
                "known": prior[0] > 0 and (prior[1] or date) < date,
                "prior_count": prior[0], "first_seen": prior[1],
            })
        conn.commit()
    finally:
        conn.close()
    return catalog


def check(db_path: Path, date: str, strict: bool) -> dict:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    failures: list[str] = []
    warnings: list[str] = []
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in {"macro_indicators", "collection_checks", "source_registry"}:
            if table not in tables:
                failures.append(f"table missing: {table}")
        rows = conn.execute("SELECT indicator_name,country,period,value,value_type,collected_at,source,raw_json,is_revision,original_value FROM macro_indicators").fetchall()
        total = len(rows)
        indicators = {r[0] for r in rows}
        missing = sorted(REQUIRED - indicators)
        bad = sum(1 for r in rows if not r[0] or not str(r[0]).strip() or r[3] is None)
        if total == 0: failures.append("macro_indicators has no rows")
        if bad: failures.append(f"rows with empty indicator/value: {bad}")
        if missing: failures.append("required indicators missing: " + ", ".join(missing))

        latest = conn.execute("SELECT MAX(checked_at) FROM collection_checks WHERE substr(checked_at,1,10)=?", (date,)).fetchone()[0] if "collection_checks" in tables else None
        checks = conn.execute("SELECT source,status,dataset,rows_seen,detail FROM collection_checks WHERE checked_at=? ORDER BY id", (latest,)).fetchall() if latest else []
        if not checks: failures.append("no collection_checks for requested date")
        bls_ok = any(r[0] == "bls" and r[1] == "ok" for r in checks)
        fred_ok = any(r[0] == "fred_csv" and r[1] == "ok" for r in checks)
        if not bls_ok and not fred_ok: failures.append("neither BLS nor FRED fallback succeeded in latest batch")
        if not bls_ok and fred_ok: warnings.append("BLS unavailable; FRED supplied US employment data")

        revision_count = sum(1 for r in rows if r[8] == 1)
        sources = conn.execute("SELECT source,COUNT(*),MIN(collected_at),MAX(collected_at) FROM macro_indicators GROUP BY source ORDER BY source").fetchall()

        if strict:
            mismatches = 0
            for indicator, country, period, value, value_type, collected_at, source, raw_json, is_revision, original in rows:
                try: raw = json.loads(raw_json or "{}")
                except json.JSONDecodeError: failures.append(f"invalid raw_json for {indicator}/{period}"); continue
                rp = raw_period(raw)
                if rp and norm_period(period) != rp: mismatches += 1
            if mismatches: failures.append(f"{mismatches} rows with raw/period mismatch")
            dup = conn.execute("SELECT COUNT(*) FROM (SELECT 1 FROM macro_indicators GROUP BY indicator_name,country,period,value_type,collected_at,source HAVING COUNT(*)>1)").fetchone()[0]
            if dup: failures.append(f"{dup} duplicate vintage-key groups")
            bad_rev = conn.execute("SELECT COUNT(*) FROM macro_indicators WHERE is_revision=1 AND original_value IS NULL").fetchone()[0]
            if bad_rev: failures.append(f"{bad_rev} revision rows without original_value")
            report = db_path.parent / "macro_collection_report.md"
            if not report.exists(): failures.append("macro_collection_report.md not found")
            else:
                text = report.read_text(encoding="utf-8")
                m_rows = re.search(r"累计行数[：:]\s*`?(\d+)", text)
                m_rev = re.search(r"修订行数[：:]\s*`?(\d+)", text)
                if not m_rows: failures.append("report does not expose cumulative row count")
                elif int(m_rows.group(1)) != total: failures.append(f"report rows {m_rows.group(1)} != db rows {total}")
                if not m_rev: failures.append("report does not expose revision count")
                elif int(m_rev.group(1)) != revision_count: failures.append(f"report revisions {m_rev.group(1)} != db revisions {revision_count}")

        failure_catalog = record_failures(db_path.parent.parent, date, failures)
        result = {"ok": not failures, "date": date, "db": str(db_path), "report": str(db_path.parent / "macro_collection_report.md"), "rows": total, "bad_rows": bad, "indicators": sorted(indicators), "missing_required": missing, "revision_rows": revision_count, "checks": [list(r) for r in checks], "sources": [list(r) for r in sources], "warnings": warnings, "failures": failures, "failure_catalog": failure_catalog, "strict": strict}
        return result
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 派生层校验（飞轮③延伸）：校验 clean 库的 derived 行与一致性对账。
# 借鉴 Great Expectations / Soda 的「列级断言」思路：派生值可复现、版本化、
# 区间合理；观测值 vs 公式派生值的一致性偏差受控。
# --------------------------------------------------------------------------

def check_clean(clean_db: Path, strict: bool) -> dict:
    uri = f"file:{clean_db.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    failures: list[str] = []
    warnings: list[str] = []
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "clean_series" not in tables:
            return {"ok": True, "skipped": "no clean_series table", "failures": [], "warnings": []}
        derived = conn.execute(
            "SELECT indicator,country,period,value,transform,transform_version,derived_from "
            "FROM clean_series WHERE layer='derived'").fetchall()
        for ind, cty, per, val, tr, ver, df in derived:
            if ver is None or not str(ver).strip():
                failures.append(f"derived row {ind}/{cty}/{per} missing transform_version")
            if df is None or not str(df).strip():
                failures.append(f"derived row {ind}/{cty}/{per} missing derived_from")
            if val is None or val != val:
                failures.append(f"derived row {ind}/{cty}/{per} has NaN/None value")
                continue
            # 区间断言：同比/环比(%) ∈ [-100,100]；变化量(千人) ∈ [-10000,10000]
            lo, hi = (-100.0, 100.0) if tr != "diff_level" else (-10000.0, 10000.0)
            if not (lo <= val <= hi):
                failures.append(f"derived row {ind}/{cty}/{per} value {val} out of [{lo},{hi}]")
        checks = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN consistent=0 THEN 1 ELSE 0 END), "
            "MAX(abs_delta) FROM derived_checks").fetchone()
        total = checks[0] or 0
        inconsistent = checks[1] or 0
        max_abs = checks[2]
        if total and inconsistent:
            msg = f"{inconsistent}/{total} derived_checks inconsistent (max |delta|={max_abs})"
            if strict:
                failures.append(msg + " — 出版值与方法派生值不一致，需人工核查")
            else:
                warnings.append(msg)
        return {
            "ok": not failures, "derived_rows": len(derived),
            "derived_checks": total, "derived_inconsistent": inconsistent,
            "max_abs_delta": max_abs, "failures": failures, "warnings": warnings,
        }
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--output-root", default=None); ap.add_argument("--date", required=True); ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(); root = Path(args.output_root or os.environ.get("MACRO_OUTPUT_ROOT") or Path(__file__).resolve().parents[1] / "outputs"); db = root / args.date / DB_NAME
    if not db.exists(): print(json.dumps({"ok": False, "error": f"db not found: {db}"}, ensure_ascii=False)); return 1
    result = check(db, args.date, args.strict)
    clean_db = Path(os.environ.get("MACRO_CLEAN_DB") or str(root / "macro_clean.sqlite"))
    if clean_db.exists():
        clean_res = check_clean(clean_db, args.strict)
        result["failures"].extend(clean_res["failures"])
        result["warnings"].extend(clean_res["warnings"])
        result["clean_layer"] = clean_res
        result["ok"] = result["ok"] and clean_res["ok"]
    print(json.dumps(result, ensure_ascii=False, indent=2)); return 0 if result["ok"] else 1


if __name__ == "__main__": raise SystemExit(main())
