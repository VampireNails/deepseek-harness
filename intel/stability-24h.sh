#!/bin/bash
# 24h 空转存活监控：web-intel 常驻 + 内存监控（非负载稳定性测试，仅进程存活+RSS）
# 用法：./stability-24h.sh start|status|stop
# 日志：/root/intel/stability/monitor.log
set -e
MON_DIR="/root/intel/stability"
PID_FILE="$MON_DIR/web-intel.pid"
LOG="$MON_DIR/monitor.log"

case "$1" in
  start)
    mkdir -p "$MON_DIR"
    if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
      echo "已在运行 (pid $(cat $PID_FILE))"
      exit 0
    fi
    # 启动 web-intel（mock LLM，避免烧真 token）
    cd /root/intel/dsh-fork/packages/test-support/llm-mock-server
    node src/bin.ts --port 18099 --api-key mock-key --sequence success --repeat-last \
      > "$MON_DIR/mock.log" 2>&1 &
    echo $! > "$MON_DIR/mock.pid"
    sleep 2
    DEEPSEEK_BASE_URL=http://127.0.0.1:18099/v1 DEEPSEEK_API_KEY=mock-key \
      nohup dsh --profile web-intel --port 3080 > "$MON_DIR/web.log" 2>&1 &
    echo $! > "$PID_FILE"
    echo "$(date '+%F %T') 启动 web-intel pid=$(cat $PID_FILE)" >> "$LOG"
    # 内存监控：每 5 分钟记一次 RSS
    nohup bash -c "
      while kill -0 $(cat $PID_FILE) 2>/dev/null; do
        RSS=\$(ps -o rss= -p $(cat $PID_FILE) 2>/dev/null | tr -d ' ')
        echo \"\$(date '+%F %T') RSS=\${RSS}KB\" >> '$LOG'
        sleep 300
      done
      echo \"\$(date '+%F %T') 进程退出\" >> '$LOG'
    " > /dev/null 2>&1 &
    echo "已启动，日志：$LOG"
    ;;
  status)
    if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
      echo "运行中 pid=$(cat $PID_FILE)"
      tail -5 "$LOG"
      # 内存趋势
      echo "--- RSS 趋势（最近5条） ---"
      grep "RSS=" "$LOG" | tail -5
    else
      echo "未运行"
      tail -10 "$LOG" 2>/dev/null
    fi
    ;;
  stop)
    [ -f "$PID_FILE" ] && kill $(cat "$PID_FILE") 2>/dev/null || true
    [ -f "$MON_DIR/mock.pid" ] && kill $(cat "$MON_DIR/mock.pid") 2>/dev/null || true
    rm -f "$PID_FILE" "$MON_DIR/mock.pid"
    echo "$(date '+%F %T') 手动停止" >> "$LOG"
    echo "已停止"
    ;;
esac
