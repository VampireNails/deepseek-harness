#!/usr/bin/env python3
"""Equity vintage-data MCP server (stdio transport, mcp 2.0 low-level Server).

Exposes the equity fundamental/quotes database (equity_fundamental.sqlite) to
any MCP client — read-only, no network, no predictions.

Tools (each returns a single structured object wrapped in one text block):
  list_tickers      -> {"tickers":[...]}
  list_factors      -> {"factors":[...]}                  (factor_registry)
  get_factor_series -> {"factor","ticker","data":[...]}  (derived_factors)
  get_latest_factor -> {"factor","ticker","latest":[...]}
  get_fundamentals  -> {"ticker","facts":[...]}          (company_facts, optional key filter)
  get_quotes        -> {"ticker","data":[...]}           (daily_quotes)
  get_vintage       -> {"key","ticker","batches":[...]}  (fact/derived across collected_at)
  get_source_trust  -> {"sources":[...]}

Configure the DB path with the EQUITY_DB environment variable.
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

DEFAULT_DB = Path(__file__).resolve().parents[4] / "outputs" / "equity_fundamental.sqlite"
DB = Path(os.environ.get("EQUITY_DB", str(DEFAULT_DB)))
server = Server("equity-vintage-db")


def _conn() -> sqlite3.Connection:
    if not DB.exists():
        raise RuntimeError(f"equity DB not found: {DB}; run equity_fundamental.py backfill first")
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    return c


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    c = _conn()
    try:
        return [dict(r) for r in c.execute(sql, params).fetchall()]
    finally:
        c.close()


TOOLS = [
    Tool(name="list_tickers", description="List all tickers present in the equity vintage DB",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="list_factors", description="List factor registry (key/description/formula/layer/version/notes)",
         inputSchema={"type": "object", "properties": {"layer": {"type": "string"}}}),
    Tool(name="get_factor_series", description="Full time series of one derived factor for a ticker",
         inputSchema={"type": "object", "properties": {
             "factor": {"type": "string"}, "ticker": {"type": "string", "default": "hk03738"}},
             "required": ["factor"]}),
    Tool(name="get_latest_factor", description="Latest batch values for one factor (or all factors if omitted)",
         inputSchema={"type": "object", "properties": {
             "factor": {"type": "string"}, "ticker": {"type": "string", "default": "hk03738"}}}),
    Tool(name="get_fundamentals", description="Raw company facts (observed layer); optional fact_key filter",
         inputSchema={"type": "object", "properties": {
             "ticker": {"type": "string", "default": "hk03738"}, "fact_key": {"type": "string"}}}),
    Tool(name="get_quotes", description="Daily OHLCV quotes (third-party source, flagged in source_trust)",
         inputSchema={"type": "object", "properties": {
             "ticker": {"type": "string", "default": "hk03738"}, "limit": {"type": "integer", "default": 60}}}),
    Tool(name="get_vintage", description="All collected_at batches for one fact or factor key (revision tracing)",
         inputSchema={"type": "object", "properties": {
             "key": {"type": "string"}, "ticker": {"type": "string", "default": "hk03738"}},
             "required": ["key"]}),
    Tool(name="get_source_trust", description="Source trust registry (official vs third_party)",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_panel", description="Panel view: ticker x trade_date x factor (sector-adjusted)",
         inputSchema={"type": "object", "properties": {
             "ticker": {"type": "string", "default": "hk03738"}, "limit": {"type": "integer", "default": 60}}}),
    Tool(name="get_forward_returns", description="Forward return labels (fwd_1d/5d/20d/60d) for factor validation",
         inputSchema={"type": "object", "properties": {
             "ticker": {"type": "string", "default": "hk03738"}, "limit": {"type": "integer", "default": 60}}}),
    Tool(name="get_benchmarks", description="Benchmark index daily data (HSI, HSTECH, etc.)",
         inputSchema={"type": "object", "properties": {
             "index_code": {"type": "string", "default": "HSI"}, "limit": {"type": "integer", "default": 60}}}),
    Tool(name="get_universe", description="Stock universe: tickers, sectors, currencies, fiscal year end",
         inputSchema={"type": "object", "properties": {
             "included_only": {"type": "boolean", "default": True}}}),
    Tool(name="get_sector_map", description="Sector/industry mapping for all tickers",
         inputSchema={"type": "object", "properties": {}}),
]


def _call_tool(name: str, args: dict) -> dict:
    ticker = args.get("ticker", "hk03738")
    if name == "list_tickers":
        out = {"tickers": [r["ticker"] for r in _rows(
            "SELECT DISTINCT ticker FROM company_facts UNION SELECT DISTINCT ticker FROM daily_quotes")]}
    elif name == "list_factors":
        cond, params = "", ()
        if args.get("layer"):
            cond, params = " WHERE layer=?", (args["layer"],)
        out = {"factors": _rows("SELECT factor_key,description,formula,unit,layer,version,notes "
                                "FROM factor_registry" + cond + " ORDER BY layer,factor_key", params)}
    elif name == "get_factor_series":
        out = {"factor": args["factor"], "ticker": ticker, "data": _rows(
            "SELECT period,value,unit,transform,collected_at FROM derived_factors "
            "WHERE factor_key=? AND ticker=? ORDER BY period,collected_at", (args["factor"], ticker))}
    elif name == "get_latest_factor":
        if args.get("factor"):
            out = {"ticker": ticker, "latest": _rows(
                "SELECT factor_key,period,value,unit FROM derived_factors WHERE ticker=? "
                "AND factor_key=? AND collected_at=(SELECT MAX(collected_at) FROM derived_factors "
                "WHERE ticker=?) ORDER BY period,factor_key", (ticker, args["factor"], ticker))}
        else:
            out = {"ticker": ticker, "latest": _rows(
                "SELECT factor_key,period,value,unit FROM derived_factors WHERE ticker=? "
                "AND collected_at=(SELECT MAX(collected_at) FROM derived_factors WHERE ticker=?) "
                "ORDER BY period,factor_key", (ticker, ticker))}
    elif name == "get_fundamentals":
        if args.get("fact_key"):
            out = {"ticker": ticker, "facts": _rows(
                "SELECT fact_key,period,freq,value,unit,source,release_date,collected_at FROM company_facts "
                "WHERE ticker=? AND fact_key=? ORDER BY fact_key,period,collected_at",
                (ticker, args["fact_key"]))}
        else:
            out = {"ticker": ticker, "facts": _rows(
                "SELECT fact_key,period,freq,value,unit,source,release_date,collected_at FROM company_facts "
                "WHERE ticker=? ORDER BY fact_key,period,collected_at", (ticker,))}
    elif name == "get_quotes":
        out = {"ticker": ticker, "data": _rows(
            "SELECT quote_date,open,high,low,close,volume,amount,currency,source FROM daily_quotes "
            "WHERE ticker=? ORDER BY quote_date DESC LIMIT ?", (ticker, int(args.get("limit", 60))))}
    elif name == "get_vintage":
        key = args["key"]
        batches = _rows(
            "SELECT 'fact' AS kind,period,value,unit,source,collected_at FROM company_facts "
            "WHERE fact_key=? AND ticker=? UNION ALL "
            "SELECT 'derived' AS kind,period,value,unit,source,collected_at FROM derived_factors "
            "WHERE factor_key=? AND ticker=? ORDER BY collected_at,period", (key, ticker, key, ticker))
        out = {"key": key, "ticker": ticker, "batches": batches}
    elif name == "get_source_trust":
        out = {"sources": _rows("SELECT source,authority,trust_level,attribution FROM source_trust")}
    elif name == "get_panel":
        out = {"ticker": ticker, "data": _rows(
            "SELECT q.ticker, q.quote_date AS trade_date, q.close, q.open, q.high, q.low, q.volume, "
            "COALESCE(s.sector, 'unknown') AS sector FROM daily_quotes q "
            "LEFT JOIN sector_map s ON q.ticker = s.ticker "
            "WHERE q.ticker=? ORDER BY q.quote_date DESC LIMIT ?",
            (ticker, int(args.get("limit", 60))))}
    elif name == "get_forward_returns":
        out = {"ticker": ticker, "data": _rows(
            "SELECT trade_date, fwd_1d, fwd_5d, fwd_20d, fwd_60d FROM forward_returns "
            "WHERE ticker=? ORDER BY trade_date DESC LIMIT ?",
            (ticker, int(args.get("limit", 60))))}
    elif name == "get_benchmarks":
        idx = args.get("index_code", "HSI")
        out = {"index_code": idx, "data": _rows(
            "SELECT trade_date, open, high, low, close, volume, currency FROM benchmarks "
            "WHERE index_code=? ORDER BY trade_date DESC LIMIT ?",
            (idx, int(args.get("limit", 60))))}
    elif name == "get_universe":
        cond = " WHERE included=1" if args.get("included_only", True) else ""
        out = {"universe": _rows(
            "SELECT ticker, name_zh, name_en, sector, currency, fiscal_year_end, included, reason "
            "FROM universe" + cond + " ORDER BY ticker")}
    elif name == "get_sector_map":
        out = {"sectors": _rows(
            "SELECT ticker, sector, industry, source FROM sector_map ORDER BY ticker")}
    else:
        out = {"error": f"unknown tool: {name}"}
    return out


_TOOL_NAMES = [t.name for t in TOOLS]
_DISPATCH = {n: (lambda a, _n=n: _call_tool(_n, a)) for n in _TOOL_NAMES}


# --------------------------------------------------------------------------
# Low-level request handlers (mcp 2.0 contract, mirrors macro_mcp_server.py)
# --------------------------------------------------------------------------

def _json_default(o):
    if isinstance(o, (sqlite3.Row,)):
        return dict(o)
    raise TypeError(f"not JSON serializable: {type(o)}")


async def _list_tools(_ctx, _params: PaginatedRequestParams) -> ListToolsResult:
    return ListToolsResult(tools=TOOLS)


async def _handle_call(_ctx, params: CallToolRequestParams) -> CallToolResult:
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
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=_json_default))],
        is_error=False,
        structured_content=result,
    )


server.add_request_handler("tools/list", PaginatedRequestParams, _list_tools)
server.add_request_handler("tools/call", CallToolRequestParams, _handle_call)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

async def _amain() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_amain())
