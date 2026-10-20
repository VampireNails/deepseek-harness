#!/bin/bash
set -euo pipefail
RUNNER_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
  ENV_FILE="${INTEL_RUNNER_ENV_FILE:-/etc/deepseek-harness/.env}"
  if [ -e "$ENV_FILE" ] || [ -n "${INTEL_RUNNER_ENV_FILE:-}" ]; then
    if [ ! -r "$ENV_FILE" ]; then
      echo 'Task runner failed: RUNNER_ENV_UNREADABLE' >&2
      exit 1
    fi
    set -a
    . "$ENV_FILE"
    set +a
  fi
fi
exec node "$RUNNER_DIR/cli.mjs" "$@"
