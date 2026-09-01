#!/usr/bin/env python3
"""Fixed macro collector: sources -> checks -> append-only vintage DB -> report."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

EASTMONEY_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
UA = "macro-analysis-agent/1.0 (+vintage-collector)"
FRED_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DEFAULT_PROXY_CONFIG = Path(__file__).with_name("macro_proxy.env")

CHINA_SOURCES = {
    "china_cpi": ("RPT_ECONOMY_CPI", [("cpi_yoy", "NATIONAL_SAME"), ("cpi_base", "NATIONAL_BASE"), ("cpi_mom", "NATIONAL_SEQUENTIAL")]),
    "china_ppi": ("RPT_ECONOMY_PPI", [("ppi_yoy", "BASE_SAME"), ("ppi_base", "BASE"), ("ppi_accumulated", "BASE_ACCUMULATE")]),
    "china_pmi": ("RPT_ECONOMY_PMI", [("manufacturing_pmi", "MAKE_INDEX"), ("nonmanufacturing_pmi", "NMAKE_INDEX")]),
}
BLS_SERIES = {
    "nonfarm_payroll_level": ("CES0000000001", "thousand persons, seasonally adjusted"),
    "unemployment_rate": ("LNS14000000", "percent, seasonally adjusted"),
}

_NETWORK_OPENER: urllib.request.OpenerDirector | None = None
_NETWORK_ROUTE = "direct"


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _proxy_candidate() -> tuple[str | None, str]:
    config = Path(os.environ.get("MACRO_PROXY_CONFIG", str(DEFAULT_PROXY_CONFIG)))
    file_values = _read_env_file(config)
    if os.environ.get("MACRO_PROXY_URL"):
        return os.environ["MACRO_PROXY_URL"], "MACRO_PROXY_URL"
    if file_values.get("MACRO_PROXY_URL"):
        return file_values["MACRO_PROXY_URL"], str(config)
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        if os.environ.get(key):
            return os.environ[key], key
    return None, "direct"


def _proxy_label(proxy_url: str | None, source: str) -> str:
    if not proxy_url:
        return "direct"
    parsed = urllib.parse.urlsplit(proxy_url)
    host = parsed.hostname or "unknown"
    port = f":{parsed.port}" if parsed.port else ""
    return f"proxy://{host}{port} ({source})"


def configure_network() -> str:
    global _NETWORK_OPENER, _NETWORK_ROUTE
    proxy_url, source = _proxy_candidate()
    if not proxy_url:
        _NETWORK_OPENER = urllib.request.build_opener()
        _NETWORK_ROUTE = "direct"
        return _NETWORK_ROUTE
    parsed = urllib.parse.urlsplit(proxy_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("仅支持 http(s)://host:port 代理；SOCKS 必须转换为 HTTP 监听端口")
    _NETWORK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    _NETWORK_ROUTE = _proxy_label(proxy_url, source)
    return _NETWORK_ROUTE


def safe_error(exc: Exception) -> str:
    text = str(exc)
    return re.sub(r"(https?://)([^/@\s]+):([^/@\s]+)@", r"\1***:***@", text)


def _open(req: urllib.request.Request, timeout: int):
    return (_NETWORK_OPENER or urllib.request.build_opener()).open(req, timeout=timeout)


def http_json(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None,
              timeout: int = 45, retries: int = 2) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            with _open(req, timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{type(last).__name__}: {safe_error(last)}")


def http_bytes(url: str, timeout: int = 90) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": FRED_UA, "Accept": "text/csv,*/*", "Connection": "close"})
    with _open(req, timeout) as response:
        return response.read()


def to_float(value: Any) -> float | None:
    if value is None or str(value).strip() in {"", ".", "null", "None"}:
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def normalize_period(value: Any) -> str:
    text = str(value or "").strip()
    m = re.search(r"(20\d{2})\D{0,5}(1[0-2]|0?[1-9])", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    return text[:10]


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS macro_indicators (
      id INTEGER PRIMARY KEY AUTOINCREMENT, indicator_name TEXT NOT NULL,
      country TEXT NOT NULL, period TEXT NOT NULL, value REAL,
      value_type TEXT NOT NULL, release_date TEXT, collected_at TEXT NOT NULL,
      source TEXT NOT NULL, source_series TEXT, raw_json TEXT,
      is_revision INTEGER NOT NULL DEFAULT 0, original_value REAL
    );
    CREATE TABLE IF NOT EXISTS collection_checks (
      id INTEGER PRIMARY KEY AUTOINCREMENT, checked_at TEXT NOT NULL,
      source TEXT NOT NULL, dataset TEXT NOT NULL, status TEXT NOT NULL,
      rows_seen INTEGER NOT NULL DEFAULT 0, detail TEXT
    );
    CREATE TABLE IF NOT EXISTS source_registry (
      source TEXT PRIMARY KEY, authority TEXT NOT NULL, endpoint TEXT NOT NULL,
      access_mode TEXT NOT NULL, attribution TEXT NOT NULL, priority INTEGER NOT NULL,
      active INTEGER NOT NULL DEFAULT 1
    );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_macro_key ON macro_indicators(indicator_name, country, period, value_type, source)")
    conn.executemany(
        "INSERT OR IGNORE INTO source_registry VALUES(?,?,?,?,?,?,?)",
        [("nbs", "国家统计局 NBS", "https://data.stats.gov.cn", "GET", "中国官方一手源", 0, 1),
         ("eastmoney", "东方财富宏观数据中心", EASTMONEY_URL, "GET", "第三方结构化源", 2, 1),
         ("bls", "美国劳工统计局 BLS", BLS_URL, "POST", "美国官方一手源", 1, 1),
         ("fred_csv", "圣路易斯联储 FRED（数据归属 BLS）", FRED_CSV_URL, "GET", "官方二次发布", 2, 1)],
    )
    # Do not rewrite legacy rows in place: older databases may contain both
    # localized and normalized period keys under the same vintage key. New
    # observations are normalized on insert; historical rows remain immutable.
    conn.commit()


def log_check(conn: sqlite3.Connection, checked_at: str, source: str, dataset: str,
              status: str, rows_seen: int, detail: str) -> None:
    conn.execute("INSERT INTO collection_checks(checked_at,source,dataset,status,rows_seen,detail) VALUES(?,?,?,?,?,?)",
                 (checked_at, source, dataset, status, rows_seen, detail[:2000]))
    conn.commit()


def insert_vintage(conn: sqlite3.Connection, *, indicator: str, country: str, period: str,
                   value: float | None, value_type: str, release_date: str | None,
                   collected_at: str, source: str, source_series: str, raw: Any) -> bool:
    if value is None:
        return False
    period = normalize_period(period)
    existing = conn.execute(
        "SELECT value, original_value FROM macro_indicators WHERE indicator_name=? AND country=? AND period=? AND value_type=? AND source=? ORDER BY id LIMIT 1",
        (indicator, country, period, value_type, source)).fetchone()
    original = existing[1] if existing and existing[1] is not None else (existing[0] if existing else value)
    # A source can return duplicate rows for the same period in one response.
    # Any existing same-batch key is idempotent, even if the duplicate payload
    # differs; revisions are only allowed from a later collected_at.
    same_batch = conn.execute(
        "SELECT 1 FROM macro_indicators WHERE indicator_name=? AND country=? AND period=? AND value_type=? AND source=? AND collected_at=? LIMIT 1",
        (indicator, country, period, value_type, source, collected_at)).fetchone()
    if same_batch:
        return False
    is_revision = 1 if existing and existing[0] != value else 0
    try:
        conn.execute("""INSERT INTO macro_indicators
          (indicator_name,country,period,value,value_type,release_date,collected_at,source,source_series,raw_json,is_revision,original_value)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
          (indicator, country, period, value, value_type, release_date, collected_at, source, source_series,
           json.dumps(raw, ensure_ascii=False, separators=(",", ":")), is_revision, original))
    except sqlite3.IntegrityError:
        # A legacy database may already contain the same vintage key while the
        # read-before-write check is racing another source row. The unique
        # index is authoritative: treat that key as idempotent, not as a
        # source-level collection failure.
        return False
    return True


