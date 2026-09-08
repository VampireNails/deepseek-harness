#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
CN 宏观长历史回补（第三方结构化源 eastmoney）

为什么需要
----------
collect_macro_data.py 里 eastmoney 请求写死 pageSize=24，导致 CN 指标历史被
**人为截断**（PMI/CPI ~24 期、PPI ~25 期），OOS 只有 6~12 个，任何显著性检验
都落进"功效不足"。实测 eastmoney 实际提供：
    RPT_ECONOMY_PMI  count=224  2008-01 ~ 2026-08
    RPT_ECONOMY_PPI  count=247  2006-01 ~ 2026-07
    RPT_ECONOMY_CPI  count=223  2008-01 ~ 2026-07
    RPT_ECONOMY_GDP  count=82   季度
本脚本把这段历史取回，OOS 可提升到 150+。

源标注纪律（SOP 三飞轮：严禁第三方源冒充官方一手）
-------------------------------------------------
eastmoney = 第三方结构化源，authority=third_party。所有行 source='eastmoney'
明确落库，绝不写成 nbs。
与 NBS 官方一手（macro_vintage 快照，2025-01+ 重叠期）做交叉校验，差异单独出
报告——第三方源只有验过才准用于延长历史，不静默混用。

口径坑（本脚本已修）
--------------------
1. **REPORT_DATE 不是发布日**：eastmoney 的 REPORT_DATE 是"期始日"
   （PMI 2026年08月份 -> 2026-08-01，而实际发布在 8/31）。现有采集器把它当
   release_date 写入，会让任何 as-of 发布日过滤失真。本脚本 release_date=NULL，
   真实发布日未知就是未知。
2. 值为"截至今日的终值"(final)，非首发值 → **修订偏置未消除**，只能做终值口径
   回测，不能冒充实时 nowcast。
3. GDP 季度标签 "2026年第1-2季度" -> period '2026-06'（季末月），便于月网格对齐。

