#!/bin/bash
# 集成测试：mock LLM + dsh headless 真跑，断言智能层副作用。
# 用法：./run-integration.sh
# 要求：dsh 已安装，mock server 在 packages/test-support/llm-mock-server
set -e
cd "$(dirname "$0")/.."

MOCK_PORT=18090
MOCK_DIR="packages/test-support/llm-mock-server"
PASS=0; FAIL=0

pass() { PASS=$((PASS+1)); echo "  ✅ $1"; }
fail() { FAIL=$((FAIL+1)); echo "  ❌ $1"; }

# start_mock <sequence> [tool_name] [tool_arguments_json_file]
start_mock() {
  local args=(node "$MOCK_DIR/src/bin.ts" --port $MOCK_PORT --api-key mock-key
    --sequence "$1" --repeat-last)
  if [ -n "$2" ]; then
    args+=(--tool-name "$2" --tool-arguments "$(cat "$3")")
  fi
  "${args[@]}" > /tmp/itest-mock.log 2>&1 &
  MOCK_PID=$!
  sleep 2
}

stop_mock() { kill $MOCK_PID 2>/dev/null || true; }

run_dsh() {
  echo "$1" | DEEPSEEK_BASE_URL=http://127.0.0.1:$MOCK_PORT/v1 \
    DEEPSEEK_API_KEY=mock-key timeout 120 dsh --profile "$2" > /tmp/itest-dsh.log 2>&1 || true
}

latest_session() {
  find ~/.dsh/sessions/ -name "session.v4.jsonl.zstd" -newermt "5 minutes ago" 2>/dev/null | head -1
}

echo "=== 集成测试：memory_write → DB 落盘 ==="
rm -rf ~/.dsh/intel-memory
echo '{"text":"集成测试记忆：用户喜欢喝茶","kind":"preference"}' > /tmp/itest-args.json
start_mock "tool_call_success,success" "memory_write" /tmp/itest-args.json
run_dsh "请记住：用户喜欢喝茶" "evolve"
stop_mock
if python3 -c "
import sqlite3, os
db = sqlite3.connect(os.path.expandvars('\$HOME/.dsh/intel-memory/memory.db'))
rows = db.execute(\"SELECT text FROM memories WHERE text LIKE '%喝茶%'\").fetchall()
assert rows, 'DB 中没有喝茶记忆'
print('DB 命中:', rows[0][0][:30])
"; then pass "memory_write 落盘"; else fail "memory_write 落盘"; fi

echo "=== 集成测试：feed_post → feed.json ==="
rm -rf ~/.dsh/intel-feed
echo '{"title":"集成测试简报","body":"正文","source":"测试"}' > /tmp/itest-args.json
start_mock "tool_call_success,success" "feed_post" /tmp/itest-args.json
run_dsh "发一条 Feed 简报" "evolve"
stop_mock
if python3 -c "
import json, os
d = json.load(open(os.path.expandvars('\$HOME/.dsh/intel-feed/feed.json')))
assert d[0]['title'] == '集成测试简报', d
print('feed:', d[0]['id'], d[0]['title'])
"; then pass "feed_post 落盘"; else fail "feed_post 落盘"; fi

echo "=== 集成测试：画像在 step 1 注入 ==="
start_mock "success"
run_dsh "你好" "evolve"
stop_mock
S=$(latest_session)
if zstd -d -c "$S" 2>/dev/null | python3 -c "
import json, sys
found = False
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    e = json.loads(line)
    if e.get('type') == 'user/message' and e.get('data', {}).get('source', {}).get('kind') == 'dsh-intelligence-profile':
        found = True; break
assert found, '未找到画像注入消息'
print('画像注入 found')
"; then pass "画像注入"; else fail "画像注入"; fi

echo ""
echo "=== 结果：$PASS 通过，$FAIL 失败 ==="
[ "$FAIL" -eq 0 ]
