// store.js —— job 元数据持久化：DSH_HOME/intel-cron/cron.json（对标 Python 的 memory/custom_cron.json）。
//
// 写入顺序保证 crash 安全：
//   - 创建：先落盘 job 记录，再 append schedule/change。crash 在中间 → 对账补建（只防丢）。
//   - 删除：先从文件删 job，再 append schedule delete。crash 在中间 → 孤儿 schedule
//     会在对账时被回收（只触发一次，不复活）。

import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { homedir } from "node:os";

export function defaultDataDir() {
  return join(process.env.DSH_HOME || join(homedir(), ".dsh"), "intel-cron");
}

export class CronStore {
  constructor(dataDir) {
    this.file = join(dataDir || defaultDataDir(), "cron.json");
  }

  load() {
    try {
      if (!existsSync(this.file)) return [];
      const data = JSON.parse(readFileSync(this.file, "utf-8"));
      return Array.isArray(data) ? data : [];
    } catch {
      return [];
    }
  }

  save(jobs) {
    mkdirSync(dirname(this.file), { recursive: true });
    writeFileSync(this.file, JSON.stringify(jobs, null, 2), "utf-8");
  }
}

/** 分配下一个 cN id（对标 Python _next_id）。 */
export function nextJobId(jobs) {
  let max = 0;
  for (const j of jobs) {
    const m = /^c(\d+)$/.exec(String(j.id || ""));
    if (m) max = Math.max(max, parseInt(m[1], 10));
  }
  return `c${max + 1}`;
}