def collect_eastmoney(conn: sqlite3.Connection, collected_at: str) -> tuple[int, list[str]]:
    inserted, errors = 0, []
    for dataset, (report, fields) in CHINA_SOURCES.items():
        try:
            params = {"reportName": report, "columns": "REPORT_DATE,TIME," + ",".join(f for _, f in fields),
                      "pageNumber": "1", "pageSize": "24", "sortColumns": "REPORT_DATE", "sortTypes": "-1", "source": "WEB", "client": "WEB"}
            data = (http_json(EASTMONEY_URL + "?" + urllib.parse.urlencode(params)).get("result") or {}).get("data") or []
            count = 0
            for row in data:
                period = normalize_period(row.get("TIME") or row.get("REPORT_DATE"))
                for indicator, field in fields:
                    if insert_vintage(conn, indicator=indicator, country="CN", period=period, value=to_float(row.get(field)),
                                      value_type="reported", release_date=str(row.get("REPORT_DATE") or "") or None,
                                      collected_at=collected_at, source="eastmoney", source_series=report, raw=row):
                        inserted += 1; count += 1
            conn.commit(); log_check(conn, collected_at, "eastmoney", dataset, "ok" if data else "empty", len(data), f"report={report}; inserted={count}")
        except Exception as exc:
            msg = safe_error(exc); errors.append(f"eastmoney/{dataset}: {msg}"); log_check(conn, collected_at, "eastmoney", dataset, "error", 0, msg)
    return inserted, errors


