// store.js —— hook 配置持久化：DSH_HOME/intel-hooks/hooks.json（对标 Python 的 workspace/hooks.json）。
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { homedir } from "node:os";

export function defaultDataDir() {
  return join(process.env.DSH_HOME || join(homedir(), ".dsh"), "intel-hooks");
}

export class HookStore {
  constructor(dataDir) {
    this.dataDir = dataDir || defaultDataDir();
    this.file = join(this.dataDir, "hooks.json");
  }

  load() {
    try {
      if (!existsSync(this.file)) return {};
      const data = JSON.parse(readFileSync(this.file, "utf-8"));
      return data && typeof data === "object" && !Array.isArray(data) ? data : {};
    } catch {
      return {};
    }
  }

  save(hooks) {
    mkdirSync(dirname(this.file), { recursive: true });
    writeFileSync(this.file, JSON.stringify(hooks, null, 2), "utf-8");
  }
}
