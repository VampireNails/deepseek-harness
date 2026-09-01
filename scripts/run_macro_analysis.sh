#!/usr/bin/env bash
# Macro-analysis Agent: fixed Collect -> Verify -> Clean pipeline.
# No prediction, no LLM step — deterministic data pipeline only.
set -euo pipefail

PY_BIN="/c/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe"
CLEAN_PY_BIN="/c/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
GIT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WIN_SCRIPT_DIR="$(cygpath -w "$SCRIPT_DIR")"
OUT_ROOT="${MACRO_OUTPUT_ROOT:-$(cygpath -w "$GIT_ROOT/../../outputs")}"
TODAY="$(date +%Y-%m-%d)"
COLLECTOR="$WIN_SCRIPT_DIR\\collect_macro_data.py"
VERIFIER="$WIN_SCRIPT_DIR\\verify_macro_data.py"
CLEANER="$WIN_SCRIPT_DIR\\clean_macro_data.py"
PROXY_CONFIG="${MACRO_PROXY_CONFIG:-$SCRIPT_DIR/macro_proxy.env}"

export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*" NODE_OPTIONS=
export MACRO_PROXY_CONFIG="$PROXY_CONFIG"

echo "[1/3] Collect"
"$PY_BIN" "$COLLECTOR" --output-root "$OUT_ROOT" --date "$TODAY"

echo "[2/3] Verify (strict Proof)"
"$PY_BIN" "$VERIFIER" --output-root "$OUT_ROOT" --date "$TODAY" --strict

echo "[3/3] Clean (rebuild macro_clean.sqlite)"
"$CLEAN_PY_BIN" "$CLEANER" --output-root "$OUT_ROOT"
