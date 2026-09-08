#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# dsh Agent (headless) 启动 wrapper —— equity-research / macro-analysis 通用
#
# 存在的理由（每条都对应一个真实踩过的坑）：
#   1. DSH_PERMISSION_MODE 必须设：base profile 默认 workspace-write，工作区外 exe
#      一律拒绝执行（python.exe "Access is denied"）。运行时升级需审批通道，而
#      headless 没有 ⇒ fail-closed。此变量改的是进程后备值，不需要审批通道。
#   2. MSYS 防护：Windows Git Bash 会把 /c/... 错转成 d:\c\...。
#   3. NODE_OPTIONS= ：清掉 safe-delete shim，否则 dsh 启动异常。
#   4. overlay 是 workflow.md 的**生成副本**：改了 workflow 不重新生成，headless 就跑旧指令。
#      ⇒ 本脚本默认每次运行前自动重新生成，杜绝"改了没生效"。
#   5. 每轮全新 DSH_HOME：避免 watcher 文件锁（EPERM）；跑完默认自动清理，不留残留。
#
# 用法：
#   ./run_dsh_agent.sh "任务描述"
#   ./run_dsh_agent.sh --preset macro-analysis "任务描述"
#   ./run_dsh_agent.sh --no-regen "任务描述"      # 跳过 overlay 重新生成
#   ./run_dsh_agent.sh --keep-home "任务描述"     # 保留 DSH_HOME 便于排查
#   ./run_dsh_agent.sh --dry-run "任务描述"       # 只打印命令不执行
# ---------------------------------------------------------------------------
set -euo pipefail

PRESET="equity-research"
REGEN=1
KEEP_HOME=0
DRY_RUN=0
NO_PROMPT=0

while [ $# -gt 0 ]; do
  case "$1" in
    --preset)    PRESET="$2"; shift 2 ;;
    --no-regen)  REGEN=0; shift ;;
    --keep-home) KEEP_HOME=1; shift ;;
    --dry-run)   DRY_RUN=1; shift ;;
    --no-prompt) NO_PROMPT=1; shift ;;
    -h|--help)   sed -n '2,25p' "$0"; exit 0 ;;
    *)           break ;;
  esac
done

TASK="${1:-}"
if [ -z "$TASK" ]; then
  echo "用法: $0 [--preset <id>] [--no-regen] [--keep-home] [--dry-run] \"<任务描述>\"" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GIT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPO_ROOT="$(cd "$GIT_ROOT/../.." && pwd)"          # D:/tmp/deepseek-harness
NODE="/c/Users/Administrator/.workbuddy/binaries/node/versions/22.22.2-2/node.exe"
VENV_PY="/c/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

WIN_SCRIPT_DIR="$(cygpath -w "$SCRIPT_DIR")"
OVERLAY="$SCRIPT_DIR/${PRESET}-headless.yml"
# 传给原生 node 的路径必须是 Windows 格式：MSYS 的 /d/tmp/... 会被 node 解析成 D:\d\tmp\...
CLI="$(cygpath -w "$GIT_ROOT/apps/cli/lib/bin.js")"

# ---- 前置校验（fail loud，不静默降级）----
for f in "$NODE" "$VENV_PY" "$CLI"; do
  [ -e "$f" ] || { echo "[FATAL] 缺失: $f" >&2; exit 3; }
done
[ -d "$HOME/.dsh/.agent-presets/$PRESET" ] || {
  echo "[FATAL] preset 不存在: ~/.dsh/.agent-presets/$PRESET" >&2; exit 3; }

# ---- 1) 自动生成 overlay（保证 workflow.md 的改动生效）----
if [ "$REGEN" -eq 1 ]; then
  echo "[1/3] 重新生成 overlay: ${PRESET}-headless.yml"
  ( cd "$SCRIPT_DIR" && "$VENV_PY" gen_headless_overlay.py --preset "$PRESET" ) \
    || { echo "[FATAL] overlay 生成失败" >&2; exit 4; }
else
  echo "[1/3] 跳过 overlay 生成（--no-regen）"
  [ -f "$OVERLAY" ] || { echo "[FATAL] overlay 不存在: $OVERLAY" >&2; exit 4; }
