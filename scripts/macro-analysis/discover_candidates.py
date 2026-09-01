#!/usr/bin/env python3
"""飞轮④ 候选发现：官方一手覆盖度扫描（v1）。

把「官方一手已能提供、但我们尚未跟踪」的核心宏观指标，作为候选提案输出，
写入独立库 outputs/macro_discovery.sqlite（candidate_dimensions 表）。

数据来源（确定性、零网络、零 LLM、零 key）：
  - nbsc 包内置 codes.json：25 个官方 NBS 系列（含英文描述、频率、UUID）。
  - 下方 CANDIDATES：精选的「值得跟踪」逻辑指标 → 中文标签 / 分类 / 是否核心 /
    单位 / 频率 / nbsc 访问函数。
  - 已跟踪集合：动态从 collect_macro_data.NBS_INDICATORS 与
    clean_macro_data.METRICS 推导（单一事实源，避免漂移）。

红线：本脚本只读 + 只写独立提案库。绝不调用 collect、绝不写
      macro_indicators / macro_clean。候选须经人工审批（status=pending）后，
      才走现有「3 文件注册」机制进入数据层。

v2/v3 预留（详见 SOP §9）：
  - NBS 目录树实时 diff：易碎（NBS 2026-06 已换 endpoint、有 WAF JS 挑战、UUID 会失效）。
  - FRED 发布日历/搜索：需 FRED API key（项目现无）。
  - GDELT 新闻热点反查：噪声大 + 滞后于官方发布，需关键词映射 + LLM。
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import sqlite3
from pathlib import Path

# 当前已跟踪指标（候选判定基准）。优先动态推导，失败时回退此硬编码快照。
TRACKED_FALLBACK = {
    "cpi_yoy", "cpi_mom", "cpi_base", "ppi_yoy", "ppi_base", "ppi_accumulated",
    "manufacturing_pmi", "nonmanufacturing_pmi",
    "nonfarm_payroll_level", "nonfarm_payroll_change", "unemployment_rate",
}

# 官方 NBS 一手候选目录：suggested_name -> (label_zh, category, core, unit, freq, nbsc_fn)
# 其中 core=True 的是「核心宏观指标，缺失会严重削弱分析可信度」。
CANDIDATES: dict[str, tuple] = {
    # ---- GDP（季度，7 个系列中最该跟踪的 4 个口径） ----
    "gdp_nominal":  ("中国 GDP 现价当季",          "GDP", True,  "亿元", "quarter", "get_gdp_nominal"),
    "gdp_real":     ("中国 GDP 不变价当季",        "GDP", True,  "亿元", "quarter", "get_gdp_real"),
    "gdp_index_yoy":("中国 GDP 同比指数",          "GDP", True,  "上年同期=100", "quarter", "get_gdp_index"),
    "gdp_qoq":      ("中国 GDP 环比增速",          "GDP", True,  "%",   "quarter", "get_gdp_qoq_growth"),
    # ---- 货币供应（月度，M0/M1/M2 存量 + 同比） ----
    "m2":           ("中国 M2 期末余额",           "货币供应", True,  "亿元", "month", "get_m2"),
    "m2_yoy":       ("中国 M2 同比",               "货币供应", True,  "%",   "month", "get_m2_yoy"),
    "m1":           ("中国 M1 期末余额",           "货币供应", True,  "亿元", "month", "get_m1"),
    "m1_yoy":       ("中国 M1 同比",               "货币供应", True,  "%",   "month", "get_m1_yoy"),
    "m0":           ("中国 M0 期末余额",           "货币供应", True,  "亿元", "month", "get_m0"),
    "m0_yoy":       ("中国 M0 同比",               "货币供应", True,  "%",   "month", "get_m0_yoy"),
    # ---- 就业 / 景气 / 价格 ----
    "cn_unemployment_rate": ("中国城镇调查失业率", "就业", True,  "%",   "month", "get_unemployment_rate"),
    "composite_pmi":        ("中国综合 PMI 产出指数", "景气", True, "点",  "month", "get_composite_pmi"),
    "ppi_mom":              ("中国 PPI 环比",      "价格", True,  "%",   "month", "get_ppi_mom"),
    # ---- 次要（有官方一手、但优先级低于核心项） ----
    "real_estate_investment_yoy": ("房地产开发投资累计同比", "投资", False, "%", "month", "get_real_estate_investment_growth_rate"),
    "fixed_assets_investment_yoy": ("固定资产投资同比",      "投资", False, "%", "month", "get_new_fixed_assets_growth_rate"),
    "auto_retail_yoy":             ("汽车零售同比",          "消费", False, "%", "month", "get_auto_retail_growth_rate"),
    "household_appliances_retail_yoy": ("家电零售同比",      "消费", False, "%", "month", "get_household_appliances_retail_growth_rate"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidate_dimensions (
    suggested_name  TEXT PRIMARY KEY,
    label_zh        TEXT NOT NULL,
    category        TEXT,
    core            INTEGER NOT NULL DEFAULT 0,
    unit            TEXT,
    freq            TEXT,
    source          TEXT NOT NULL,
    authority_level TEXT NOT NULL,
    nbsc_fn         TEXT,
    confidence      REAL,
    status          TEXT NOT NULL DEFAULT 'pending',
    first_detected  TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);
"""


