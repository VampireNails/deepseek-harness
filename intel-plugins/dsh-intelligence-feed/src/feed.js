import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

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
// feed.json 是唯一真相源（数组，新在前），上限 100 条，id 为 f1、f2……
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
    writeFileSync(this.file, JSON.stringify(posts, null, 2));
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

  addPost(title, body, source = "") {
    const posts = this.load();
    const pid = this._nextId(posts);
    posts.unshift({ id: pid, ts: ts(), title, body, source });
    posts.length = Math.min(posts.length, MAX_POSTS);
    this._save(posts);
    return pid;
  }

  listPosts(n = 20) {
    return this.load().slice(0, Math.max(0, n));
  }
}
