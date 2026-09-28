#!/bin/bash
# dsh-muse 数据迁移：Python 栈 → dsh 智能层
# 用法：./migrate.sh [--dry-run]
# 要求：先备份，Python 栈在迁移期间保持可用（可回退）
set -e

PY_MEM="/opt/deepseek-harness/memory"
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
BACKUP_DIR="/root/intel/backups"
DRY_RUN=0
[ "$1" = "--dry-run" ] && DRY_RUN=1

log() { echo "[migrate] $1"; }
dry() { [ $DRY_RUN -eq 1 ] && echo "[dry-run] $1" || eval "$1"; }

if [ ! -d "$PY_MEM" ]; then
  log "Python memory 目录不存在：$PY_MEM，跳过"
  exit 0
fi

# 1. 备份（迁移前必须）
TS=$(date +%Y%m%d-%H%M%S)
BACKUP="$BACKUP_DIR/memory-$TS.tar.gz"
log "备份 $PY_MEM → $BACKUP"
if [ $DRY_RUN -eq 0 ]; then
  mkdir -p "$BACKUP_DIR"
  tar -czf "$BACKUP" -C "$(dirname "$PY_MEM")" "$(basename "$PY_MEM")"
  echo "$BACKUP" > "$BACKUP_DIR/latest.txt"
else
  echo "[dry-run] 将备份到 $BACKUP"
fi

# 2. 记忆 chunks → dsh memories
log "迁移记忆 chunks → dsh"
dry "python3 - << 'PYEOF'
import sqlite3, os
py_db = sqlite3.connect('$PY_MEM/memory.db')
dsh_db_path = os.path.expandvars('$DSH_HOME/intel-memory/memory.db')
os.makedirs(os.path.dirname(dsh_db_path), exist_ok=True)
# 用 dsh 的 schema 建表（若不存在）
import subprocess
# 直接 SQL 插入，手工双写 memories + memories_fts（store.js 无触发器，与线上写入路径一致）
dsh = sqlite3.connect(dsh_db_path)
dsh.execute('''CREATE TABLE IF NOT EXISTS memories(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL, kind TEXT DEFAULT 'note',
  created_at INTEGER, session_id TEXT)''')
dsh.execute('''CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
  USING fts5(text, tokenize=\"unicode61\")''')
count = 0
for src, text, created_at in py_db.execute('SELECT source, text, created_at FROM chunks'):
    kind = 'note'
    if 'MEMORY.md' in src: kind = 'fact'
    # 去重：相同文本跳过
    if dsh.execute('SELECT 1 FROM memories WHERE text=?', (text,)).fetchone():
        continue
    r = dsh.execute('INSERT INTO memories(text,kind,created_at,session_id) VALUES(?,?,?,?)',
                    (text, kind, int(created_at or 0), None))
    # CJK 空格化（与 retriever.js spaceCjk 等价：只在汉字两侧加空格，拉丁词保持完整）
    import re as _re
    spaced = _re.sub(r'\s+', ' ', _re.sub(r'([一-鿿])', r' \1 ', text)).strip()
    dsh.execute('INSERT INTO memories_fts(rowid,text) VALUES(?,?)', (r.lastrowid, spaced))
    count += 1
dsh.commit()
print(f'迁移 {count} 条记忆')
PYEOF"

# 3. feed.json 合并（Python 格式与 dsh 兼容：f1/f2…）
log "合并 feed.json"
dry "python3 - << 'PYEOF'
import json, os
py_feed = '$PY_MEM/feed.json'
dsh_feed = os.path.expandvars('$DSH_HOME/intel-feed/feed.json')
os.makedirs(os.path.dirname(dsh_feed), exist_ok=True)
py_posts = json.load(open(py_feed)) if os.path.exists(py_feed) else []
dsh_posts = json.load(open(dsh_feed)) if os.path.exists(dsh_feed) else []
seen = {p['id'] for p in dsh_posts}
merged = dsh_posts + [p for p in py_posts if p['id'] not in seen]
# 按 id 数字重排
merged.sort(key=lambda p: int(p['id'][1:]) if p['id'][1:].isdigit() else 0, reverse=True)
merged = merged[:100]
json.dump(merged, open(dsh_feed, 'w'), ensure_ascii=False, indent=2)
print(f'feed 合并：dsh {len(dsh_posts)} + python {len(py_posts)} → {len(merged)}')
PYEOF"

# 4. user_profile.md（若 dsh 侧为空才复制）
log "检查用户画像"
dry "if [ ! -s '$DSH_HOME/intel-profile/user_profile.md' ] && [ -f '$PY_MEM/user_profile.md' ]; then
  mkdir -p '$DSH_HOME/intel-profile'
  cp '$PY_MEM/user_profile.md' '$DSH_HOME/intel-profile/user_profile.md'
  echo '已复制 user_profile.md'
else
  echo 'dsh 画像已存在，跳过'
fi"

# 5. goals/todos/日记 → intel-migration 存档
log "存档 goals/todos/日记"
dry "mkdir -p '$DSH_HOME/intel-migration/diary'
for f in goals.json todos.json goals.md ideas.md; do
  [ -f '$PY_MEM/\$f' ] && cp '$PY_MEM/\$f' '$DSH_HOME/intel-migration/' 2>/dev/null || true
done
cp '$PY_MEM'/2026-*.md '$DSH_HOME/intel-migration/diary/' 2>/dev/null || true
echo '存档完成'"

log "迁移完成。回退用 ./rollback.sh"
