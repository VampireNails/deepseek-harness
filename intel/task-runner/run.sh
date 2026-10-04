#!/bin/bash
set -euo pipefail
RUNNER_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
  set -a
  . /etc/deepseek-harness/.env
  set +a
fi
exec node "$RUNNER_DIR/cli.mjs" "$@"
