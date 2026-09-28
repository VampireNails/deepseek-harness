#!/bin/bash
# 一键回滚：dsh-prod -> Python harness。用法: ./rollback-full.sh --yes
set -e
[ "$1" = "--yes" ] || { echo "用法: $0 --yes"; exit 1; }
echo "[1/5] 停 dsh-prod..."
systemctl stop dsh-prod.service 2>/dev/null || true
systemctl disable dsh-prod.service 2>/dev/null || true
echo "[2/5] 恢复 Python 数据..."
/root/intel/dsh-fork/intel/rollback.sh --yes
echo "[3/5] 启动 Python harness..."
systemctl start deepseek-harness.service
sleep 8
systemctl is-active deepseek-harness.service
curl -s -o /dev/null -w "python %{http_code}\n" http://127.0.0.1:8080/
echo "[4/5] 恢复 HMC 手动进程（hmc-test :3095+:43197，真 key）..."
set -a; source /etc/deepseek-harness/.env; set +a
HMC_CONFIG=/root/intel/hmc-test/hmc-config.json nohup /usr/local/bin/dsh --profile hmc-test --port 3095 >/tmp/dsh-hmc-rollback.log 2>&1 &
sleep 10
ss -ltn | grep -w 43197 && echo "HMC 43197 OK"
echo "[5/5] 回滚完成。dsh 侧新数据保留在 /root/.dsh/intel-{memory,feed,profile}（需手动确认）。"