def collect_bls(conn: sqlite3.Connection, collected_at: str, years: int = 2) -> tuple[int, list[str]]:
    end_year = now_local().year; start_year = end_year - years + 1
    payload = {"seriesid": [sid for sid, _ in BLS_SERIES.values()], "startyear": str(start_year), "endyear": str(end_year)}
    inserted, errors = 0, []
    try:
        response = http_json(BLS_URL, method="POST", payload=payload, retries=0)
        if response.get("status") != "REQUEST_SUCCEEDED":
            raise RuntimeError(response.get("message") or response.get("status") or "BLS request failed")
        series_rows = {s.get("seriesID"): s.get("data", []) for s in response.get("Results", {}).get("series", [])}
        for indicator, (series_id, unit) in BLS_SERIES.items():
            rows = [r for r in series_rows.get(series_id, []) if str(r.get("period", "")).startswith("M")]
            rows.sort(key=lambda r: (str(r.get("year")), str(r.get("period"))))
            count = 0
            for idx, row in enumerate(rows):
                period = normalize_period(f"{row.get('year')}-{str(row.get('period', '')).replace('M', '')}")
                if insert_vintage(conn, indicator=indicator, country="US", period=period, value=to_float(row.get("value")),
                                  value_type="level_thousand_sa" if indicator == "nonfarm_payroll_level" else "percent_sa",
                                  release_date=None, collected_at=collected_at, source="bls", source_series=series_id, raw=row):
                    inserted += 1; count += 1
                if indicator == "nonfarm_payroll_level" and idx > 0:
                    previous = to_float(rows[idx - 1].get("value")); current = to_float(row.get("value"))
                    if previous is not None and current is not None and insert_vintage(conn, indicator="nonfarm_payroll_change", country="US", period=period,
                        value=current - previous, value_type="thousand_persons_sa_mom", release_date=None, collected_at=collected_at,
                        source="bls", source_series=series_id, raw={"period": period, "value": current, "previous": previous}):
                        inserted += 1; count += 1
            conn.commit(); log_check(conn, collected_at, "bls", indicator, "ok" if rows else "empty", len(rows), f"series={series_id}; unit={unit}; inserted={count}")
    except Exception as exc:
        msg = safe_error(exc); errors.append(f"bls: {msg}"); log_check(conn, collected_at, "bls", "us_employment", "error", 0, f"series={','.join(s for s,_ in BLS_SERIES.values())}; {msg}")
    return inserted, errors


