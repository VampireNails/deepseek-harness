#!/bin/bash
# 集成测试：mock LLM + dsh headless 真跑，断言智能层副作用。
# 用法：./run-integration.sh
# 要求：dsh 已安装，mock server 在 packages/test-support/llm-mock-server，
#       python3（sqlite/json 断言），zstd（解会话快照）
# 数据安全：全程使用隔离的 DSH_HOME（mktemp），不碰真实 ~/.dsh。
set -e
cd "$(dirname "$0")/.."

export DSH_HOME="$(mktemp -d)"
trap 'rm -rf "$DSH_HOME"' EXIT
echo "隔离 DSH_HOME=$DSH_HOME"
# 重建 evolve profile：只拷定义文件，用 dsh 自带 install 重建 link（离线可跑）
# 数据目录（intel-memory 等）保持全新隔离，不从真实 ~/.dsh 拷
mkdir -p "$DSH_HOME/profiles/evolve"
cp "$HOME/.dsh/profiles/evolve/package.json" "$HOME/.dsh/profiles/evolve/cordis.yml" "$DSH_HOME/profiles/evolve/"
(cd "$DSH_HOME/profiles/evolve" && dsh plugin --profile evolve install >/dev/null 2>&1)

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
    DEEPSEEK_API_KEY=mock-key timeout 90 dsh --profile "$2" 2>&1 | tail -3
}

latest_session() {
  find "$DSH_HOME/sessions/" -name "session.v4.jsonl.zstd" -newermt "5 minutes ago" -printf "%T@ %p\n" 2>/dev/null | sort -rn | head -1 | cut -d" " -f2-
}

echo "=== 集成测试：memory_write → DB 落盘 ==="
rm -rf "$DSH_HOME/intel-memory"
echo '{"text":"集成测试记忆：用户喜欢喝茶","kind":"preference"}' > /tmp/itest-args.json
start_mock "tool_call_success,success" "memory_write" /tmp/itest-args.json
run_dsh "请记住：用户喜欢喝茶" "evolve"
stop_mock
if python3 -c "
import sqlite3, os
db = sqlite3.connect(os.path.join(os.environ['DSH_HOME'], 'intel-memory/memory.db'))
rows = db.execute(\"SELECT text FROM memories WHERE text LIKE '%喝茶%'\").fetchall()
assert rows, 'DB 中没有喝茶记忆'
print('DB 命中:', rows[0][0][:30])
"; then pass "memory_write 落盘"; else fail "memory_write 落盘"; fi

echo "=== 集成测试：feed_post → feed.json ==="
rm -rf "$DSH_HOME/intel-feed"
echo '{"title":"集成测试简报","body":"正文","source":"测试"}' > /tmp/itest-args.json
start_mock "tool_call_success,success" "feed_post" /tmp/itest-args.json
run_dsh "发一条 Feed 简报" "evolve"
stop_mock
if python3 -c "
import json, os
d = json.load(open(os.path.join(os.environ['DSH_HOME'], 'intel-feed/feed.json')))
assert d[0]['title'] == '集成测试简报', d
print('feed:', d[0]['id'], d[0]['title'])
"; then pass "feed_post 落盘"; else fail "feed_post 落盘"; fi

echo "=== 集成测试：画像在 step 1 注入 ==="
# seed 最小画像（否则插件无数据时跳过注入）
mkdir -p "$DSH_HOME/intel-profile"
printf '# 用户画像（测试）\n\n## 基本信息\n测试用户\n' > "$DSH_HOME/intel-profile/user_profile.md"
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
