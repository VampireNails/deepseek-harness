#!/bin/bash
# cron 插件集成测试：隔离 profile + mock LLM + HMC RPC，真跑 dsh。
# 覆盖：cron_create（每5分钟）→ cron.json 落盘 → session 日志 schedule/change →
# 真等 ~5 分钟 → [SCHEDULE REMINDER] follow-up → cron_list → cron_delete。
# 数据安全：全新隔离 DSH_HOME，不碰生产 profile（hmc-test/web-intel/web）。
# 端口：mock 18094 / web 3091 / HMC TLS 43199（避开生产 43197/3080 与 hmc-e2e 的 3090/43198）。
set -u
cd "$(dirname "$0")/.."
REPO=/root/intel/dsh-fork

export DSH_HOME="$(mktemp -d)"
HMC_DIR=""
cleanup() {
kill ${MOCK_PID:-} ${DSH_PID:-} 2>/dev/null || true
rm -rf "$DSH_HOME" "${HMC_DIR:-}"
}
trap cleanup EXIT
echo "隔离 DSH_HOME=$DSH_HOME"

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); echo " ✅ $1";}
fail() { FAIL=$((FAIL+1)); echo " ❌ $1";}

# ---- 1. 建隔离 profile cron-test ----
mkdir -p "$DSH_HOME/profiles/cron-test"
cat > "$DSH_HOME/profiles/cron-test/package.json" << 'PEOF'
{
"name": "dsh-profile-cron-test",
"private": true,
"dsh": {
"profile": {
"bundles": [
"@deepseek-ai/dsh-base",
"@deepseek-ai/dsh-web-app",
"dsh-intelligence-cron",
"hmc-service-wrapper"
]
}
},
"dependencies": {
"dsh-intelligence-cron": "file:/root/intel/dsh-fork/intel-plugins/dsh-intelligence-cron",
"hmc-service-wrapper": "file:/root/intel/dsh-fork/intel/hmc-service-wrapper"
}
}
PEOF
cat > "$DSH_HOME/profiles/cron-test/cordis.yml" << 'PEOF'
[]
PEOF
cat > "$DSH_HOME/profiles/cron-test/cordis.patch.yml" << 'PEOF'
[]
PEOF
(cd "$DSH_HOME/profiles/cron-test" && npm install --no-audit --no-fund --loglevel=error)
[ -f "$DSH_HOME/profiles/cron-test/node_modules/dsh-intelligence-cron/package.json" ] || { echo "插件安装失败"; exit 1;}
(cd "$DSH_HOME/profiles/cron-test" && DSH_HOME="$DSH_HOME" dsh plugin --profile cron-test install >/dev/null 2>&1)
echo "profile 就绪"

# ---- 2. HMC 测试配置（43199，复用 e2e 的 token/证书）----
# wrapper 以 HMC_CONFIG 所在目录的 workspace/ 为 workspaceRoot，必须预建
HMC_DIR=$(mktemp -d)
mkdir -p "$HMC_DIR/workspace"
HMC_CFG=$HMC_DIR/cron-e2e-hmc.json
cat > $HMC_CFG << 'PEOF'
{
"tokenFile": "/root/intel/hmc-test/token.txt",
"certFile": "/root/intel/hmc-test/cert.pem",
"keyFile": "/root/intel/hmc-test/key.pem",
"port": 43199,
"host": "127.0.0.1"
}
PEOF

MOCK_PORT=18094
MOCK_DIR="$REPO/packages/test-support/llm-mock-server"
start_mock() { # $1=tool-name $2=tool-arguments-json $3=sequence
node "$MOCK_DIR/src/bin.ts" --port $MOCK_PORT --api-key mock-key \
--sequence "$3" --repeat-last \
--tool-name "$1" --tool-arguments "$2" > /tmp/cron-itest-mock.log 2>&1 &
MOCK_PID=$!
sleep 2
}
stop_mock() { kill $MOCK_PID 2>/dev/null || true;}

# ---- 3. 启动 dsh ----
HMC_CONFIG=$HMC_CFG \
DEEPSEEK_BASE_URL=http://127.0.0.1:$MOCK_PORT/v1 DEEPSEEK_API_KEY=mock-key \
timeout 900 dsh --profile cron-test --port 3091 > /tmp/cron-itest-dsh.log 2>&1 &
DSH_PID=$!
for i in $(seq 1 30); do
if (echo > /dev/tcp/127.0.0.1/43199) 2>/dev/null; then break; fi
sleep 2
done
(echo > /dev/tcp/127.0.0.1/43199) 2>/dev/null || { echo "HMC TLS 43199 未起来"; tail -30 /tmp/cron-itest-dsh.log; exit 1;}
echo "dsh + HMC 已启动"

