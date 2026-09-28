// store.js — SQLite 存储（node:sqlite 内建，FTS5 已验证可用）
// 数据放在 DSH_HOME 下的自有目录，绝不写进 workspace。
import { DatabaseSync } from "node:sqlite";
import { mkdirSync } from "node:fs";
import { join } from "node:path";

export function openStore(dataDir) {
  mkdirSync(dataDir, { recursive: true });
  const db = new DatabaseSync(join(dataDir, "memory.db"));
  db.exec(`
    CREATE TABLE IF NOT EXISTS memories (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      text TEXT NOT NULL,
      kind TEXT NOT NULL DEFAULT 'note',
      created_at INTEGER NOT NULL,
      session_id TEXT
    );
    -- 全文索引表：由 retriever.js 显式维护（中文先按字切分加空格，见 retriever.js）
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text);
  `);
  return db;
}
