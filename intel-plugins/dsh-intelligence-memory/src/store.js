// store.js — SQLite 存储（node:sqlite 内建，FTS5 已验证可用）
// 数据放在 DSH_HOME 下的自有目录，绝不写进 workspace。
import { DatabaseSync } from "node:sqlite";
import { mkdirSync, openSync, closeSync } from "node:fs";
import { join } from "node:path";
import { createValiditySchema, hasValiditySchema } from './validity.js';

export function openStore(dataDir) {
  mkdirSync(dataDir, { recursive: true, mode:0o700 });
  const databasePath=join(dataDir,'memory.db');let fresh=false;
  try{closeSync(openSync(databasePath,'wx',0o600));fresh=true;}catch(error){if(error.code!=='EEXIST')throw error;}
  const db = new DatabaseSync(databasePath);
  try {
    if(!fresh)hasValiditySchema(db);
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
    if(fresh){
      db.exec('BEGIN IMMEDIATE');
      try{createValiditySchema(db);db.exec('COMMIT');}
      catch(error){db.exec('ROLLBACK');throw error;}
    }
    return db;
  } catch(error) {db.close();throw error;}
}
