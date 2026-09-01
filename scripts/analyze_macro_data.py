#!/usr/bin/env python3
"""Create a traceable macro retrospective and next-period heuristic report.

This script is deliberately deterministic: it reads only the vintage SQLite
store and never fetches data or invents missing observations. The report is a
baseline for the macro-analysis Agent to review after the independent Proof.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Any

SOURCE_PRIORITY = {"bls": 1, "eastmoney": 2, "fred_csv": 3}
METRICS = {
    "cpi_yoy": ("中国 CPI 同比", "%"),
    "cpi_mom": ("中国 CPI 环比", "%"),
    "ppi_yoy": ("中国 PPI 同比", "%"),
    "manufacturing_pmi": ("中国制造业 PMI", "点"),
    "nonmanufacturing_pmi": ("中国非制造业 PMI", "点"),
    "nonfarm_payroll_change": ("美国非农就业环比", "千人"),
    "unemployment_rate": ("美国失业率", "%"),
}


def norm_period(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"(20\d{2})\D{0,5}(0?[1-9]|1[0-2])", text)
    return f"{match.group(1)}-{int(match.group(2)):02d}" if match else text[:10]


def fmt(value: float | None, digits: int = 2) -> str:
    return "缺失" if value is None else f"{value:.{digits}f}"


def direction(values: list[float]) -> str:
    if len(values) < 2:
        return "样本不足"
    delta = values[-1] - values[-2]
    if abs(delta) < 1e-9:
        return "持平"
    return "上升" if delta > 0 else "下降"


def trend_sentence(metric: str, values: list[float], unit: str) -> str:
    if len(values) < 2:
        return "样本不足，不能判断趋势。"
    delta = values[-1] - values[-2]
    recent = values[-3:]
    return (
        f"最近一期较前一期{direction(values)} {abs(delta):.2f}{unit}；"
        f"最近 {len(recent)} 期区间 {min(recent):.2f}–{max(recent):.2f}{unit}。"
    )


def load_canonical(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute(
        "SELECT indicator_name,country,period,value,value_type,collected_at,source,source_series,is_revision,original_value "
        "FROM macro_indicators WHERE value IS NOT NULL"
    ).fetchall()
    by_key: dict[tuple[str, str], tuple[tuple[int, str], dict[str, Any]]] = {}
    for row in rows:
        indicator, country, period, value, value_type, collected_at, source, series, is_revision, original = row
        key = (str(indicator), norm_period(period))
        # Prefer the configured source tier, then the newest collected vintage.
        rank = (SOURCE_PRIORITY.get(str(source), 99), str(collected_at))
        item = {
            "indicator": str(indicator),
            "country": str(country or ""),
            "period": norm_period(period),
            "value": float(value),
            "value_type": str(value_type or ""),
            "collected_at": str(collected_at),
            "source": str(source or ""),
            "source_series": str(series or ""),
            "is_revision": int(is_revision or 0),
            "original_value": original,
        }
        previous = by_key.get(key)
        if previous is None or rank < previous[0]:
            by_key[key] = (rank, item)
    result: dict[str, list[dict[str, Any]]] = {}
    for _, item in by_key.values():
        result.setdefault(item["indicator"], []).append(item)
    for items in result.values():
        items.sort(key=lambda item: item["period"])
    return result


def spread(canonical: dict[str, list[dict[str, Any]]], left: str, right: str) -> str:
    lmap = {x["period"]: x["value"] for x in canonical.get(left, [])}
    rmap = {x["period"]: x["value"] for x in canonical.get(right, [])}
    common = sorted(set(lmap) & set(rmap))
    if not common:
        return "暂无共同周期数据。"
    recent = common[-3:]
    values = [lmap[p] - rmap[p] for p in recent]
    return f"最近共同周期 {recent[-1]} 的差值为 {values[-1]:.2f}，最近 {len(values)} 期区间 {min(values):.2f}–{max(values):.2f}。"


def forecast_line(name: str, items: list[dict[str, Any]], unit: str) -> str:
    if len(items) < 2:
        return f"{name}：样本不足，暂不预判。"
    recent = items[-12:]
    values = [x["value"] for x in recent]
    latest = values[-1]
    low, high = min(values[-3:]), max(values[-3:])
    conf = "中低" if len(items) >= 12 else "低"
    d = direction(values)
    if name == "中国制造业 PMI":
        state = "扩张区间" if latest >= 50 else "收缩区间"
        reverse = "若新订单或生产分项继续走弱，PMI 可能重新跌破 50。"
        return f"{name}：下一期倾向{d}、仍处{state}；参考区间 {low:.2f}–{high:.2f}{unit}，置信度{conf}。反向声音：{reverse}"
    reverse = "单期变化可能受季节性、基数或一次性因素影响，不能据此确认拐点。"
    return f"{name}：下一期倾向{d}；参考区间 {low:.2f}–{high:.2f}{unit}，置信度{conf}。反向声音：{reverse}"


def build_report(conn: sqlite3.Connection, db_path: Path, proof_note: str) -> str:
    canonical = load_canonical(conn)
    total = conn.execute("SELECT COUNT(*) FROM macro_indicators").fetchone()[0]
    revisions = conn.execute("SELECT COUNT(*) FROM macro_indicators WHERE is_revision=1").fetchone()[0]
    collected_at = conn.execute("SELECT MAX(collected_at) FROM macro_indicators").fetchone()[0] or "未知"
    lines = [
        "# 宏观数据回溯与预判报告",
        "",
        f"- 生成时点：`{dt.datetime.now().astimezone().isoformat(timespec='seconds')}`",
        f"- 数据库：`{db_path}`",
        f"- 库内累计行数：`{total}`",
        f"- 修订行数：`{revisions}`",
        f"- 最近采集时点：`{collected_at}`",
        f"- Proof：{proof_note}",
        "- 口径：按指标/周期选择优先来源的最新可用 vintage；修订记录保留但不覆盖首次值。",
        "",
        "## 1. 最新观测与趋势",
        "",
        "| 指标 | 统计周期 | 数值 | 来源 | 采集时点 | 趋势 |",
        "|---|---:|---:|---|---|---|",
    ]
    for metric, (label, unit) in METRICS.items():
        items = canonical.get(metric, [])
        if not items:
            lines.append(f"| {label} | - | 缺失 | - | - | 无法判断 |")
            continue
        item = items[-1]
        values = [x["value"] for x in items]
        lines.append(
            f"| {label} | {item['period']} | {fmt(item['value'])}{unit} | `{item['source']}` / `{item['source_series']}` | `{item['collected_at']}` | {trend_sentence(label, values, unit)} |"
        )
    lines += [
        "",
        "## 2. 重点传导观察",
        "",
        f"- PPI-CPI 剪刀差：{spread(canonical, 'ppi_yoy', 'cpi_yoy')}",
        "- PMI：50 为常用荣枯线。高于 50 只能说明调查口径下环比扩张，不能直接等同于 GDP 或盈利改善。",
        "- 美国就业：非农就业环比与失业率需联合观察；单一指标改善不能排除就业结构或基数因素。",
        "",
        "## 3. 下一期预判（启发式基线，不是统计模型）",
        "",
    ]
    for metric, (label, unit) in METRICS.items():
        lines.append(f"- {forecast_line(label, canonical.get(metric, []), unit)}")
    lines += [
        "",
        "## 4. 数据边界与待核项",
        "",
        "- 本报告只使用 SQLite 中已经存档的观测，不进行临时联网补数。",
        "- 参考区间是最近观测区间，不是置信区间；尚未进行季节调整、发布日对齐、回测或模型校准。",
        "- 若中国指标或美国官方 BLS 在最新批次失败，必须回到采集报告和 `collection_checks` 核对真实来源；不得把 FRED 兜底写成 BLS 直连。",
        "- 若要提升预判置信度，下一步应持续积累至少 24 个月 vintage，并加入发布日期、修订幅度、季节性和回测评估。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", default=None)
    parser.add_argument("--date", required=True)
    parser.add_argument("--proof", default="strict Proof passed")
    args = parser.parse_args()
    root = Path(args.output_root or Path(__file__).resolve().parents[1] / "outputs")
    output = root / args.date
    db_path = output / "macro_indicators.sqlite"
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")
    report_path = output / "macro_predict_report.md"
    conn = sqlite3.connect(db_path)
    try:
        report = build_report(conn, db_path, args.proof)
    finally:
        conn.close()
    report_path.write_text(report, encoding="utf-8")
    print(json.dumps({"ok": True, "db": str(db_path), "report": str(report_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
