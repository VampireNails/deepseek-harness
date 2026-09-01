#!/usr/bin/env python3
"""End-to-end stdio test for macro_mcp_server.py using the official MCP client.

Verifies: initialize handshake, tools/list, and each tool call returns valid JSON.
Usage: python test_mcp_server.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.types import CallToolResult, TextContent

ROOT = Path(__file__).resolve().parent
SERVER = str(ROOT / "macro_mcp_server.py")
PY = r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

CALLS = [
    ("list_indicators", {}),
    ("get_series", {"indicator": "cpi_yoy", "country": "CN"}),
    ("get_series", {"indicator": "nonfarm_payroll_change", "country": "US", "field": "value_sa"}),
    ("get_latest", {"indicator": "unemployment_rate", "country": "US"}),
    ("get_vintage", {"indicator": "cpi_yoy", "country": "CN", "period": "2024-01"}),
    ("get_metadata", {"indicator": "manufacturing_pmi", "country": "CN"}),
    ("get_source_trust", {}),
    # error path: missing required arg
    ("get_series", {}),
]


async def main() -> int:
    params = StdioServerParameters(
        command=PY, args=[SERVER],
        env={"MACRO_OUTPUT_ROOT": r"D:\tmp\deepseek-harness\outputs"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"[init] server={init.server_info.name} "
                  f"proto={init.protocol_version}")
            tools = await session.list_tools()
            print(f"[list_tools] {len(tools.tools)} tools: "
                  f"{[t.name for t in tools.tools]}")
            ok = 0
            for name, args in CALLS:
                res = await session.call_tool(name, args)
                assert isinstance(res, CallToolResult)
                block = res.content[0] if res.content else None
                text = block.text if isinstance(block, TextContent) else str(block)
                tag = "OK" if not res.is_error else "ERR"
                # parse to ensure valid JSON
                try:
                    payload = json.loads(text)
                    valid = "valid-json"
                except Exception:
                    payload, valid = text[:80], "NOT-json"
                summary = (json.dumps(payload, ensure_ascii=False)[:160]
                           if isinstance(payload, (dict, list)) else payload)
                print(f"[{tag}][{valid}] {name}({args}) -> {summary}")
                if not res.is_error and valid == "valid-json":
                    ok += 1
            print(f"\nPASS {ok}/{len(CALLS)} calls returned valid non-error JSON")
            return 0 if ok == len(CALLS) - 1 else 1  # last call is expected error


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
