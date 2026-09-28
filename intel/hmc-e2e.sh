#!/bin/bash
# HMC 端到端 API 级验证：TLS → session/create → session/prompt → dsh agent → memory_write → SQLite 落盘
# 用法：HMC_TEST_TOKEN=<token> ./hmc-e2e.sh
#   token 在 /root/intel/hmc-test/token.txt（测试环境）
# 要求：hmc-test profile 已配置，mock LLM 可用
# 数据安全：使用隔离的 DSH_HOME，不碰真实 ~/.dsh
set -e
cd "$(dirname "$0")/.."

export DSH_HOME="$(mktemp -d)"
trap 'rm -rf "$DSH_HOME"' EXIT
echo "隔离 DSH_HOME=$DSH_HOME"

# 重建 hmc-test profile
mkdir -p "$DSH_HOME/profiles/hmc-test"
cp "$HOME/.dsh/profiles/hmc-test/package.json" "$HOME/.dsh/profiles/hmc-test/cordis.yml" "$DSH_HOME/profiles/hmc-test/"
(cd "$DSH_HOME/profiles/hmc-test" && dsh plugin --profile hmc-test install >/dev/null 2>&1)

MOCK_PORT=18093
MOCK_DIR="packages/test-support/llm-mock-server"

# 启动 mock LLM（触发 memory_write）
(node "$MOCK_DIR/src/bin.ts" --port $MOCK_PORT --api-key mock-key \
  --sequence "tool_call_success,success" --repeat-last \
  --tool-name "memory_write" --tool-arguments '{"text":"HMC端到端测试：用户通过手机发送消息","kind":"note"}' \
  > /tmp/hmc-e2e-mock.log 2>&1 &)
MOCK_PID=$!
sleep 2

# 启动 hmc-test（含 HMC service，TLS 43197）；web 端口用 3090 避开稳定性测试的 3080
HMC_CONFIG=/root/intel/hmc-test/hmc-config.json \
DEEPSEEK_BASE_URL=http://127.0.0.1:$MOCK_PORT/v1 DEEPSEEK_API_KEY=mock-key \
  timeout 150 dsh --profile hmc-test --port 3090 > /tmp/hmc-e2e-dsh.log 2>&1 &
DSH_PID=$!
sleep 10

# 等 HMC TLS 端口起来
for i in $(seq 1 10); do
  if (echo > /dev/tcp/127.0.0.1/43197) 2>/dev/null; then break; fi
  sleep 2
done

echo "=== 1. 未带 token 应返回 401 ==="
python3 << 'PYEOF'
import socket, ssl
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
s = socket.create_connection(("100.73.148.102", 43197), timeout=10)
ss = ctx.wrap_socket(s, server_hostname="127.0.0.1")
ss.sendall(b"POST /v1/rpc HTTP/1.0\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
resp = ss.recv(4096).decode()
assert "401" in resp, resp[:200]
print("未带 token → 401 OK")
PYEOF

echo "=== 2. session/create → session/prompt ==="
TOKEN="${HMC_TEST_TOKEN:?HMC_TEST_TOKEN 未设置}"
export HMC_TOKEN="$TOKEN"
python3 << 'PYEOF'
import json, os, urllib.request, ssl
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
token = os.environ["HMC_TOKEN"]
def rpc(endpoint, sessionId=None, args=None):
    body = {"endpoint": endpoint, "args": args or {}}
    if sessionId: body["sessionId"] = sessionId
    req = urllib.request.Request(
        "https://100.73.148.102:43197/v1/rpc",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, context=ctx, timeout=60) as r:
        return json.load(r)
s = rpc("session/create")
sid = s["value"]["sessionId"]
print("session/create →", sid[:8])
r = rpc("session/prompt", sid, {"request": {
    "sessionId": sid, "requestId": "req-e2e-1", "mode": "queue",
    "content": [{"type": "text", "text": "记住：HMC端到端测试"}]}})
print("session/prompt →", json.dumps(r)[:120])
PYEOF

sleep 8  # 等记忆落盘

echo "=== 3. 断言 SQLite 落盘 ==="
python3 -c "
import sqlite3, os
db = sqlite3.connect(os.path.join(os.environ['DSH_HOME'], 'intel-memory/memory.db'))
rows = db.execute(\"SELECT text FROM memories WHERE text LIKE '%HMC端到端%'\").fetchall()
assert rows, 'DB 中没有 HMC 测试记忆'
print('DB 命中:', rows[0][0][:40])
db.execute(\"DELETE FROM memories WHERE text LIKE '%HMC端到端%'\")
db.commit()
print('测试数据已清理')
"

kill $DSH_PID $MOCK_PID 2>/dev/null || true
echo ""
echo "=== HMC 端到端 API 级验证通过 ==="
