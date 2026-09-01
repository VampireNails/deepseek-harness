#!/usr/bin/env python3
"""Advanced cleaning layer for the macro-analysis database.

Reads the append-only raw vintage store(s) produced by collect_macro_data.py,
builds a canonical cleaned time series, and writes a stable, rebuildable
``macro_clean.sqlite``. The raw store is never mutated; the clean store is
fully derived and idempotent.

Cleaning steps (advanced tier, per user decision):
  1. Source priority + de-duplication  (latest collected_at wins per key)
  2. Unit / value_type normalization    (keep one canonical series per indicator)
  3. Period alignment                   (YYYY-MM)
  4. Missing-value imputation           (linear interpolation; is_imputed flagged)
  5. Seasonal adjustment (STL)          (level-type series only; method recorded)

Outputs three tables:
  clean_series   canonical long table (value / value_sa / value_imputed / flags)
  indicators     metadata per indicator (label, unit, sa_method, coverage...)
  vintage_traces revision history per (indicator, country, period)
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

# ---- indicator metadata (label / unit / seasonal-adjustment applicability) ----
METRICS: dict[str, dict[str, Any]] = {
    "cpi_yoy":              {"label": "中国 CPI 同比",        "unit": "%",   "sa": False},
    "cpi_mom":              {"label": "中国 CPI 环比",        "unit": "%",   "sa": False},
    "cpi_base":             {"label": "中国 CPI 同比指数",    "unit": "上年同月=100", "sa": False},
    "ppi_yoy":              {"label": "中国 PPI 同比",        "unit": "%",   "sa": False},
    "ppi_base":             {"label": "中国 PPI 同比指数",    "unit": "上年同月=100", "sa": False},
    "ppi_accumulated":      {"label": "中国 PPI 累计同比指数", "unit": "年初至今累计=100", "sa": False},
    "manufacturing_pmi":    {"label": "中国制造业 PMI",       "unit": "点",  "sa": False},
    "nonmanufacturing_pmi": {"label": "中国非制造业 PMI",     "unit": "点",  "sa": False},
    "composite_pmi":        {"label": "中国综合 PMI 产出指数", "unit": "点",  "sa": False},
    "cn_unemployment_rate": {"label": "中国城镇调查失业率",    "unit": "%",   "sa": False},
    "ppi_mom":              {"label": "中国 PPI 环比",        "unit": "%",   "sa": False},
    "m0":                   {"label": "中国 M0 期末余额",     "unit": "亿元", "sa": False},
    "m0_yoy":               {"label": "中国 M0 同比",         "unit": "%",   "sa": False},
    "m1":                   {"label": "中国 M1 期末余额",     "unit": "亿元", "sa": False},
    "m1_yoy":               {"label": "中国 M1 同比",         "unit": "%",   "sa": False},
    "m2":                   {"label": "中国 M2 期末余额",     "unit": "亿元", "sa": False},
    "m2_yoy":               {"label": "中国 M2 同比",         "unit": "%",   "sa": False},
    "gdp_nominal":          {"label": "中国 GDP 现价当季",    "unit": "亿元", "sa": False, "freq": "Q"},
    "gdp_real":             {"label": "中国 GDP 不变价当季",  "unit": "亿元", "sa": False, "freq": "Q"},
    "gdp_yoy":              {"label": "中国 GDP 同比",        "unit": "%",   "sa": False, "freq": "Q"},
    "gdp_qoq":              {"label": "中国 GDP 环比",        "unit": "%",   "sa": False, "freq": "Q"},
    "nonfarm_payroll_level":   {"label": "美国非农就业人数",  "unit": "千人", "sa": True},
    "nonfarm_payroll_change":  {"label": "美国非农就业环比变化", "unit": "千人", "sa": False},
    "unemployment_rate":    {"label": "美国失业率",           "unit": "%",   "sa": False},
}
# value_type -> canonical label we keep (one per indicator)
KEEP_VALUE_TYPE: dict[str, str] = {
    "cpi_yoy": "reported", "cpi_mom": "reported", "cpi_base": "reported",
    "ppi_yoy": "reported", "ppi_base": "reported", "ppi_accumulated": "reported",
    "manufacturing_pmi": "reported", "nonmanufacturing_pmi": "reported",
    "composite_pmi": "reported", "cn_unemployment_rate": "reported", "ppi_mom": "reported",
    "m0": "reported", "m0_yoy": "reported", "m1": "reported", "m1_yoy": "reported",
    "m2": "reported", "m2_yoy": "reported",
    "gdp_nominal": "reported", "gdp_real": "reported", "gdp_yoy": "reported", "gdp_qoq": "reported",
    "nonfarm_payroll_level": "level_thousand_sa", "nonfarm_payroll_change": "thousand_persons_sa_mom",
    "unemployment_rate": "percent_sa",
}
SOURCE_PRIORITY = {"nbs": 1, "bls": 1, "eastmoney": 2, "fred_csv": 3}


# --------------------------------------------------------------------------
# 派生层（飞轮③延伸：派生指标治理）
# --------------------------------------------------------------------------
# 数据基石原则：派生指标必须记录，但作为独立「派生层」与「观测层」严格分层，
# 且必须带血缘（derived_from）+ 变换版本（transform_version）+ 计算时点（computed_at）。
# 派生值绝不与观测值混存，也绝不覆盖官方出版值：当某变换官方已出版（如 m2_yoy、
# gdp_qoq、nonfarm_payroll_change），出版序列属观测层，公式仅作一致性对账 + 缺口兜底。
#
# 变换登记簿（借鉴 dbt：变换版本化定义入 VCS；借鉴 OpenLineage：血缘可追溯）。
TRANSFORM_REGISTRY = [
    ("yoy_from_level", "由水平序列计算同比 (YoY)", "value_t / value_{t-12} - 1", "M", "1",
     "m2/m1/m0 等货币供应余额同比；官方出版时仅作对账（年比消除季节性，可与出版值对账）"),
    ("diff_level", "由水平序列计算环比变化量 (MoM diff)", "value_t - value_{t-1月}", "M", "1",
     "nonfarm_payroll_level -> nonfarm_payroll_change；BLS 出版值即水平差分，可精确对账"),
]

# 派生规格：派生指标 -> 底层源指标 / 频率 / 变换 / 版本 / 缩放(百分点或原单位) / 一致性容差
# 注：gdp_qoq 不在此列——NBS 出版的是「季节调整后环比」，而 gdp_real 是未季调水平，
# 二者口径不可比（NSA 水平差额 ≠ SA 出版环比）；故 gdp_qoq 仅作观测层，不做公式对账/兜底。
# 容差：百分比变换 0.1（吸收出版值保留 1 位小数的舍入）；变化量(千人) 1.0。
DERIVED_SPECS = {
    "m2_yoy":               {"from": "m2",                   "freq": "M", "transform": "yoy_from_level", "version": "1", "scale": 100, "tol": 0.1},
    "m1_yoy":               {"from": "m1",                   "freq": "M", "transform": "yoy_from_level", "version": "1", "scale": 100, "tol": 0.1},
    "m0_yoy":               {"from": "m0",                   "freq": "M", "transform": "yoy_from_level", "version": "1", "scale": 100, "tol": 0.1},
    "nonfarm_payroll_change": {"from": "nonfarm_payroll_level", "freq": "M", "transform": "diff_level", "version": "1", "scale": 1, "tol": 1.0},
}


def shift_period(period: str, months: int) -> str | None:
    """把 'YYYY-MM' 往前推 months 个月，返回归一化 'YYYY-MM'。"""
    import re
    m = re.match(r"(20\d{2})-(\d{2})", period)
    if not m:
        return None
    total = int(m.group(1)) * 12 + (int(m.group(2)) - 1) - months
    y, mo = divmod(total, 12)
    return f"{y}-{mo + 1:02d}"


def try_imports():
    try:
        import pandas as pd  # noqa: F401
        from statsmodels.tsa.seasonal import STL
        return pd, STL, True
    except Exception as exc:  # pragma: no cover
        print(json.dumps({"warn": "statsmodels unavailable; SA disabled", "detail": str(exc)}))
        return None, None, False


def norm_period(value: Any) -> str:
    import re
    text = str(value or "").strip()
    m = re.search(r"(20\d{2})\D{0,5}(1[0-2]|0?[1-9])", text)
    return f"{m.group(1)}-{int(m.group(2)):02d}" if m else text[:10]


def load_raw_rows(root: Path) -> list[dict[str, Any]]:
    """Merge every raw macro_indicators store found under outputs/*/."""
    paths = sorted(glob.glob(str(root / "*" / "macro_indicators.sqlite")))
    rows: list[dict[str, Any]] = []
    for p in paths:
        try:
            conn = sqlite3.connect(p)
            for r in conn.execute(
                "SELECT indicator_name,country,period,value,value_type,release_date,"
                "collected_at,source,source_series,is_revision,original_value "
                "FROM macro_indicators WHERE value IS NOT NULL"
            ):
                rows.append({
                    "indicator": r[0], "country": r[1], "period": norm_period(r[2]),
                    "value": r[3], "value_type": r[4], "release_date": r[5],
                    "collected_at": r[6], "source": r[7], "series": r[8],
                    "is_revision": int(r[9] or 0), "original": r[10],
                })
            conn.close()
        except sqlite3.Error as exc:
            print(json.dumps({"warn": f"skip {p}", "detail": str(exc)}))
    return rows


def canonical_per_key(rows: list[dict[str, Any]]) -> dict[tuple, dict[str, Any]]:
    """For each (indicator,country,period,value_type,source) keep latest collected_at."""
    best: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        key = (r["indicator"], r["country"], r["period"], r["value_type"], r["source"])
        prev = best.get(key)
        if prev is None or r["collected_at"] > prev["collected_at"]:
            best[key] = r
    return best


def pick_source(rows_by_key: dict[tuple, dict[str, Any]]) -> dict[tuple, dict[str, Any]]:
    """Pick the highest-priority source value for each (indicator,country,period)."""
    by_period: dict[tuple, list[dict[str, Any]]] = {}
    for key, r in rows_by_key.items():
        ind, cty, per, vtype, src = key
        by_period.setdefault((ind, cty, per), []).append(r)
    chosen: dict[tuple, dict[str, Any]] = {}
    for k, candidates in by_period.items():
        candidates.sort(key=lambda x: (SOURCE_PRIORITY.get(x["source"], 99), -1))
        chosen[k] = candidates[0]
    return chosen


def stl_adjust(periods: list[str], values: list[float], STL) -> list[float] | None:
    try:
        import pandas as pd
        s = pd.Series(values, index=pd.PeriodIndex(periods, freq="M"))
        s = s.sort_index()
        if len(s) < 24:
            return None
        resid = STL(s, period=12).fit()
        return [round(float(x), 6) for x in (resid.trend + resid.resid)]
    except Exception:
        return None


def build_clean(rows: list[dict[str, Any]], pd, STL, sa_enabled: bool):
    by_key = canonical_per_key(rows)
    chosen = pick_source(by_key)  # key=(indicator,country,period)
    # group series by (indicator,country)
    series: dict[tuple, list[dict[str, Any]]] = {}
    for (ind, cty, per), r in chosen.items():
        if KEEP_VALUE_TYPE.get(ind) and r["value_type"] != KEEP_VALUE_TYPE[ind]:
            continue
        series.setdefault((ind, cty), []).append(r)

    clean_rows: list[dict[str, Any]] = []
    indicators_meta: list[dict[str, Any]] = []
    vintage_traces: list[dict[str, Any]] = []

    for (ind, cty), items in series.items():
        items.sort(key=lambda x: x["period"])
        periods = [x["period"] for x in items]
        raw_vals = [x["value"] for x in items]
        meta = METRICS.get(ind, {})
        freq = meta.get("freq", "M")
        val_map = {x["period"]: x["value"] for x in items}
        src_map = {x["period"]: x["source"] for x in items}
        rel_map = {x["period"]: x["release_date"] for x in items}
        col_map = {x["period"]: x["collected_at"] for x in items}
        sa_method = "not_applicable"
        if freq == "Q":
            # 季度指标：period 已是季末月（如 2026-06），保持原样；不做月度插值/STL。
            full_idx = periods
            aligned = list(raw_vals)
            is_imp = [0] * len(full_idx)
            imputed = [None] * len(full_idx)
            value_sa = [None if (v is None or (isinstance(v, float) and v != v)) else round(float(v), 6)
                        for v in aligned]
        else:
            # 月度指标：补齐连续月索引 + 短缺口线性插值 + 可选 STL。
            import pandas as pd_local
            full_idx = pd_local.period_range(periods[0], periods[-1], freq="M").astype(str).tolist()
            aligned = [val_map.get(p) for p in full_idx]
            # imputation (advanced): linear interpolation, but only for SHORT gaps
            # (<= MAX_GAP months). Long gaps stay NULL so we never fabricate history.
            MAX_GAP = 6
            s = pd_local.Series(aligned, index=full_idx)
            is_imp = [1 if v is None else 0 for v in aligned]
            s_interp = s.interpolate(method="linear", limit=MAX_GAP, limit_direction="both")
            imputed = [None if (is_imp[i] == 0 or s_interp.iloc[i] is None or (isinstance(s_interp.iloc[i], float) and s_interp.iloc[i] != s_interp.iloc[i]))
                       else round(float(s_interp.iloc[i]), 6) for i in range(len(full_idx))]
            value_sa = [None] * len(full_idx)
            if sa_enabled and meta.get("sa") and STL is not None:
                adj = stl_adjust(full_idx, [float(x) for x in s_interp.tolist()], STL)
                if adj is not None:
                    value_sa = adj
                    sa_method = "STL"
            # When seasonal adjustment is not applicable (official series already
            # SA / YoY / MoM), value_sa falls back to the interpolated series so
            # the field is never empty for external consumers.
            if sa_method == "not_applicable":
                value_sa = [None if (v is None or (isinstance(v, float) and v != v)) else round(float(v), 6)
                            for v in s_interp.tolist()]
        for i, p in enumerate(full_idx):
            clean_rows.append({
                "indicator": ind, "country": cty, "period": p,
                "value": aligned[i],
                "value_sa": value_sa[i],
                "value_imputed": (None if is_imp[i] == 0 else imputed[i]),
                "is_imputed": is_imp[i],
                "source": src_map.get(p),
                "release_date": rel_map.get(p),
                "collected_at": col_map.get(p),
            })
        n_obs = sum(1 for v in aligned if v is not None)
        n_imp = sum(1 for x in imputed if x is not None)
        indicators_meta.append({
            "indicator": ind, "country": cty,
            "label": meta.get("label", ind), "unit": meta.get("unit", ""),
            "frequency": freq, "sa_method": sa_method,
            "first_period": full_idx[0], "last_period": full_idx[-1],
            "n_obs": n_obs, "n_imputed": n_imp,
            "last_updated": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        })

    # vintage traces: original + every revision per (indicator,country,period)
    # vintage traces: per (indicator,country,period) keep value-change points.
    # Free sources (eastmoney/FRED/BLS) return stable revised values, so this
    # captures the first observation plus any later revision the source later
    # published. When the value never changed, only the first snapshot remains.
    from collections import defaultdict
    vt_groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if KEEP_VALUE_TYPE.get(r["indicator"]) and r["value_type"] != KEEP_VALUE_TYPE[r["indicator"]]:
            continue
        vt_groups[(r["indicator"], r["country"], r["period"])].append(r)
    for (ind, cty, per), grp in vt_groups.items():
        grp.sort(key=lambda x: x["collected_at"])
        last_val: float | None = None
        for r in grp:
            if r["value"] != last_val:
                vintage_traces.append({
                    "indicator": ind, "country": cty, "period": per,
                    "value_type": r["value_type"], "source": r["source"],
                    "collected_at": r["collected_at"], "value": r["value"],
                    "original_value": last_val,
                    "is_revision": 0 if last_val is None else 1,
                })
                last_val = r["value"]
    return clean_rows, indicators_meta, vintage_traces


def build_derived(clean_rows: list[dict[str, Any]], indicators_meta: list[dict[str, Any]],
                   computed_at: str) -> tuple[list[dict[str, Any]], list[tuple]]:
    """由观测层水平序列计算派生指标，返回 (派生 clean 行, 一致性对账行)。

    规则：
    - 若某期该派生指标已有官方观测值 -> 仅记录 derived_checks 对账（observed vs
      公式派生），不覆盖、不重复入库。
    - 若某期缺官方观测值（缺口）-> 写入 layer='derived' 行作兜底填充，并标注血缘。
    - 派生值 source 记为 'derived'，避免冒充任一官方源（红线）。
    """
    import json as _json
    from collections import defaultdict

    obs: dict[tuple, float] = {}
    for r in clean_rows:
        obs[(r["indicator"], r["country"], r["period"])] = r["value"]

    derived_rows: list[dict[str, Any]] = []
    checks: list[tuple] = []
    meta_keys = {(m["indicator"], m["country"]) for m in indicators_meta}
    offset_by_transform = {"yoy_from_level": 12, "qoq_from_level": 3, "diff_level": 1}

    for ind, spec in DERIVED_SPECS.items():
        frm = spec["from"]; freq = spec["freq"]; transform = spec["transform"]
        version = spec["version"]; scale = spec.get("scale", 1); tol = spec.get("tol", 0.05)
        months = offset_by_transform[transform]
        # 按国家收集底层源序列
        src: dict[str, dict[str, float]] = defaultdict(dict)
        for (si, cty, per), v in obs.items():
            if si == frm and v is not None:
                src[cty][per] = v
        for cty, permap in src.items():
            for per, v in permap.items():
                pp = shift_period(per, months)
                if pp is None or pp not in permap:
                    continue
                prev_v = permap[pp]
                if prev_v in (None, 0):
                    continue
                if transform == "yoy_from_level":
                    dv = (v / prev_v - 1) * scale
                elif transform == "qoq_from_level":
                    dv = (v / prev_v - 1) * scale
                elif transform == "diff_level":
                    dv = (v - prev_v) * scale
                else:
                    continue
                dv = round(float(dv), 6)
                ov = obs.get((ind, cty, per))
                if ov is not None:
                    delta = round(float(ov) - dv, 6)
                    checks.append((ind, cty, per, ov, dv, delta, abs(delta),
                                  1 if abs(delta) <= tol else 0, transform, version))
                else:
                    derived_rows.append({
                        "indicator": ind, "country": cty, "period": per,
                        "value": dv, "value_sa": None, "value_imputed": None, "is_imputed": 0,
                        "source": "derived", "release_date": None, "collected_at": computed_at,
                        "layer": "derived",
                        "derived_from": _json.dumps([f"{frm}:{cty}"]),
                        "transform": transform, "transform_version": version, "computed_at": computed_at,
                    })
                    if (ind, cty) not in meta_keys:
                        meta_keys.add((ind, cty))
                        indicators_meta.append({
                            "indicator": ind, "country": cty,
                            "label": METRICS.get(ind, {}).get("label", ind),
                            "unit": METRICS.get(ind, {}).get("unit", ""),
                            "frequency": freq, "sa_method": "derived",
                            "first_period": per, "last_period": per,
                            "n_obs": 1, "n_imputed": 0, "last_updated": computed_at,
                        })
                    else:
                        for m in indicators_meta:
                            if m["indicator"] == ind and m["country"] == cty:
                                m["first_period"] = min(m["first_period"], per)
                                m["last_period"] = max(m["last_period"], per)
                                m["n_obs"] = m.get("n_obs", 0) + 1
                                m["sa_method"] = "derived"
                                break
    return derived_rows, checks


# --------------------------------------------------------------------------
# 轮 B：数据可信度 —— revision_stats（修订幅度统计）+ source_trust（源可信度分级）
# 借鉴 FAIR / data provenance 分级：官方一手 > 官方二次 > 第三方。绝不把第三方冒充一手。
# --------------------------------------------------------------------------

SOURCE_TRUST = [
    ("nbs", "国家统计局 NBS", "official_primary", "中国官方一手源", 0),
    ("bls", "美国劳工统计局 BLS", "official_primary", "官方一手源", 1),
    ("fred_csv", "圣路易斯联储 FRED", "official_secondary", "官方二次发布（数据归属 BLS）", 2),
    ("eastmoney", "东方财富宏观数据中心", "third_party", "第三方结构化源", 3),
]


def build_revision_stats(clean_rows: list[dict], vintage_traces: list[dict]) -> list[tuple]:
    """按 (indicator,country) 汇总：首值/终值/修订次数/平均修订幅度。

    免费源返回稳定修订值，故 n_rev 通常为 0；接入官方一手源（NBS 每日快照对比）后
    此处才会有真实修订统计。字段不虚构：n_rev=0 时 mean_abs_rev 为 NULL。
    """
    from collections import defaultdict
    rev: dict[tuple, dict] = defaultdict(lambda: {"n": 0, "abs": []})
    for t in vintage_traces:
        if t.get("is_revision") == 1:
            rev[(t["indicator"], t["country"])]["n"] += 1
            if t.get("original_value") is not None and t.get("value") is not None:
                rev[(t["indicator"], t["country"])]["abs"].append(abs(t["value"] - t["original_value"]))
    fl: dict[tuple, dict] = {}
    for r in clean_rows:
        key = (r["indicator"], r["country"])
        if key not in fl:
            fl[key] = {"first": r["value"], "last": r["value"], "fp": r["period"], "lp": r["period"]}
        else:
            if r["period"] < fl[key]["fp"]:
                fl[key]["first"], fl[key]["fp"] = r["value"], r["period"]
            if r["period"] > fl[key]["lp"]:
                fl[key]["last"], fl[key]["lp"] = r["value"], r["period"]
    out: list[tuple] = []
    for key in sorted(set(fl) | set(rev)):
        ind, cty = key
        f = fl.get(key, {})
        rv = rev.get(key, {"n": 0, "abs": []})
        abs_revs = rv.get("abs", [])
        mean_abs = round(sum(abs_revs) / len(abs_revs), 6) if abs_revs else None
        out.append((ind, cty, f.get("first"), f.get("last"), rv.get("n", 0), mean_abs))
    return out


def write_db(db_path: Path, clean_rows, indicators_meta, vintage_traces, derived_checks) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript("""
    DROP TABLE IF EXISTS clean_series;
    DROP TABLE IF EXISTS indicators;
    DROP TABLE IF EXISTS vintage_traces;
    DROP TABLE IF EXISTS revision_stats;
    DROP TABLE IF EXISTS source_trust;
    DROP TABLE IF EXISTS transform_registry;
    DROP TABLE IF EXISTS derived_checks;
    CREATE TABLE clean_series (
      indicator TEXT NOT NULL, country TEXT NOT NULL, period TEXT NOT NULL,
      value REAL, value_sa REAL, value_imputed REAL, is_imputed INTEGER NOT NULL DEFAULT 0,
      source TEXT, release_date TEXT, collected_at TEXT,
      layer TEXT NOT NULL DEFAULT 'observed',
      derived_from TEXT, transform TEXT, transform_version TEXT, computed_at TEXT,
      PRIMARY KEY(indicator, country, period)
    );
    CREATE TABLE transform_registry (
      transform_id TEXT PRIMARY KEY, description TEXT NOT NULL, formula TEXT NOT NULL,
      frequency TEXT NOT NULL, version TEXT NOT NULL, notes TEXT
    );
    CREATE TABLE derived_checks (
      indicator TEXT NOT NULL, country TEXT NOT NULL, period TEXT NOT NULL,
      observed_value REAL, derived_value REAL, delta REAL, abs_delta REAL,
      consistent INTEGER NOT NULL, transform TEXT NOT NULL, transform_version TEXT NOT NULL
    );
    CREATE TABLE indicators (
      indicator TEXT NOT NULL, country TEXT NOT NULL, label TEXT, unit TEXT,
      frequency TEXT, sa_method TEXT, first_period TEXT, last_period TEXT,
      n_obs INTEGER, n_imputed INTEGER, last_updated TEXT,
      PRIMARY KEY(indicator, country)
    );
    CREATE TABLE vintage_traces (
      indicator TEXT NOT NULL, country TEXT NOT NULL, period TEXT NOT NULL,
      value_type TEXT, source TEXT, collected_at TEXT, value REAL,
      original_value REAL, is_revision INTEGER
    );
    CREATE TABLE revision_stats (
      indicator TEXT NOT NULL, country TEXT NOT NULL,
      first_value REAL, last_value REAL, n_revisions INTEGER, mean_abs_rev REAL,
      PRIMARY KEY(indicator, country)
    );
    CREATE TABLE source_trust (
      source TEXT PRIMARY KEY, authority TEXT NOT NULL,
      trust_level TEXT NOT NULL, attribution TEXT NOT NULL, priority INTEGER NOT NULL
    );
    """)
    conn.executemany(
        "INSERT OR REPLACE INTO clean_series "
        "(indicator,country,period,value,value_sa,value_imputed,is_imputed,source,release_date,collected_at,layer,derived_from,transform,transform_version,computed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r["indicator"], r["country"], r["period"], r["value"], r["value_sa"],
          r["value_imputed"], r["is_imputed"], r["source"], r["release_date"], r["collected_at"],
          r.get("layer", "observed"), r.get("derived_from"), r.get("transform"),
          r.get("transform_version"), r.get("computed_at"))
         for r in clean_rows])
    conn.executemany(
        "INSERT OR REPLACE INTO indicators VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(m["indicator"], m["country"], m["label"], m["unit"], m["frequency"], m["sa_method"],
          m["first_period"], m["last_period"], m["n_obs"], m["n_imputed"], m["last_updated"])
         for m in indicators_meta])
    conn.executemany(
        "INSERT INTO vintage_traces VALUES(?,?,?,?,?,?,?,?,?)",
        [(t["indicator"], t["country"], t["period"], t["value_type"], t["source"],
          t["collected_at"], t["value"], t["original_value"], t["is_revision"])
         for t in vintage_traces])
    rev_stats = build_revision_stats(clean_rows, vintage_traces)
    conn.executemany("INSERT OR REPLACE INTO revision_stats VALUES(?,?,?,?,?,?)", rev_stats)
    conn.executemany("INSERT OR REPLACE INTO source_trust VALUES(?,?,?,?,?)", SOURCE_TRUST)
    conn.executemany(
        "INSERT OR REPLACE INTO transform_registry VALUES(?,?,?,?,?,?)", TRANSFORM_REGISTRY)
    conn.executemany(
        "INSERT INTO derived_checks VALUES(?,?,?,?,?,?,?,?,?,?)", derived_checks)
    conn.commit(); conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-root", default=None)
    ap.add_argument("--clean-db", default=None)
    args = ap.parse_args()
    root = Path(args.output_root or os.environ.get("MACRO_OUTPUT_ROOT") or Path(__file__).resolve().parents[1] / "outputs")
    db_path = Path(args.clean_db or os.environ.get("MACRO_CLEAN_DB") or root / "macro_clean.sqlite")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    pd, STL, sa_enabled = try_imports()
    rows = load_raw_rows(root)
    clean_rows, indicators_meta, vintage_traces = build_clean(rows, pd, STL, sa_enabled)
    computed_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    derived_rows, derived_checks = build_derived(clean_rows, indicators_meta, computed_at)
    clean_rows.extend(derived_rows)
    write_db(db_path, clean_rows, indicators_meta, vintage_traces, derived_checks)
    n_inconsistent = sum(1 for c in derived_checks if c[7] == 0)
    print(json.dumps({
        "clean_db": str(db_path), "raw_rows": len(rows),
        "clean_rows": len(clean_rows), "observed_rows": len(clean_rows) - len(derived_rows),
        "derived_rows": len(derived_rows), "indicators": len(indicators_meta),
        "vintage_traces": len(vintage_traces), "derived_checks": len(derived_checks),
        "derived_inconsistent": n_inconsistent, "sa_enabled": sa_enabled,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
