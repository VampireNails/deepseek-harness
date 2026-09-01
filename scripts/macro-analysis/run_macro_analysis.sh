#!/usr/bin/env bash
# Macro-analysis Agent: fixed Collect -> Verify -> Clean pipeline.
# No prediction, no LLM step — deterministic data pipeline only.
set -euo pipefail

# 统一使用 managed venv python：唯一具备 nbsc（NBS 官方一手源）/ pandas / statsmodels / mcp 的运行时。
# 注意：裸 python (versions/3.13.12) 无 nbsc，会导致 collect_nbs 静默跳过 NBS 官方一手源，
# 使中国 18 个指标退化为东财第三方——必须用 venv python 跑全部步骤。
VENV_PY="/c/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
GIT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WIN_SCRIPT_DIR="$(cygpath -w "$SCRIPT_DIR")"
OUT_ROOT="${MACRO_OUTPUT_ROOT:-$(cygpath -w "$GIT_ROOT/../../outputs")}"
TODAY="$(date +%Y-%m-%d)"
COLLECTOR="$WIN_SCRIPT_DIR\\collect_macro_data.py"
VERIFIER="$WIN_SCRIPT_DIR\\verify_macro_data.py"
CLEANER="$WIN_SCRIPT_DIR\\clean_macro_data.py"
DISCOVER="$WIN_SCRIPT_DIR\\discover_candidates.py"
PROXY_CONFIG="${MACRO_PROXY_CONFIG:-$SCRIPT_DIR/macro_proxy.env}"

export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*" NODE_OPTIONS=
export MACRO_PROXY_CONFIG="$PROXY_CONFIG"

echo "[1/4] Collect"
"$VENV_PY" "$COLLECTOR" --output-root "$OUT_ROOT" --date "$TODAY"

echo "[2/4] Verify (strict Proof)"
"$VENV_PY" "$VERIFIER" --output-root "$OUT_ROOT" --date "$TODAY" --strict

echo "[3/4] Clean (rebuild macro_clean.sqlite)"
"$VENV_PY" "$CLEANER" --output-root "$OUT_ROOT"

echo "[4/4] Discover (candidate dimensions)"
"$VENV_PY" "$DISCOVER" --output-root "$OUT_ROOT"