def collect_fred(conn: sqlite3.Connection, collected_at: str, years: int = 2) -> tuple[int, list[str]]:
    start = f"{now_local().year - years + 1}-01-01"; inserted, errors = 0, []
    for indicator, (fred_id, unit) in {"nonfarm_payroll_level": ("PAYEMS", "thousand persons, seasonally adjusted"), "unemployment_rate": ("UNRATE", "percent, seasonally adjusted")}.items():
        try:
            url = FRED_CSV_URL + "?" + urllib.parse.urlencode({"id": fred_id, "cosd": start})
            rows = list(csv.DictReader(io.StringIO(http_bytes(url).decode("utf-8-sig"))))
            valid = [(normalize_period(r.get("observation_date")), to_float(r.get(fred_id)), r) for r in rows]
            valid = [(p, v, r) for p, v, r in valid if p and v is not None]
            count = 0
            for p, v, raw in valid:
                if insert_vintage(conn, indicator=indicator, country="US", period=p, value=v,
                    value_type="level_thousand_sa" if indicator == "nonfarm_payroll_level" else "percent_sa",
                    release_date=None, collected_at=collected_at, source="fred_csv", source_series=fred_id, raw=raw):
                    inserted += 1; count += 1
            if indicator == "nonfarm_payroll_level":
                for (p, v, _), (_, prev, _) in zip(valid[1:], valid[:-1]):
                    if insert_vintage(conn, indicator="nonfarm_payroll_change", country="US", period=p, value=v-prev,
                        value_type="thousand_persons_sa_mom", release_date=None, collected_at=collected_at,
                        source="fred_csv", source_series=fred_id, raw={"period": p, "value": v, "previous": prev}):
                        inserted += 1; count += 1
            conn.commit(); log_check(conn, collected_at, "fred_csv", indicator, "ok" if valid else "empty", len(valid), f"series={fred_id}; unit={unit}; fallback=BLS; inserted={count}")
        except Exception as exc:
            msg = safe_error(exc); errors.append(f"fred_csv/{indicator}: {msg}"); log_check(conn, collected_at, "fred_csv", indicator, "error", 0, msg)
    return inserted, errors


# 中国官方一手源：国家统计局 NBS（经 nbsc 库封装的新 UUID API；easyquery 已于 2026-05 废弃）。
# 官方一手优先于东财第三方；nbsc 未安装或接口失效时自动降级，东财仍兜底。
NBS_INDICATORS = {
    "cpi_yoy": ("get_annual_inflation", lambda v: round(v * 100, 4)),
    "cpi_mom": ("get_recent_inflation", lambda v: round(v * 100, 4)),
    "ppi_yoy": ("get_ppi_yoy", lambda v: round(v - 100, 4)),
    "ppi_mom": ("get_ppi_mom", lambda v: round(v - 100, 4)),
    "manufacturing_pmi": ("get_manufacturing_pmi", lambda v: round(v, 4)),
    "nonmanufacturing_pmi": ("get_non_manufacturing_pmi", lambda v: round(v, 4)),
    "composite_pmi": ("get_composite_pmi", lambda v: round(v, 4)),
    "cn_unemployment_rate": ("get_unemployment_rate", lambda v: round(v, 4)),
    "m0": ("get_m0", lambda v: round(v, 4)),
    "m0_yoy": ("get_m0_yoy", lambda v: round(v, 4)),
    "m1": ("get_m1", lambda v: round(v, 4)),
    "m1_yoy": ("get_m1_yoy", lambda v: round(v, 4)),
    "m2": ("get_m2", lambda v: round(v, 4)),
    "m2_yoy": ("get_m2_yoy", lambda v: round(v, 4)),
    "gdp_nominal": ("get_gdp_nominal", lambda v: round(v, 4)),
    "gdp_real": ("get_gdp_real", lambda v: round(v, 4)),
    "gdp_yoy": ("get_gdp_index", lambda v: round(v - 100, 4)),
    "gdp_qoq": ("get_gdp_qoq_growth", lambda v: round(v, 4)),
}


