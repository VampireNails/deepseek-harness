import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

// 简单 JSON 状态：{ lastAt, lastHash, pendingHash }，5 分钟冷却 + 内容去重 + 并发防重
export class QuietState {
  constructor(dir) {
    this.file = join(dir, "state.json");
    this.data = {};
    try {
      if (existsSync(this.file)) this.data = JSON.parse(readFileSync(this.file, "utf8"));
    } catch {
      this.data = {};
    }
  }
  get(key, fallback) {
    const v = this.data[key];
    return v === undefined ? fallback : v;
  }
  set(key, value) {
    this.data[key] = value;
    this._save();
  }
  delete(key) {
    delete this.data[key];
    this._save();
  }
  _save() {
    try {
      mkdirSync(join(this.file, ".."), { recursive: true });
      writeFileSync(this.file, JSON.stringify(this.data));
    } catch {
      // 状态写失败不影响复盘本身
    }
  }
}
