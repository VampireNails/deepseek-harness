import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

export function goalsDir() {
  return join(process.env.DSH_HOME || join(homedir(), ".dsh"), "intel-goals");
}

// 只展示最近 N 条进展（存储本身保留全部）
const PROGRESS_DISPLAY_N = 10;

function ts() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// Goals：长期目标追踪（复刻 Python 版 goals.py）。
// goals.json 是唯一真相源（{goals:[], seq}，goal: id/title/desc/status/created/progress[]/closed）。
// 每次变更后自动重写人类可读的 goals.md（## Active / ## Closed 分区）。
export class GoalsStore {
  constructor(dir = goalsDir()) {
    this.dir = dir;
    this.file = join(dir, "goals.json");
    this.mdFile = join(dir, "goals.md");
    mkdirSync(dir, { recursive: true });
  }

  load() {
    try {
      const data = JSON.parse(readFileSync(this.file, "utf8"));
      if (data && Array.isArray(data.goals) && typeof data.seq === "number") return data;
    } catch {}
    return { goals: [], seq: 0 };
  }

  _save(data) {
    writeFileSync(this.file, JSON.stringify(data, null, 1));
    this._renderMd(data);
  }

  _renderMd(data = null) {
    data = data || this.load();
    const lines = ["# Goals", ""];
    const active = data.goals.filter((g) => g.status === "active");
    const done = data.goals.filter((g) => g.status !== "active");
    lines.push(`## Active (${active.length})`);
    for (const g of active) {
      lines.push("", `### ${g.title} [${g.id}]`);
      if (g.desc) lines.push(g.desc);
      lines.push(`- created: ${g.created}`);
      if (g.progress && g.progress.length) {
        lines.push("- progress:");
        for (const p of g.progress.slice(-PROGRESS_DISPLAY_N)) {
          lines.push(`  - ${p.ts}: ${p.text}`);
        }
      }
    }
    if (done.length) {
      lines.push("", `## Closed (${done.length})`);
      for (const g of done) {
        lines.push(`- ${g.title} [${g.id}] (closed ${g.closed || "?"})`);
      }
    }
    lines.push("");
    writeFileSync(this.mdFile, lines.join("\n"));
  }

  create(title, desc = "") {
    title = (title || "").trim();
    if (!title) throw new Error("goal_create: title 不能为空");
    const data = this.load();
    data.seq += 1;
    const gid = `g${data.seq}`;
    data.goals.push({
      id: gid,
      title,
      desc: (desc || "").trim(),
      status: "active",
      created: ts(),
      progress: [],
    });
    this._save(data);
    return gid;
  }

  // 按 id 或标题模糊匹配查找。返回 {goal} 或 {error}；找不到/歧义时报错说明，绝不猜。
  findGoal(ref) {
    return this._find(this.load(), ref);
  }

  _find(data, ref) {
    const r = (ref || "").trim().toLowerCase();
    if (!r) return { error: "引用为空。请用 goal_list 查看现有目标后重试。" };
    for (const g of data.goals) {
      if (g.id.toLowerCase() === r) return { goal: g };
    }
    const hits = data.goals.filter((g) => g.title.toLowerCase().includes(r));
    if (hits.length === 1) return { goal: hits[0] };
    if (hits.length > 1) {
      return {
        error:
          `「${ref}」匹配到多个目标，请用 id 精确指定：` +
          hits.map((g) => `${g.title}[${g.id}]`).join("、"),
      };
    }
    return { error: `找不到引用为「${ref}」的目标。请用 goal_list 查看现有目标后重试。` };
  }

  // status: "active" | "closed" | "all"（默认 all）
  listGoals(status = "all") {
    const gs = this.load().goals;
    if (status === "active") return gs.filter((g) => g.status === "active");
    if (status === "closed") return gs.filter((g) => g.status !== "active");
    return gs;
  }

  logProgress(ref, text) {
    text = (text || "").trim();
    if (!text) throw new Error("goal_progress: text 不能为空");
    const data = this.load();
    const { goal, error } = this._find(data, ref);
    if (error) throw new Error(error);
    goal.progress.push({ ts: ts(), text });
    this._save(data);
    return goal;
  }

  close(ref) {
    const data = this.load();
    const { goal, error } = this._find(data, ref);
    if (error) throw new Error(error);
    if (goal.status !== "active") throw new Error(`${goal.title}[${goal.id}] 已经关闭`);
    goal.status = "closed";
    goal.closed = ts();
    this._save(data);
    return goal;
  }
}
