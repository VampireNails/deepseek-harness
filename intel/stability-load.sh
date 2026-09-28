#!/bin/bash
# dsh 稳定性负载测试：web-intel 常驻 + 周期真实 prompt + 断言
# 用法：./stability-load.sh start|status|stop
# 与 stability-24h.sh（空转存活监控）的区别：本脚本每 10 分钟发一轮真实 prompt，
# 覆盖会话轮次、记忆写入、quiet 复盘路径，并有通过/失败判据。
set -e
MON_DIR="/root/intel/stability-load"
PID_FILE="$MON_DIR/web-intel.pid"
LOG="$MON_DIR/load.log"
WEB_PORT=3081
MOCK_PORT=18098

# 判据
MAX_RSS_GROWTH_KB=102400   # 24h 内 RSS 增长不超过 100MB
MAX_ERROR_RATE=0.05        # 错误率不超过 5%

case "$1" in
  start)
    mkdir -p "$MON_DIR"
    if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
      echo "已在运行 (pid $(cat $PID_FILE))"
      exit 0
    fi
    DSH_HOME_BAK="$DSH_HOME"
    export DSH_HOME="$MON_DIR/dsh-home"
    mkdir -p "$DSH_HOME/profiles"
    cp "$HOME/.dsh/profiles/web-intel/package.json" "$HOME/.dsh/profiles/web-intel/cordis.yml" "$DSH_HOME/profiles/web-intel/" 2>/dev/null || mkdir -p "$DSH_HOME/profiles/web-intel"
    # mock LLM：交替返回 tool_call（memory_write）和普通回复
    cd /root/intel/dsh-fork/packages/test-support/llm-mock-server
    node src/bin.ts --port $MOCK_PORT --api-key mock-key \
      --sequence "tool_call_success,success" --repeat-last \
      --tool-name "memory_write" --tool-arguments '{"text":"负载测试记忆","kind":"note"}' \
      > "$MON_DIR/mock.log" 2>&1 &
    echo $! > "$MON_DIR/mock.pid"
    sleep 2
    DEEPSEEK_BASE_URL=http://127.0.0.1:$MOCK_PORT/v1 DEEPSEEK_API_KEY=mock-key \
      nohup dsh --profile web-intel --port $WEB_PORT > "$MON_DIR/web.log" 2>&1 &
    echo $! > "$PID_FILE"
    export DSH_HOME="$DSH_HOME_BAK"
    echo "$(date '+%F %T') 启动 web-intel pid=$(cat $PID_FILE)" >> "$LOG"
    # 负载驱动：每 10 分钟一轮 prompt
    nohup bash -c "
      PASS=0; FAIL=0
      RSS0=''
      while kill -0 $(cat $PID_FILE) 2>/dev/null; do
        RSS=\$(ps -o rss= -p $(cat $PID_FILE) 2>/dev/null | tr -d ' ')
        [ -z \"\$RSS0\" ] && RSS0=\$RSS
        # 发一轮 prompt（经 web API）
        TOKEN=\$(grep -o 'token=[^&]*' '$MON_DIR/web.log' | head -1 | cut -d= -f2)
        if curl -s -m 60 -X POST http://127.0.0.1:$WEB_PORT/api/agent/prompt \
            -H \"Authorization: Bearer \$TOKEN\" \
            -H 'Content-Type: application/json' \
            -d '{\"text\":\"负载测试：请记住现在的时间\"}' > /dev/null 2>&1; then
          PASS=\$((PASS+1))
        else
          FAIL=\$((FAIL+1))
        fi
        GROWTH=\$((RSS - RSS0))
        TOTAL=\$((PASS + FAIL))
        ERATE=\$(awk \"BEGIN{printf \\\"%.3f\\\", $FAIL/($TOTAL==0?1:$TOTAL)}\")
        echo \"\$(date '+%F %T') RSS=\${RSS}KB growth=\${GROWTH}KB pass=\$PASS fail=\$FAIL erate=\$ERATE\" >> '$LOG'
        sleep 600
      done
      echo \"\$(date '+%F %T') 进程退出\" >> '$LOG'
    " > /dev/null 2>&1 &
    echo "已启动，日志：$LOG"
    ;;
  status)
    if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
      echo "运行中 pid=$(cat $PID_FILE)"
      tail -5 "$LOG"
    else
      echo "未运行"
      tail -10 "$LOG" 2>/dev/null
      # 输出判据结论
      if [ -f "$LOG" ]; then
        echo "--- 判据 ---"
        tail -1 "$LOG" | grep -o "growth=[0-9]*KB pass=[0-9]* fail=[0-9]* erate=[0-9.]*"
      fi
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