def norm_nbs_period(period) -> str:
    """把 nbsc 返回的 pandas Period 归一为 'YYYY-MM'。

    季度 Period（如 2026Q2）→ 季末月（2026-06），与全系统 'YYYY-MM' 周期格式对齐；
    月度 Period → 原 normalize_period。GDP 等季度指标由此保持正确的季末月标签。
    """
    ps = str(period)
    m = re.match(r"(20\d{2})Q([1-4])", ps)
    if m:
        return f"{m.group(1)}-{int(m.group(2)) * 3:02d}"
    return normalize_period(ps)


def collect_nbs(conn: sqlite3.Connection, collected_at: str, years: int = 2) -> tuple[int, list[str]]:
    """采集中国官方一手宏观数据（NBS），口径对齐现有东财指标。"""
    try:
        import nbsc  # noqa: F401
    except ImportError:
        # 官方一手源缺失必须显式上报（红线：不得静默降级）。
        # 裸 python 无 nbsc 时，中国 18 个 NBS 指标会退化为东财第三方；
        # 采集报告「待核项」必须列出，让使用者明确知晓官方一手源本次未采集。
        msg = "nbs: nbsc 未安装，中国官方一手源(NBS)本次未采集（CPI/PPI/PMI/M0/M1/M2/GDP/失业率已退化为东财第三方或缺失）"
        log_check(conn, collected_at, "nbs", "china_official", "skip", 0, "nbsc not installed; NBS official-primary unavailable; fallback eastmoney")
        return 0, [msg]
    start_year = str(now_local().year - years + 1)
    inserted, errors = 0, []
    for indicator, (fn_name, transform) in NBS_INDICATORS.items():
        try:
            series = getattr(nbsc, fn_name)(start_year)  # pandas Series indexed by monthly Period
            count = 0
            for period, raw in series.items():
                raw_f = float(raw)
                if raw_f != raw_f:  # NaN guard
                    continue
                p = norm_nbs_period(period)
                if insert_vintage(conn, indicator=indicator, country="CN", period=p, value=transform(raw_f),
                                  value_type="reported", release_date=None, collected_at=collected_at,
                                  source="nbs", source_series=fn_name,
                                  raw={"period": p, "raw_value": raw_f, "fn": fn_name}):
                    inserted += 1; count += 1
            conn.commit()
            log_check(conn, collected_at, "nbs", indicator, "ok" if count else "empty", len(series), f"fn={fn_name}; inserted={count}")
        except Exception as exc:
            msg = safe_error(exc)
            errors.append(f"nbs/{indicator}: {msg}")
            log_check(conn, collected_at, "nbs", indicator, "error", 0, msg)
    return inserted, errors


