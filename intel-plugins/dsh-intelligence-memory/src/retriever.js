// retriever.js —— 检索契约（可替换）
//
//   add({ text, kind, sessionId }) -> { id }
//   get(id) -> row | null
//   search(query, limit) -> [{ id, text, kind, createdAt }]
//   close()
//
// 当前实现：SQLite FTS5 候选及有界词法重排，无向量模型。
// 中文处理：FTS5 默认 unicode61 分词器把连续汉字当一个 token，
// 故入库前按字切分加空格（"我喜欢" -> "我 喜 欢"），查询以文字面 OR 获取候选，
// 再用汉字相邻双字、ASCII 整词及配置的同义概念评分，压住单字误召回。
// 实测结论（2026-09-28）：transformers.js + bge-small-zh-v1.5（ONNX）峰值 RSS 约 271MB，
// 与 dsh web（144MB）同进程合计约 374MB，超过迁移期可用内存（约 355MB），故降级为 FTS5。
// Python 退役、内存宽裕后，可实现同样接口的向量检索器直接替换，无需改插件主体.

import { randomUUID } from "node:crypto";
import { MemoryValidity } from './validity.js';
import { DEFAULT_SYNONYM_GROUPS, DEFAULT_RANKING_LIMITS, DEFAULT_RANKING_RATIOS, DEFAULT_PREFERENCE_SUBJECTS,
  preferencePhrases, validateSynonyms, queryEvidence, rankCandidates } from './ranking.js';

export function spaceCjk(text) {
  return text.replace(/([\p{Script=Han}])/gu, " $1 ").replace(/\s+/g, " ").trim();
}

export const DEFAULT_QUERY_LIMITS = { maxQueryChars: 65536, maxQueryTerms: 2048 };

function queryTooLarge(limit) {
  const error = new RangeError(`Memory query exceeds ${limit}; use a shorter, more specific query.`);
  error.code = 'MEMORY_QUERY_TOO_LARGE';
  return error;
}

function buildFtsQuery(query, limits, groups) {
  const text = String(query ?? "");
  if (text.length > limits.maxQueryChars) throw queryTooLarge(`maxQueryChars=${limits.maxQueryChars}`);
  const spaced = spaceCjk(text);
  const chars = [];
  for (const m of spaced.matchAll(/[\p{Script=Han}]/gu)) chars.push(m[0]);
  const words = [];
  for (const m of spaced.matchAll(/[A-Za-z0-9]{2,}/g)) words.push(m[0].toLowerCase());
  const uniqChars = [...new Set(chars)];
  const uniqWords = [...new Set(words)];
  if (uniqChars.length + uniqWords.length > limits.maxQueryTerms) {
    throw queryTooLarge(`maxQueryTerms=${limits.maxQueryTerms}`);
  }
  const evidence = queryEvidence(text, groups);
  const expanded = spaceCjk(evidence.concepts.flat().join(' '));
  const searchTerms = [...new Set([...uniqChars, ...evidence.words,
    ...(expanded.match(/[\p{Script=Han}]|[A-Za-z0-9]{2,}/gu) ?? []).map(term => term.toLowerCase())])];
  if (searchTerms.length > limits.maxQueryTerms) throw queryTooLarge(`maxQueryTerms=${limits.maxQueryTerms} including synonyms`);
  if (searchTerms.length === 0) return null;
  const q = (t) => "\"" + t.replace(/"/g, "\"\"") + "\"";
  return { fts: searchTerms.map(q).join(" OR "), evidence };
}

