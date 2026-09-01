#!/usr/bin/env python3
"""Macro clean-data MCP server (stdio transport, mcp 2.0 low-level Server).

Exposes the cleaned macro database (macro_clean.sqlite) built by
clean_macro_data.py to any MCP client (Claude Desktop / Cline / Cursor ...).

Transport: local stdio (spawned by the client as a subprocess).
No network, no predictions, no self-learning — read-only access to the
cleaned vintage macro dataset.

Tools (each returns a single structured object wrapped in one text block):
  list_indicators  -> {"indicators": [...]}
  get_series       -> {"indicator","country","field","data":[...]}
  get_latest       -> {"indicator","country","latest":[...]}
  get_vintage      -> {"indicator","period","country","revisions":[...]}
  get_metadata     -> {"indicator","country","metadata":[...]}

Configure the DB path with the MACRO_CLEAN_DB environment variable.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

OUT_ROOT = Path(os.environ.get("MACRO_OUTPUT_ROOT", str(Path(__file__).resolve().parents[1] / "outputs")))
DEFAULT_DB = OUT_ROOT / "macro_clean.sqlite"
DB = Path(os.environ.get("MACRO_CLEAN_DB", str(DEFAULT_DB)))

server = Server("macro-clean-db")

# --------------------------------------------------------------------------
# DB access
# --------------------------------------------------------------------------

VALID_FIELDS = ("value", "value_sa", "value_imputed")


def _conn() -> sqlite3.Connection:
    if not DB.exists():
        raise RuntimeError(f"clean DB not found: {DB}; run clean_macro_data.py first")
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    return c


def _json_default(o):
    # sqlite3.Row already converted to dict; this guards any stray types.
    if isinstance(o, (sqlite3.Row,)):
        return dict(o)
    raise TypeError(f"not JSON serializable: {type(o)}")


# --------------------------------------------------------------------------
# Tool implementations (return plain dicts)
# --------------------------------------------------------------------------

def _list_indicators() -> dict:
    c = _conn()
    try:
        rows = c.execute(
            "SELECT indicator,country,label,unit,frequency,sa_method,"
            "first_period,last_period,n_obs,n_imputed,last_updated FROM indicators "
            "ORDER BY country,indicator").fetchall()
        return {"indicators": [dict(r) for r in rows]}
    finally:
        c.close()


def _get_series(args: dict) -> dict:
    indicator = args.get("indicator")
    if not indicator:
        raise ValueError("indicator is required")
    country = (args.get("country") or "").strip()
    field = args.get("field") or "value"
    if field not in VALID_FIELDS:
        field = "value"
    start = args.get("start") or ""
    end = args.get("end") or ""
    c = _conn()
    try:
        q = ("SELECT period,country,value,value_sa,value_imputed,is_imputed,source,"
             "layer,derived_from,transform FROM clean_series WHERE indicator=?")
        a: list = [indicator]
        if country:
            q += " AND country=?"; a.append(country)
        if start:
            q += " AND period>=?"; a.append(start)
        if end:
            q += " AND period<=?"; a.append(end)
        q += " ORDER BY country,period"
        rows = c.execute(q, a).fetchall()
        data = [{
            "period": r["period"], "country": r["country"], "value": r[field],
            "is_imputed": r["is_imputed"], "source": r["source"],
            "layer": r["layer"], "derived_from": r["derived_from"], "transform": r["transform"],
        } for r in rows]
        return {"indicator": indicator, "country": country or "ALL",
                "field": field, "data": data}
    finally:
        c.close()


def _get_latest(args: dict) -> dict:
    indicator = args.get("indicator")
    if not indicator:
        raise ValueError("indicator is required")
    country = (args.get("country") or "").strip()
    c = _conn()
    try:
        q = ("SELECT country,period,value,value_sa,value_imputed,is_imputed,source,"
             "release_date,collected_at FROM clean_series WHERE indicator=?")
        a: list = [indicator]
        if country:
            q += " AND country=?"; a.append(country)
        q += " ORDER BY country,period DESC"
        rows = c.execute(q, a).fetchall()
        # keep only the latest per country
        best: dict[str, sqlite3.Row] = {}
        for r in rows:
            best.setdefault(r["country"], r)
        latest = [dict(best[k]) for k in sorted(best)]
        return {"indicator": indicator, "country": country or "ALL", "latest": latest}
    finally:
        c.close()


def _get_vintage(args: dict) -> dict:
    indicator = args.get("indicator")
    period = args.get("period")
    if not indicator:
        raise ValueError("indicator is required")
    if not period:
        raise ValueError("period is required (e.g. 2024-01)")
    country = (args.get("country") or "").strip()
    c = _conn()
    try:
        q = ("SELECT country,collected_at,value,original_value,is_revision,source,value_type "
             "FROM vintage_traces WHERE indicator=? AND period=?")
        a: list = [indicator, period]
        if country:
            q += " AND country=?"; a.append(country)
        q += " ORDER BY country,collected_at"
        rows = c.execute(q, a).fetchall()
        revisions = [dict(r) for r in rows]
        return {"indicator": indicator, "period": period,
                "country": country or "ALL", "revisions": revisions}
    finally:
        c.close()


def _get_metadata(args: dict) -> dict:
    indicator = args.get("indicator")
    if not indicator:
        raise ValueError("indicator is required")
    country = (args.get("country") or "").strip()
    c = _conn()
    try:
        q = ("SELECT indicator,country,label,unit,frequency,sa_method,"
             "first_period,last_period,n_obs,n_imputed,last_updated "
             "FROM indicators WHERE indicator=?")
        a: list = [indicator]
        if country:
            q += " AND country=?"; a.append(country)
        q += " ORDER BY country"
        rows = c.execute(q, a).fetchall()
        return {"indicator": indicator, "country": country or "ALL",
                "metadata": [dict(r) for r in rows]}
    finally:
        c.close()


def _get_source_trust() -> dict:
    c = _conn()
    try:
        rows = c.execute(
            "SELECT source,authority,trust_level,attribution,priority "
            "FROM source_trust ORDER BY priority").fetchall()
        return {"sources": [dict(r) for r in rows]}
    finally:
        c.close()


def _get_derivation(args: dict) -> dict:
    """返回某指标的派生血缘：layer / derived_from / transform / 版本 / 计算时点，
    以及观测值 vs 公式派生值的一致性对账（derived_checks）。数据基石透明度工具。"""
    indicator = args.get("indicator")
    if not indicator:
        raise ValueError("indicator is required")
    country = (args.get("country") or "").strip()
    c = _conn()
    try:
        q = ("SELECT DISTINCT layer, derived_from, transform, transform_version, computed_at "
             "FROM clean_series WHERE indicator=?")
        a: list = [indicator]
        if country:
            q += " AND country=?"; a.append(country)
        rows = c.execute(q, a).fetchall()
        lineage = [dict(r) for r in rows]
        cc = c.execute(
            "SELECT country,period,observed_value,derived_value,delta,abs_delta,consistent,"
            "transform,transform_version FROM derived_checks WHERE indicator=?", (indicator,)).fetchall()
        checks = [dict(r) for r in cc]
        return {"indicator": indicator, "country": country or "ALL",
                "lineage": lineage, "consistency_checks": checks}
    finally:
        c.close()


# --------------------------------------------------------------------------
# Tool registry (name -> (handler, description, input_schema))
# --------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="list_indicators",
        description="列出清洗库中可用的宏观指标（中美 CPI/PPI/PMI/非农/失业率）。返回 indicators 列表，每项含标签、单位、频率、季节调整方法、覆盖区间与观测数。",
        input_schema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_series",
        description="获取某指标的规范月度时序。field 默认 value(原始规范观测，始终有值)；可选 value_sa(季节调整后/插补回退) / value_imputed(插补填充值)。start/end 形如 2024-01。country 省略时返回所有国家。返回 data 每项含 layer(observed=官方出版 / derived=公式派生兜底) 与 derived_from/transform 血缘，供消费方判断数据性质。",
        input_schema={
            "type": "object",
            "properties": {
                "indicator": {"type": "string", "description": "指标键，如 cpi_yoy / nonfarm_payroll_change"},
                "country": {"type": "string", "description": "国家代码 CN/US，省略则返回所有国家", "default": "CN"},
                "field": {"type": "string", "enum": ["value", "value_sa", "value_imputed"], "default": "value"},
                "start": {"type": "string", "description": "起始期 YYYY-MM"},
                "end": {"type": "string", "description": "结束期 YYYY-MM"},
            },
            "required": ["indicator"],
        },
    ),
    Tool(
        name="get_latest",
        description="获取某指标最新一期（每匹配国家一条）：含原始/季节调整后/插补值、来源、发布日、采集时点。",
        input_schema={
            "type": "object",
            "properties": {
                "indicator": {"type": "string"},
                "country": {"type": "string", "description": "国家代码，省略则返回所有国家最新值", "default": "CN"},
            },
            "required": ["indicator"],
        },
    ),
    Tool(
        name="get_vintage",
        description="获取某指标在某发布周期的历史采集快照（vintage）：每次采集的 collected_at、值、是否修订、初值、来源。免费源返回稳定修订值，故记录每次采集的值变化点。",
        input_schema={
            "type": "object",
            "properties": {
                "indicator": {"type": "string"},
                "period": {"type": "string", "description": "发布周期 YYYY-MM"},
                "country": {"type": "string", "description": "国家代码，省略则返回所有国家", "default": "CN"},
            },
            "required": ["indicator", "period"],
        },
    ),
    Tool(
        name="get_metadata",
        description="获取某指标元数据：标签、单位、频率、季节调整方法、覆盖区间、观测数、插补数、最后更新时间。",
        input_schema={
            "type": "object",
            "properties": {
                "indicator": {"type": "string"},
                "country": {"type": "string", "description": "国家代码，省略则返回所有国家", "default": "CN"},
            },
            "required": ["indicator"],
        },
    ),
    Tool(
        name="get_source_trust",
        description="列出数据源可信度分级（官方一手 > 官方二次 > 第三方），含 authority/attribution/priority。用于判断某指标是否来自官方第一手数据。",
        input_schema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_derivation",
        description="返回某指标的派生血缘与一致性对账（数据基石透明度）：layer(observed/derived)、derived_from(底层源指标)、transform(变换id)、transform_version(版本)、computed_at(计算时点)；若该派生指标有官方出版值，同时返回 derived_checks 里「观测值 vs 公式派生值」的偏差与一致性标记。用于确认某个同比/环比/变化量是否为官方出版、还是公式兜底。",
        input_schema={
            "type": "object",
            "properties": {
                "indicator": {"type": "string", "description": "指标键，如 m2_yoy / gdp_qoq / nonfarm_payroll_change"},
                "country": {"type": "string", "description": "国家代码，省略则返回所有国家", "default": "CN"},
            },
            "required": ["indicator"],
        },
    ),
]

_DISPATCH = {
    "list_indicators": lambda a: _list_indicators(),
    "get_series": _get_series,
    "get_latest": _get_latest,
    "get_vintage": _get_vintage,
    "get_metadata": _get_metadata,
    "get_source_trust": lambda a: _get_source_trust(),
    "get_derivation": _get_derivation,
}


# --------------------------------------------------------------------------
# Low-level request handlers (mcp 2.0 contract)
# --------------------------------------------------------------------------

async def _list_tools(_ctx, _params: PaginatedRequestParams) -> ListToolsResult:
    return ListToolsResult(tools=TOOLS)


async def _call_tool(_ctx, params: CallToolRequestParams) -> CallToolResult:
    name = params.name
    args = params.arguments or {}
    handler = _DISPATCH.get(name)
    if handler is None:
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(
                {"error": f"unknown tool: {name}"}, ensure_ascii=False))],
            is_error=True,
        )
    try:
        result = handler(args)
    except Exception as e:  # surface errors to the client instead of crashing
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(
                {"error": str(e)}, ensure_ascii=False))],
            is_error=True,
        )
    text = json.dumps(result, ensure_ascii=False, default=_json_default)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        is_error=False,
        structured_content=result,
    )


server.add_request_handler("tools/list", PaginatedRequestParams, _list_tools)
server.add_request_handler("tools/call", CallToolRequestParams, _call_tool)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

async def _main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