def build_report(conn: sqlite3.Connection, report_path: Path, collected_at: str, inserted: int, errors: list[str]) -> None:
    total = conn.execute("SELECT COUNT(*) FROM macro_indicators").fetchone()[0]
    revisions = conn.execute("SELECT COUNT(*) FROM macro_indicators WHERE is_revision=1").fetchone()[0]
    indicators = [r[0] for r in conn.execute("SELECT DISTINCT indicator_name FROM macro_indicators ORDER BY 1")]
    checks = conn.execute("SELECT source,status,dataset,detail FROM collection_checks WHERE checked_at=? ORDER BY id", (collected_at,)).fetchall()
    fallback = any(s == "fred_csv" and st == "ok" for s, st, _, _ in checks)
    visible_errors = [e for e in errors if not (fallback and e.startswith("bls:"))]
    lines = ["# 宏观数据采集报告", "", f"- 本次采集时点：`{collected_at}`", f"- 本次新增 vintage 行：`{inserted}`", f"- 库内累计行数：`{total}`", f"- 修订行数：`{revisions}`", f"- 覆盖指标：{', '.join(indicators) if indicators else '暂无'}", f"- 网络路由：`{_NETWORK_ROUTE}`（凭据不落库）", "", "## 数据源与口径", "- 中国 CPI/PPI/PMI/综合PMI/M0/M1/M2(及同比)/城镇调查失业率/GDP：首选国家统计局 NBS（`nbsc` 官方一手，直连免代理）；`cpi_base`/`ppi_base`/`ppi_accumulated` 等冗余派生/累计指数由东财第三方兜底，来源标记 `eastmoney`。", "- 美国就业：首选 BLS；BLS 不可达时使用 FRED PAYEMS/UNRATE，并在 checks 中标记真实来源。", "- 所有观测保留 period、collected_at、source、source_series、raw_json；修订 append-only，不覆盖历史。", "", "## 本次质量检查", "", "| 来源 | 状态 | 数据集 | 详情 |", "|---|---|---|---|"]
    lines.extend(f"| {s} | {st} | {ds} | {detail.replace('|', '/')[:180]} |" for s, st, ds, detail in checks)
    lines += ["", "## 待核项", ""]
    if visible_errors: lines.extend(f"- {e}" for e in visible_errors)
    elif fallback: lines.append("- BLS 当前不可达，已由 FRED 成功兜底；未伪称为 BLS API。")
    else: lines.append("- 本次采集未记录网络或数据源错误。")
    lines += ["", "## 后续处理", "", "- 采集报告只记录可验证事实；Agent 在 Verify 通过后运行 clean_macro_data.py 重建清洗库，供 MCP server 只读获取。", ""]
    report_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--output-root", default=None); ap.add_argument("--date", default=None); ap.add_argument("--proxy-url", default=None)
    args = ap.parse_args()
    if args.proxy_url: os.environ["MACRO_PROXY_URL"] = args.proxy_url
    route = configure_network(); date = args.date or now_local().date().isoformat()
    root = Path(args.output_root or os.environ.get("MACRO_OUTPUT_ROOT") or Path(__file__).resolve().parents[1] / "outputs"); output = root / date; output.mkdir(parents=True, exist_ok=True)
    db_path = output / "macro_indicators.sqlite"; report_path = output / "macro_collection_report.md"; collected_at = now_local().isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn); inserted_cn, errors_cn = collect_eastmoney(conn, collected_at)
        nbs_inserted, nbs_errors = collect_nbs(conn, collected_at); inserted_cn += nbs_inserted; errors_cn += nbs_errors
        inserted_us, errors_us = collect_bls(conn, collected_at)
        if errors_us:
            fred_inserted, fred_errors = collect_fred(conn, collected_at); inserted_us += fred_inserted; errors_us = [] if not fred_errors else errors_us + fred_errors
        build_report(conn, report_path, collected_at, inserted_cn + inserted_us, errors_cn + errors_us)
        total = conn.execute("SELECT COUNT(*) FROM macro_indicators").fetchone()[0]
        print(json.dumps({"network_route": route, "db": str(db_path), "report": str(report_path), "inserted": inserted_cn + inserted_us, "rows": total, "errors": errors_cn + errors_us}, ensure_ascii=False))
    finally: conn.close()
    return 0


if __name__ == "__main__": raise SystemExit(main())