TOKEN=$(cat /root/intel/hmc-test/token.txt)
rpc() { # $1=endpoint $2=sessionId $3=args-json
python3 - "$1" "$2" "$3" << 'PYEOF'
import json, sys, urllib.request, ssl
endpoint, sid, args = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
body = {"endpoint": endpoint, "args": args}
if sid: body["sessionId"] = sid
req = urllib.request.Request("https://127.0.0.1:43199/v1/rpc", data=json.dumps(body).encode(),
    headers={"Authorization": "Bearer " + open("/root/intel/hmc-test/token.txt").read().strip(), "Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(req, context=ctx, timeout=90) as r:
    print(json.dumps(json.load(r)))
PYEOF
}
prompt() { # $1=sessionId $2=text $3=reqId
rpc "session/prompt" "$1" "{\"request\":{\"sessionId\":\"$1\",\"requestId\":\"$3\",\"mode\":\"queue\",\"content\":[{\"type\":\"text\",\"text\":\"$2\"}]}}"
}
session_file() {
find "$DSH_HOME/sessions/" -name "session.v4.jsonl.zstd" -printf "%T@ %p\n" 2>/dev/null | sort -rn | head -1 | cut -d" " -f2-
}
wait_for() { # $1=描述 $2=超时秒 $3…=命令（返回0即成功）
local desc="$1" timeout="$2"; shift 2
for i in $(seq 1 $timeout); do
if "$@" >/dev/null 2>&1; then echo " 等待[$desc] ${i}s OK"; return 0; fi
sleep 1
done
return 1
}

# ---- 4. cron_create（每5分钟）----
SID=$(rpc "session/create" "" "{}" | python3 -c "import json,sys; print(json.load(sys.stdin)['value']['sessionId'])")
echo "session: ${SID:0:8}"
# 主回合请求先于 title 请求到达 mock，tool_call 必须放首位
start_mock "cron_create" '{"name":"喝水提醒","prompt":"提醒我喝水","schedule":"每5分钟"}' "tool_call_success,success"
prompt "$SID" "帮我创建一个每5分钟提醒我喝水的定时任务" "req-cron-1" > /dev/null

if wait_for "cron.json 落盘" 60 python3 -c "
import json, os
jobs = json.load(open(os.path.join(os.environ['DSH_HOME'], 'intel-cron/cron.json')))
assert len(jobs) == 1 and jobs[0]['id'] == 'c1', jobs
print('job:', jobs[0]['id'], jobs[0]['scheduleId'])
"; then pass "cron_create → cron.json"; else fail "cron.json 落盘"; fi

if wait_for "schedule/change 入会话日志" 60 bash -c "
S=\$(find \"$DSH_HOME/sessions/\" -name session.v4.jsonl.zstd -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
[ -n \"\$S\" ] && zstd -d -c \"\$S\" 2>/dev/null | grep -q '\"kind\":\"every\"'"; then
pass "every schedule 入会话日志"
else fail "schedule/change 入会话日志"; fi
stop_mock
# 等待期：纯文本 mock（无工具），让到点 follow-up turn 能正常走完
node "$MOCK_DIR/src/bin.ts" --port $MOCK_PORT --api-key mock-key \
--sequence "success" --repeat-last > /tmp/cron-itest-mock.log 2>&1 &
MOCK_PID=$!
sleep 2
echo "等待每5分钟提醒触发（最长 420s）…"
FOUND=0
for i in $(seq 1 28); do
sleep 15
S=$(session_file)
if [ -n "$S" ] && zstd -d -c "$S" 2>/dev/null | grep -q "SCHEDULE REMINDER"; then
FOUND=1; echo " 触发于约 $((i*15))s"; break
fi
done
if [ "$FOUND" = 1 ]; then pass "到点 [SCHEDULE REMINDER] follow-up"; else fail "5分钟提醒未触发"; fi

# ---- 6. cron_list / cron_delete 往返 ----
stop_mock
start_mock "cron_list" "{}" "tool_call_success,success"
prompt "$SID" "列出我的定时任务" "req-cron-2" > /dev/null
if wait_for "cron_list 输出" 60 bash -c "
S=\$(find \"$DSH_HOME/sessions/\" -name session.v4.jsonl.zstd -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
[ -n \"\$S\" ] && zstd -d -c \"\$S\" 2>/dev/null | grep -q '喝水提醒'"; then
pass "cron_list 可见 c1"
else fail "cron_list 可见 c1"; fi
stop_mock

start_mock "cron_delete" '{"ref":"c1"}' "tool_call_success,success"
prompt "$SID" "删除喝水提醒任务" "req-cron-3" > /dev/null
if wait_for "cron_delete 清理" 60 python3 -c "
import json, os
jobs = json.load(open(os.path.join(os.environ['DSH_HOME'], 'intel-cron/cron.json')))
assert jobs == [], jobs
print('cron.json 已空')
"; then pass "cron_delete 清理记录"; else fail "cron_delete 清理记录"; fi

if wait_for "底层 schedule delete" 60 bash -c "
S=\$(find \"$DSH_HOME/sessions/\" -name session.v4.jsonl.zstd -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
[ -n \"\$S\" ] && zstd -d -c \"\$S\" 2>/dev/null | grep -q '\"operation\":\"delete\"'"; then
pass "底层 schedule delete 已追加"
else fail "底层 schedule delete"; fi

echo ""
echo "=== 结果：$PASS 通过，$FAIL 失败 ==="
[ "$FAIL" -eq 0 ]