export class FtsRetriever {
  constructor(db, limits = {}) {
    this.queryLimits = { ...DEFAULT_QUERY_LIMITS, ...limits };
    this.rankingLimits = { ...DEFAULT_RANKING_LIMITS, ...limits };
    this.rankingRatios = { ...DEFAULT_RANKING_RATIOS, ...limits };
    this.synonyms = validateSynonyms(limits.synonymGroups ?? DEFAULT_SYNONYM_GROUPS);
    this.preferencePhrases = preferencePhrases(limits.preferenceSubjects ?? DEFAULT_PREFERENCE_SUBJECTS);
    for (const key of Object.keys(DEFAULT_RANKING_RATIOS)) {
      const value = this.rankingRatios[key];
      if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1) throw new RangeError(`${key} must be a finite ratio from 0 to 1`);
    }
    for (const key of [...Object.keys(DEFAULT_QUERY_LIMITS), ...Object.keys(DEFAULT_RANKING_LIMITS)]) {
      const value = key in DEFAULT_QUERY_LIMITS ? this.queryLimits[key] : this.rankingLimits[key];
      if (!Number.isSafeInteger(value) || value < 1) {
        throw new RangeError(`${key} must be a positive safe integer`);
      }
    }
    this.db = db;
    this.validity=new MemoryValidity(db,spaceCjk);
    const active=this.validity.prepared?' AND NOT EXISTS (SELECT 1 FROM memory_state s WHERE s.memory_id=memories.id AND s.active=0)':'';
    this.insertStmt = db.prepare(
      "INSERT INTO memories (text, kind, created_at, session_id) VALUES (?, ?, ?, ?)"
    );
    this.ftsInsertStmt = db.prepare("INSERT INTO memories_fts(rowid, text) VALUES (?, ?)");
    this.getStmt = db.prepare("SELECT id, text, kind, created_at FROM memories WHERE id = ?"+active);
    const searchSql = (
      "SELECT m.id, length(CAST(m.text AS BLOB)) AS text_bytes " +
      "FROM memories_fts f JOIN memories m ON m.id = f.rowid " +
      "WHERE memories_fts MATCH ? "+(this.validity.prepared?'AND NOT EXISTS (SELECT 1 FROM memory_state s WHERE s.memory_id=m.id AND s.active=0) ':'')
    );
    this.searchStmt = db.prepare(searchSql + "ORDER BY bm25(memories_fts) LIMIT ?");
    this.preferenceStmt = db.prepare(searchSql + 'AND (' + this.preferencePhrases.map(() => 'instr(m.text, ?) > 0').join(' OR ') + ') ORDER BY bm25(memories_fts) LIMIT ?');
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

  /**
   * Search literal subjects and configured synonyms within query and candidate bounds.
   * @param {string} query Keywords or a question; FTS operators are not interpreted.
   * @param {number} limit Result count, clamped to 1–20.
   * @returns {Array<{id:number,text:string,kind:string,createdAt:number}>} Active originals ranked by literal query evidence.
   * @throws {RangeError} MEMORY_QUERY_TOO_LARGE before SQLite when a query exceeds either bound.
   * @throws {RangeError} MEMORY_RECALL_TOO_LARGE before text fetch when candidate bytes exceed the configured bound.
   */
  search(query, limit = 5) {
    const parsed = buildFtsQuery(query, this.queryLimits, this.synonyms);
    if (!parsed) return [];
    const want = Math.max(1, Math.min(20, limit | 0));
    const metadata = parsed.evidence.personal
      ? this.preferenceStmt.all(parsed.fts, ...this.preferencePhrases, this.rankingLimits.maxCandidates)
      : this.searchStmt.all(parsed.fts, this.rankingLimits.maxCandidates);
    if (metadata.reduce((sum, row) => sum + row.text_bytes, 0) > this.rankingLimits.maxCandidateBytes) {
      const error = new RangeError(`Memory candidates exceed maxCandidateBytes=${this.rankingLimits.maxCandidateBytes}; use a more specific query.`);
      error.code = 'MEMORY_RECALL_TOO_LARGE';
      throw error;
    }
    const hits = metadata.map(row => this.get(row.id)).filter(Boolean)
      .map(row => ({ id: row.id, text: row.text, kind: row.kind, createdAt: row.created_at }));
    return rankCandidates(String(query ?? ''), hits, parsed.evidence, this.rankingRatios).slice(0, want);
  }

  close() {
    this.db.close();
  }

  describe(id) { return this.validity.describe(id); }
  history(id) { return this.validity.history(id); }
  list(options) { return this.validity.list(options); }
  change(args,actor) { return this.validity.change(args,actor); }
}
