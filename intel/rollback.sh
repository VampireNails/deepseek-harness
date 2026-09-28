#!/bin/bash
# dsh-muse 回退：从备份恢复 Python 栈数据，清空 dsh 侧迁移数据
# 用法：./rollback.sh [--yes]
# 注意：只回退迁移脚本写入的数据，不删用户在 dsh 侧的新数据（需手动确认）
set -e

BACKUP_DIR="/root/intel/backups"
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
PY_MEM="/opt/deepseek-harness/memory"

YES=0
[ "$1" = "--yes" ] && YES=1

if [ ! -f "$BACKUP_DIR/latest.txt" ]; then
  echo "[rollback] 未找到备份记录，无法回退"
  exit 1
fi
BACKUP=$(cat "$BACKUP_DIR/latest.txt")
if [ ! -f "$BACKUP" ]; then
  echo "[rollback] 备份文件不存在：$BACKUP"
  exit 1
fi

echo "[rollback] 将从 $BACKUP 恢复 Python 数据"
if [ $YES -eq 0 ]; then
  read -p "确认回退？(yes/no) " ans
  [ "$ans" = "yes" ] || { echo "已取消"; exit 0; }
fi

# 1. 恢复 Python memory 目录
echo "[rollback] 恢复 $PY_MEM"
tar -xzf "$BACKUP" -C /opt/deepseek-harness --strip-components=1 2>/dev/null || \
  tar -xzf "$BACKUP" -C /tmp/rollback-tmp && cp -r /tmp/rollback-tmp/memory/* "$PY_MEM/"

# 2. 清空 dsh 侧迁移产生的数据（保留目录结构）
echo "[rollback] 清理 dsh 侧迁移数据"
rm -rf "$DSH_HOME/intel-migration"
# 注意：intel-memory / intel-feed / intel-profile 保留——用户可能已在 dsh 侧产生新数据
# 如需完全清空，手动删：
echo "[rollback] 以下目录保留（如需清空请手动删）："
echo "  $DSH_HOME/intel-memory"
echo "  $DSH_HOME/intel-feed"
echo "  $DSH_HOME/intel-profile"

echo "[rollback] 完成。Python 栈数据已恢复。"
