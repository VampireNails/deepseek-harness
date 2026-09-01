#!/usr/bin/env bash
# dsh web 检索冒烟测试：验证 --patch web-overlay.yml 后 web_search 真的返回结构化结果
set -uo pipefail
cd "D:/tmp/deepseek-harness/my-deepseek-harness/deepseek-harness"

# 从 .env 提取 key（非标准格式 "deepseek API key = sk-..."）
KEY=$(grep -oE 'sk-[A-Za-z0-9]+' .env | head -1)
if [ -z "$KEY" ]; then echo "未找到 API key"; exit 1; fi
export DEEPSEEK_API_KEY="$KEY"

# fresh DSH_HOME（时间戳，反斜杠路径），避免 watcher 文件锁 EPERM
export DSH_HOME="D:\\tmp\\deepseek-harness\\scripts\\dsh_home_smoke_$(date +%s)"
export DSH_PERMISSION_MODE="danger-full-access"
unset NODE_OPTIONS
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL="*"

TASK='联网搜索 PostgreSQL 数据库设计模式（如范式化、索引设计、分区），返回你实际搜到的 3 条结果，每条附来源 URL。只报告真实搜到的，不要编造。'

node apps/cli/lib/bin.js --profile headless --patch "D:/tmp/deepseek-harness/scripts/web-overlay.yml" "$TASK"
