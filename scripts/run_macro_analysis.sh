#!/usr/bin/env bash
# Macro-analysis Agent: fixed Collect -> Verify -> Analyze/Predict pipeline.
set -euo pipefail

NODE_BIN="${NODE_BIN:-/c/Users/Administrator/.workbuddy/binaries/node/versions/22.22.2/node.exe}"
PY_BIN="${PY_BIN:-/c/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe}"
GIT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WIN_GIT_ROOT="$(cygpath -w "$GIT_ROOT")"
WIN_SCRIPT_DIR="$(cygpath -w "$SCRIPT_DIR")"
OUT_ROOT="${MACRO_OUTPUT_ROOT:-$(cygpath -w "$GIT_ROOT/outputs")}"
TODAY="$(date +%Y-%m-%d)"
COLLECTOR="$WIN_SCRIPT_DIR\\collect_macro_data.py"
VERIFIER="$WIN_SCRIPT_DIR\\verify_macro_data.py"
ANALYZER="$WIN_SCRIPT_DIR\\analyze_macro_data.py"
OVERLAY="$WIN_SCRIPT_DIR\\macro-analysis-headless.yml"
PROXY_CONFIG="${MACRO_PROXY_CONFIG:-$SCRIPT_DIR/macro_proxy.env}"

export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*" NODE_OPTIONS=
export DSH_TELEMETRY_DISABLED=1 DSH_PERMISSION_MODE=danger-full-access
export MACRO_PROXY_CONFIG="$PROXY_CONFIG"

if [ -f "$GIT_ROOT/.env" ]; then
  RAW_KEY="$(grep -iE '^[[:space:]]*deepseek[[:space:]]+API[[:space:]]+key' "$GIT_ROOT/.env" | head -1 | sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]+$//' || true)"
  [ -n "$RAW_KEY" ] && export DEEPSEEK_API_KEY="$RAW_KEY"
fi

if [ "${1:-}" = "--web" ] || [ "${1:-}" = "web" ]; then
  # Web discovers the installed preset; headless mode uses the repo workflow source.
  cd "$GIT_ROOT"
  exec "$NODE_BIN" "$GIT_ROOT/apps/cli/lib/bin.js" web
fi

TASK="${1:-基于本次固定采集结果完成宏观数据回溯与预判。先读取 Proof 结果；若 Proof 未通过，停止分析并报告缺口。若通过，分析中国 CPI/PPI/制造业 PMI 与美国非农/失业率，所有数字标注指标、统计周期、来源、采集时点，给出观点、反向声音和置信度；数据不足不得编造。把最终报告写入 outputs/${TODAY}/macro_predict_report.md。}"

"$PY_BIN" "$SCRIPT_DIR/gen_headless_overlay.py"
mkdir -p "$OUT_ROOT/$TODAY"

echo "[1/3] Collect"
"$PY_BIN" "$COLLECTOR" --output-root "$OUT_ROOT" --date "$TODAY"

echo "[2/4] Verify (strict Proof)"
"$PY_BIN" "$VERIFIER" --output-root "$OUT_ROOT" --date "$TODAY" --strict

echo "[3/4] Build traceable baseline report"
"$PY_BIN" "$ANALYZER" --output-root "$OUT_ROOT" --date "$TODAY" --proof "strict Proof passed"

echo "[4/4] Agent review + Predict"
DSH_HOME="$(cygpath -w "$GIT_ROOT/.dsh_home/macro-$(date +%Y%m%d-%H%M%S)")" \
  "$NODE_BIN" "$GIT_ROOT/apps/cli/lib/bin.js" --profile headless --patch "$OVERLAY" "$TASK"