def _load_module(name: str, here: Path):
    spec = importlib.util.spec_from_file_location(name, here / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tracked_indicators() -> set[str]:
    """从 collect/clean 模块动态推导已跟踪指标；失败回退硬编码快照。"""
    try:
        here = Path(__file__).resolve().parent
        cm = _load_module("collect_macro_data", here)
        cl = _load_module("clean_macro_data", here)
        return set(cm.NBS_INDICATORS.keys()) | set(cl.METRICS.keys())
    except Exception:
        return set(TRACKED_FALLBACK)


def discover(root: Path) -> list[dict]:
    tracked = tracked_indicators()
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    db_path = root / "macro_discovery.sqlite"
    conn = sqlite3.connect(str(db_path))
    candidates: list[dict] = []
    try:
        conn.executescript(SCHEMA)
        for name, (label, category, core, unit, freq, fn) in CANDIDATES.items():
            is_new = name not in tracked
            if is_new:
                conn.execute(
                    """INSERT INTO candidate_dimensions
                       (suggested_name,label_zh,category,core,unit,freq,source,authority_level,
                        nbsc_fn,confidence,status,first_detected,last_seen)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(suggested_name) DO UPDATE SET last_seen=excluded.last_seen,
                         label_zh=excluded.label_zh, category=excluded.category,
                         core=excluded.core, unit=excluded.unit, freq=excluded.freq,
                         nbsc_fn=excluded.nbsc_fn""",
                    (name, label, category, 1 if core else 0, unit, freq, "nbs",
                     "official_primary", fn, 1.0, "pending", now, now))
            candidates.append({
                "suggested_name": name, "label_zh": label, "category": category,
                "core": bool(core), "unit": unit, "freq": freq, "nbsc_fn": fn,
                "tracked": not is_new,
            })
        # 已批准/注册过的候选不回退为 pending（保留其终态）
        conn.commit()
    finally:
        conn.close()
    return candidates


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-root", default=None)
    args = ap.parse_args()
    root = Path(args.output_root or os.environ.get("MACRO_OUTPUT_ROOT")
                or Path(__file__).resolve().parents[1] / "outputs")
    root.mkdir(parents=True, exist_ok=True)
    rows = discover(root)
    untracked = [r for r in rows if not r["tracked"]]
    core = [r for r in untracked if r["core"]]
    sec = [r for r in untracked if not r["core"]]
    print(json.dumps({
        "discovery_db": str(root / "macro_discovery.sqlite"),
        "catalog_size": len(rows),
        "untracked": len(untracked),
        "core_untracked": len(core),
        "secondary_untracked": len(sec),
        "core_candidates": [r["suggested_name"] for r in core],
        "secondary_candidates": [r["suggested_name"] for r in sec],
        "top_gaps": [f"{r['suggested_name']}({r['label_zh']})" for r in core],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