产物
----
outputs/macro_cn_long.sqlite  (表 macro_indicators，与每日快照同构)
outputs/2026-09-03/macro_backfill_cn_report.md
"""
import sqlite3, json, re, os, sys, time
import urllib.request, urllib.parse
from pathlib import Path
from datetime import datetime, timezone

_HERE = Path(__file__).resolve().parent
ROOT = _HERE
while not (ROOT / "outputs").is_dir() and str(ROOT) != str(ROOT.parent):
    ROOT = ROOT.parent
OUTDIR = ROOT / "outputs" / "2026-09-03"
OUTDIR.mkdir(parents=True, exist_ok=True)
DB = ROOT / "outputs" / "macro_cn_long.sqlite"
VINTAGE = ROOT / "outputs" / "macro_vintage.sqlite"

EASTMONEY_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
PROXY = os.environ.get("MACRO_PROXY_URL") or "http://127.0.0.1:10809"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# (report, [(indicator_name, field)], freq)
SERIES = [
    ("RPT_ECONOMY_PMI", [
        ("manufacturing_pmi", "MAKE_INDEX"),
        ("nonmanufacturing_pmi", "NMAKE_INDEX"),
        ("manufacturing_pmi_yoy", "MAKE_SAME"),
        ("nonmanufacturing_pmi_yoy", "NMAKE_SAME"),
    ], "M"),
    ("RPT_ECONOMY_PPI", [
        ("ppi_yoy", "BASE_SAME"),
        ("ppi_base", "BASE"),
        ("ppi_accumulated", "BASE_ACCUMULATE"),
    ], "M"),
    ("RPT_ECONOMY_CPI", [
        ("cpi_yoy", "NATIONAL_SAME"),
        ("cpi_mom", "NATIONAL_SEQUENTIAL"),
        ("cpi_base", "NATIONAL_BASE"),
        ("cpi_accumulated", "NATIONAL_ACCUMULATE"),
    ], "M"),
    ("RPT_ECONOMY_GDP", [
        ("gdp_yoy", "SUM_SAME"),
        ("gdp_primary_yoy", "FIRST_SAME"),
        ("gdp_secondary_yoy", "SECOND_SAME"),
        ("gdp_tertiary_yoy", "THIRD_SAME"),
    ], "Q"),
]

MIN_PERIOD = "2000-01"


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))


def normalize_period(t):
    """'2026年08月份'->'2026-08'; '2026年第1-2季度'->'2026-06'; '2026年第1季度'->'2026-03'"""
    if not t:
        return None
    s = str(t).strip()
    m = re.search(r"(\d{4})\D{1,4}(\d{1,2})\D*月", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.search(r"(\d{4})\D*第?\s*([1-4])\s*-?\s*([1-4])?\s*季度", s)
    if m:
        q = int(m.group(3) or m.group(2))
        return f"{m.group(1)}-{q * 3:02d}"
    m = re.search(r"(\d{4})-(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    return None


def to_float(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "--", "null", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_report(op, report, fields, page_size=500, retries=3):
    params = {
        "reportName": report,
        "columns": "REPORT_DATE,TIME," + ",".join(fields),
        "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": "REPORT_DATE", "sortTypes": "-1",
        "source": "WEB", "client": "WEB",
    }
    url = EASTMONEY_URL + "?" + urllib.parse.urlencode(params)
    last = None
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            raw = op.open(req, timeout=60).read().decode("utf-8", "replace")
            d = json.loads(raw)
            res = d.get("result") or {}
            return res.get("data") or [], res.get("count")
        except Exception as exc:
            last = exc
            time.sleep(2 + 2 * k)
    raise RuntimeError(f"{report}: {type(last).__name__}: {last}")


def init_db():
    if DB.exists():
        DB.unlink()
    c = sqlite3.connect(str(DB))
    c.execute("""CREATE TABLE macro_indicators (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        indicator_name TEXT NOT NULL, country TEXT NOT NULL, period TEXT NOT NULL,
        value REAL, value_type TEXT, release_date TEXT, collected_at TEXT NOT NULL,
        source TEXT NOT NULL, source_series TEXT, raw_json TEXT,
        is_revision INTEGER DEFAULT 0, original_value REAL)""")
    c.execute("""CREATE UNIQUE INDEX ux_key ON macro_indicators
        (indicator_name,country,period,value_type,source,collected_at)""")
    c.execute("""CREATE TABLE source_registry (
        source TEXT PRIMARY KEY, authority TEXT, endpoint TEXT, note TEXT)""")
    c.execute("INSERT INTO source_registry VALUES (?,?,?,?)",
              ("eastmoney", "第三方结构化源（东方财富宏观数据中心）", EASTMONEY_URL,
               "CN 长历史回补源。REPORT_DATE=期始日非发布日，故 release_date 置 NULL；"
               "值为今日终值，修订偏置未消除；authority=third_party，不得冒充 NBS 官方一手。"))
    c.commit()
    return c


def crosscheck_vs_nbs(built):
    """与 NBS 官方一手在重叠期比对（第三方源须先验后用）"""
    if not VINTAGE.exists():
        return {"status": "skipped", "reason": "macro_vintage.sqlite 不存在"}
    off = sqlite3.connect(str(VINTAGE))
    nbs = {}
    try:
        for ind, per, val in off.execute(
                "SELECT indicator,period,value FROM latest WHERE country='CN' AND source='nbs'"):
            nbs.setdefault(ind, {})[per] = val
    except sqlite3.Error:
        return {"status": "skipped", "reason": "latest 表不可用"}
    finally:
        off.close()

    rows = []
    for ind, per, val in built:
        if ind in nbs and per in nbs[ind]:
            o = nbs[ind][per]
            if o is not None and val is not None:
                rows.append((ind, per, o, val, abs(o - val)))
    if not rows:
        return {"status": "no_overlap"}
    by_ind = {}
    for ind, per, o, v, d in rows:
        b = by_ind.setdefault(ind, dict(n=0, maxd=0.0, meand=0.0, worst=None))
        b["n"] += 1
        b["meand"] += d
        if d > b["maxd"]:
            b["maxd"] = d
            b["worst"] = (per, o, v)
    for b in by_ind.values():
        b["meand"] = round(b["meand"] / b["n"], 6)
        b["maxd"] = round(b["maxd"], 6)
    return {"status": "ok", "n_compared": len(rows), "by_indicator": by_ind}


def main():
    op = _opener()
    conn = init_db()
    collected_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    built = []          # (indicator, period, value) 用于交叉校验
    summary = []
    total = 0
    for report, fields, freq in SERIES:
        field_names = [f for _, f in fields]
        try:
            data, count = fetch_report(op, report, field_names)
        except Exception as exc:
            print(f"[ERR ] {report}: {exc}")
            summary.append(dict(report=report, status="error", detail=str(exc)))
            continue
        n_ins = 0
        periods = []
        for row in data:
            per = normalize_period(row.get("TIME") or row.get("REPORT_DATE"))
            if not per or per < MIN_PERIOD:
                continue
            periods.append(per)
            for ind, fld in fields:
                v = to_float(row.get(fld))
                if v is None:
                    continue
                try:
                    conn.execute("""INSERT INTO macro_indicators
                        (indicator_name,country,period,value,value_type,release_date,
                         collected_at,source,source_series,raw_json,is_revision,original_value)
                        VALUES(?,?,?,?,?,?,?,?,?,?,0,NULL)""",
                                 (ind, "CN", per, v, "reported", None,
                                  collected_at, "eastmoney", report,
                                  json.dumps({k: str(x)[:40] for k, x in row.items()},
                                             ensure_ascii=False, separators=(",", ":"))))
                    n_ins += 1
                    built.append((ind, per, v))
                except sqlite3.IntegrityError:
                    pass
        conn.commit()
        total += n_ins
        periods = sorted(set(periods))
        summary.append(dict(report=report, freq=freq, api_count=count, inserted=n_ins,
                            n_periods=len(periods),
                            p0=periods[0] if periods else None, p1=periods[-1] if periods else None,
                            status="ok" if n_ins else "empty"))
        print(f"[OK  ] {report:20s} api_count={count} inserted={n_ins} "
              f"periods={len(periods)} {periods[0] if periods else '-'}..{periods[-1] if periods else '-'}")
    conn.close()

    xc = crosscheck_vs_nbs(built)
    print(f"\n[crosscheck vs NBS] {xc.get('status')} "
          f"{('n=' + str(xc['n_compared'])) if xc.get('n_compared') else ''}")
    for ind, b in (xc.get("by_indicator") or {}).items():
        print(f"  {ind:26s} n={b['n']:3d} mean|diff|={b['meand']:.4f} max|diff|={b['maxd']:.4f} worst={b['worst']}")

    report_md = OUTDIR / "macro_backfill_cn_report.md"
    with open(report_md, "w", encoding="utf-8") as f:
        f.write("# CN 宏观长历史回补报告\n\n")
        f.write(f"- 回补时点：`{collected_at}`\n")
        f.write(f"- 落库：`outputs/macro_cn_long.sqlite`（`macro_indicators`，与每日快照同构）\n")
        f.write(f"- 源：**eastmoney（第三方结构化源）**，authority=third_party，**非** NBS 官方一手\n")
        f.write(f"- 网络路由：代理 `{PROXY.split('@')[-1]}`\n")
        f.write(f"- 总插入行：`{total}`\n\n")
        f.write("## 回补结果\n\n| 报表 | 频率 | API 总数 | 插入行 | 期数 | 起 | 止 | 状态 |\n|---|---|---|---|---|---|---|---|\n")
        for s in summary:
            f.write(f"| `{s['report']}` | {s.get('freq','')} | {s.get('api_count','')} | {s.get('inserted','')} "
                    f"| {s.get('n_periods','')} | {s.get('p0','')} | {s.get('p1','')} | {s.get('status','')} |\n")
        f.write("\n## 与 NBS 官方一手交叉校验\n\n")
        f.write(f"状态：`{xc.get('status')}`")
        if xc.get("n_compared"):
            f.write(f"，比对行数 **{xc['n_compared']}**\n\n")
            f.write("| 指标 | 重叠期数 | 平均\\|差\\| | 最大\\|差\\| | 最差期(NBS,EM) |\n|---|---|---|---|---|\n")
            for ind, b in xc["by_indicator"].items():
                f.write(f"| `{ind}` | {b['n']} | {b['meand']:.4f} | {b['maxd']:.4f} | {b['worst']} |\n")
        else:
            f.write("\n")
        f.write("\n## 口径与限制（不得忽略）\n\n")
        f.write("1. **REPORT_DATE ≠ 发布日**：eastmoney 的 `REPORT_DATE` 为期始日"
                "（PMI 2026年08月份 → 2026-08-01，实际发布在 8/31）。本脚本 `release_date` 一律置 "
                "**NULL**，不伪造发布日。（现有 `collect_macro_data.py` 把它当发布日写入，属待修 bug。）\n")
        f.write("2. **值为今日终值**：非首发值，修订偏置未消除 → 只能做终值口径回测，"
                "不能冒充实时 nowcast。\n")
        f.write("3. **第三方源**：与 NBS 重叠期已交叉校验（见上表）；历史段（NBS 未覆盖）"
                "无官方一手可核对，属信任外推。\n")
        f.write("4. GDP 季度标签映射为季末月（第1-2季度→06），便于月网格对齐。\n")
    print(f"\nWROTE {DB}")
    print(f"WROTE {report_md}")
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
