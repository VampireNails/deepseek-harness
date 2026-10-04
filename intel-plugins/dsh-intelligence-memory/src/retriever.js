// retriever.js —— 检索契约（可替换）
//
//   add({ text, kind, sessionId }) -> { id }
//   get(id) -> row | null
//   search(query, limit) -> [{ id, text, kind, createdAt }]
//   close()
//
// 当前实现：SQLite FTS5（BM25 排序），零依赖、内存增量可忽略。
// 中文处理：FTS5 默认 unicode61 分词器把连续汉字当一个 token，
// 故入库前按字切分加空格（"我喜欢" -> "我 喜 欢"），查询同样按字 OR，BM25 排序，
// 再加 JS 二次重叠过滤（至少共享 2 个不同汉字），压住单字误召回。
// 实测结论（2026-09-28）：transformers.js + bge-small-zh-v1.5（ONNX）峰值 RSS 约 271MB，
// 与 dsh web（144MB）同进程合计约 374MB，超过迁移期可用内存（约 355MB），故降级为 FTS5。
// Python 退役、内存宽裕后，可实现同样接口的向量检索器直接替换，无需改插件主体.

import { randomUUID } from "node:crypto";

export function spaceCjk(text) {
  return text.replace(/([\p{Script=Han}])/gu, " $1 ").replace(/\s+/g, " ").trim();
}

function buildFtsQuery(query) {
  const spaced = spaceCjk(String(query ?? ""));
  const chars = [];
  for (const m of spaced.matchAll(/[\p{Script=Han}]/gu)) chars.push(m[0]);
  const words = [];
  for (const m of spaced.matchAll(/[A-Za-z0-9]{2,}/g)) words.push(m[0].toLowerCase());
  const uniqChars = [...new Set(chars)].slice(0, 10);
  const uniqWords = [...new Set(words)].slice(0, 6);
  if (uniqChars.length === 0 && uniqWords.length === 0) return null;
  const q = (t) => "\"" + t.replace(/"/g, "\"\"") + "\"";
  return { fts: [...uniqChars, ...uniqWords].map(q).join(" OR "), chars: uniqChars, words: uniqWords };
}

function overlapOk(text, chars, words) {
  if (words.length > 0) {
    const low = text.toLowerCase();
    if (words.some((w) => low.includes(w))) return true;
  }
  if (chars.length === 0) return false;
  let n = 0;
  for (const c of chars) if (text.includes(c)) n++;
  return n >= Math.min(2, chars.length);
}

export class FtsRetriever {
  constructor(db) {
    this.db = db;
    this.insertStmt = db.prepare(
      "INSERT INTO memories (text, kind, created_at, session_id) VALUES (?, ?, ?, ?)"
    );
    this.ftsInsertStmt = db.prepare("INSERT INTO memories_fts(rowid, text) VALUES (?, ?)");
    this.getStmt = db.prepare("SELECT id, text, kind, created_at FROM memories WHERE id = ?");
    this.searchStmt = db.prepare(
      "SELECT m.id, m.text, m.kind, m.created_at " +
      "FROM memories_fts f JOIN memories m ON m.id = f.rowid " +
      "WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) LIMIT ?"
    );
  }

  add({ text, kind = "note", sessionId = null }) {
    if (typeof text !== "string" || text.trim().length === 0) {
      throw new Error("memory text must be a non-empty string");
    }
    const clean = text.trim();
    // A savepoint also composes with a transaction owned by the caller.
    const savepoint = "memory_add_" + randomUUID().replaceAll("-", "");
    this.db.exec("SAVEPOINT " + savepoint);
    try {
      const r = this.insertStmt.run(clean, kind, Date.now(), sessionId);
      const id = Number(r.lastInsertRowid);
      this.ftsInsertStmt.run(id, spaceCjk(clean));
      this.db.exec("RELEASE " + savepoint);
      return { id };
    } catch (error) {
      try { this.db.exec("ROLLBACK TO " + savepoint + "; RELEASE " + savepoint); }
      catch (rollbackError) { throw new AggregateError([error, rollbackError], "memory write rollback failed"); }
      throw error;
    }
  }

  get(id) {
    const row = this.getStmt.get(Number(id));
    return row ?? null;
  }

  search(query, limit = 5) {
    const parsed = buildFtsQuery(query);
    if (!parsed) return [];
    const want = Math.max(1, Math.min(20, limit | 0));
    const rows = this.searchStmt.all(parsed.fts, want * 3 + 5);
    return rows
      .filter((r) => overlapOk(r.text, parsed.chars, parsed.words))
      .slice(0, want)
      .map((r) => ({ id: r.id, text: r.text, kind: r.kind, createdAt: r.created_at }));
  }

  close() {
    this.db.close();
  }
}
