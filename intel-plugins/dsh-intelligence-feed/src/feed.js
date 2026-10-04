import { mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import { withStoreWriter, atomicJson, publicationMetadata } from '../../shared/persistence.js';
import { DatabaseSync } from 'node:sqlite';

export function feedDir() {
  return join(process.env.DSH_HOME || join(homedir(), ".dsh"), "intel-feed");
}

const MAX_POSTS = 100;

function ts() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// Feed：个人简报流（复刻 Python 版 feed.py）。
// feed.json 是当前展示记录（数组，新在前），上限100；独立history保留已发布幂等身份。
export class FeedStore {
  constructor(dir = feedDir()) {
    this.dir = dir;
    this.file = join(dir, "feed.json");
    mkdirSync(dir, { recursive: true });
  }

  load() {
    try {
      const data = JSON.parse(readFileSync(this.file, "utf8"));
      if (Array.isArray(data)) return data;
    } catch {}
    return [];
  }

  _save(posts) {
    atomicJson(this.file, posts);
  }

  _nextId(posts) {
    let mx = 0;
    for (const p of posts) {
      const pid = String(p.id || "");
      if (pid.startsWith("f") && /^\d+$/.test(pid.slice(1))) {
        mx = Math.max(mx, parseInt(pid.slice(1), 10));
      }
    }
    return "f" + (mx + 1);
  }

  addPost(title, body, source = "", metadata) {
    const identity = publicationMetadata(metadata);
    return withStoreWriter(this.dir, () => {
    // A separately committed index survives a failed JSON write or a failed writer COMMIT.
    // Reconcile the previous JSON before pruning its display window.
    const database = new DatabaseSync(join(this.dir, '.publication-history.sqlite'));
    try {
    const posts = this.load();
    database.exec('CREATE TABLE IF NOT EXISTS publication_history (key TEXT PRIMARY KEY, payload TEXT NOT NULL)');
    const remember = database.prepare('INSERT OR IGNORE INTO publication_history(key,payload) VALUES (?,?)');
    database.exec('BEGIN IMMEDIATE');
    try {
      for (const post of posts) if (post.idempotencyKey) remember.run(post.idempotencyKey, JSON.stringify(post));
      database.exec('COMMIT');
    } catch (error) { database.exec('ROLLBACK'); throw error; }
    if (identity.idempotencyKey) {
      const saved = database.prepare('SELECT payload FROM publication_history WHERE key=?').get(identity.idempotencyKey);
      const previous = saved && JSON.parse(saved.payload);
      if (previous) {
        if (previous.title !== title || previous.body !== body || previous.source !== source ||
            ['taskId','runId','runDate','memoryDirectory'].some(field => previous[field] !== identity[field]) ||
            JSON.stringify(previous.references ?? []) !== JSON.stringify(identity.references ?? [])) throw Error('publication idempotency conflict');
        return previous.id;
      }
    }
    const pid = this._nextId(posts);
    posts.unshift({ id: pid, ts: ts(), title, body, source, ...identity });
    const publication = posts[0];
    posts.length = Math.min(posts.length, MAX_POSTS);
    this._save(posts);
    if (identity.idempotencyKey) remember.run(identity.idempotencyKey, JSON.stringify(publication));
    return pid;
    } finally { database.close(); }
    });
  }

  listPosts(n = 20) {
    return this.load().slice(0, Math.max(0, n));
  }
}