fi

# ---- 2) 环境与密钥 ----
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*"
export NODE_OPTIONS=
export DSH_TELEMETRY_DISABLED=1
export DSH_PERMISSION_MODE=danger-full-access   # 关键：放行工作区外的 python 等解释器

if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
  # .env 里是非标准键名 "deepseek API key = sk-..."，dsh 不识别，需手动提取
  ENVFILE="$GIT_ROOT/.env"
  [ -f "$ENVFILE" ] || { echo "[FATAL] 未找到 $ENVFILE 且未设 DEEPSEEK_API_KEY" >&2; exit 5; }
  KEY="$(sed -n 's/^[[:space:]]*deepseek API key[[:space:]]*=[[:space:]]*//p' "$ENVFILE" \
         | tr -d '\r"' | sed 's/[[:space:]]*$//')"
  [ -n "$KEY" ] || { echo "[FATAL] .env 中提取不到 deepseek API key" >&2; exit 5; }
  export DEEPSEEK_API_KEY="$KEY"
fi

if [ "$DRY_RUN" -eq 0 ]; then
  DSH_HOME_WIN="D:/tmp/dsh_home/$(date +%Y%m%d-%H%M%S)"
  export DSH_HOME="$DSH_HOME_WIN"
  mkdir -p "$DSH_HOME_WIN"
  if [ "$KEEP_HOME" -eq 0 ]; then
    # 清理失败（目录被 watcher 占用）时告警，不静默放过——避免 D:/tmp/dsh_home 越积越多
    #
    # 注（2026-09-03 实测修正，勿再当 bug 复述）：
    #   曾以为旧写法 `[ -d ... ] && echo ... || true` 会覆盖 `exit "$RC"`、把失败静默成成功。
    #   **已实测证伪**：bash 的 EXIT trap **不会**覆盖显式 `exit N` 的退出码 ——
    #   无 trap / 旧 trap / 新 trap 三对照均正确回传 7（见 outputs/2026-09-03/_verify/trap_rc_test.sh）。
    #   唯一真会吞掉退出码的是 **trap 内部自己调 exit**（`trap 'exit 0' EXIT` 会让 exit 7 变 0）。
    #   保留现写法只为「显式 + 防以后有人往 trap 里加 exit」：进入 trap 先存 $?，末尾显式回传。
    trap 'rc=$?; \
          rm -rf "$DSH_HOME_WIN" 2>/dev/null; \
          if [ -d "$DSH_HOME_WIN" ]; then \
            echo "[WARN] DSH_HOME 清理失败，请手动删除: $DSH_HOME_WIN"; \
          fi; \
          exit $rc' EXIT
  fi
else
  DSH_HOME_WIN="(dry-run 未创建)"
fi

# ---- 3) 运行 ----
OUT_DIR="$REPO_ROOT/outputs/$(date +%Y-%m-%d)"
CMD=("$NODE" "$CLI" --profile headless --patch "$WIN_SCRIPT_DIR\\${PRESET}-headless.yml" "$TASK")

# dry-run 在创建 DSH_HOME 之前退出，避免留下空目录
if [ "$DRY_RUN" -eq 1 ]; then
  echo "[3/3] dry-run（未创建 DSH_HOME，日志目录: $OUT_DIR）"
  printf '  %q' "${CMD[@]}"; echo
  exit 0
fi

mkdir -p "$OUT_DIR"
LOG="$OUT_DIR/dsh_${PRESET}_$(date +%H%M%S).log"

echo "[2/3] preset=$PRESET  DSH_HOME=$DSH_HOME_WIN"
echo "[3/3] 运行 headless（日志: $LOG）"

set +e
"${CMD[@]}" 2>&1 | tee "$LOG"
RC="${PIPESTATUS[0]}"
set -e

echo
echo "--- exit=$RC  日志: $LOG ---"
if [ "$KEEP_HOME" -eq 1 ]; then echo "DSH_HOME 已保留: $DSH_HOME_WIN"; fi
exit "$RC"
