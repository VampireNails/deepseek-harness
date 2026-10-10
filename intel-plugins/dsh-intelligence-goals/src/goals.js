import { readFileSync,mkdirSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import {readJson,atomicText,writerSync,persistenceError} from '../../shared/json-store.js';
import {intelError} from '../../shared/tool-error.js';

function validData(data){
 if(!data||typeof data!=='object'||Array.isArray(data)||!Array.isArray(data.goals)||!Number.isSafeInteger(data.seq)||data.seq<0)return false;
 const ids=new Set();
 return data.goals.every(g=>{
  if(!g||typeof g!=='object'||!/^g[1-9]\d*$/.test(g.id)||ids.has(g.id)||!Number.isSafeInteger(Number(g.id.slice(1)))||Number(g.id.slice(1))>data.seq||
   typeof g.title!=='string'||!g.title||typeof g.desc!=='string'||!['active','closed'].includes(g.status)||typeof g.created!=='string'||
   !Array.isArray(g.progress)||g.progress.some(p=>!p||typeof p.ts!=='string'||typeof p.text!=='string')||
   (g.status==='closed'&&typeof g.closed!=='string'))return false;
  ids.add(g.id);return true;
 });
}

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
    try{mkdirSync(dir,{recursive:true});}catch(error){throw persistenceError('GOALS','WRITE_FAILED',error);}
  }

  load() {
    return readJson(this.file,()=>({goals:[],seq:0}),validData,'GOALS');
  }

  _save(data) {
    this.load();
    if(!validData(data))throw persistenceError('GOALS','INVALID_DATA');
    atomicText(this.file, JSON.stringify(data, null, 1),'GOALS');
    try{this._renderMd(data);}catch(error){throw persistenceError('GOALS','PROJECTION_FAILED',error,true);}
  }

  rebuildMarkdown(){
    return writerSync(this.dir,'GOALS',()=>this._renderMd(this.load()));
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
    const text=lines.join("\n");
    try{if(readFileSync(this.mdFile,'utf8')===text)return;}
    catch(error){if(error.code!=='ENOENT')throw persistenceError('GOALS','PROJECTION_FAILED',error,true);}
    atomicText(this.mdFile,text,'GOALS');
  }

  create(title, desc = "") {
    title = (title || "").trim();
    if (!title) throw intelError('GOALS_INVALID_TITLE');
    return writerSync(this.dir,'GOALS',()=>{
    const data = this.load();
    if(!Number.isSafeInteger(data.seq+1))throw persistenceError('GOALS','ID_EXHAUSTED');
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
    });
  }

  // 按 id 或标题模糊匹配查找。返回 {goal} 或 {error}；找不到/歧义时报错说明，绝不猜。
  findGoal(ref) {
    return this._find(this.load(), ref);
  }

  _find(data, ref) {
    const r = (ref || "").trim().toLowerCase();
    if (!r) return { error: "引用为空。请用 goal_list 查看现有目标后重试。",code:'GOALS_INVALID_REF' };
    for (const g of data.goals) {
      if (g.id.toLowerCase() === r) return { goal: g };
    }
    const hits = data.goals.filter((g) => g.title.toLowerCase().includes(r));
    if (hits.length === 1) return { goal: hits[0] };
    if (hits.length > 1) {
      return {
        code:'GOALS_AMBIGUOUS_REF',
        error:
          `「${ref}」匹配到多个目标，请用 id 精确指定：` +
          hits.map((g) => `${g.title}[${g.id}]`).join("、"),
      };
    }
    return { error: `找不到引用为「${ref}」的目标。请用 goal_list 查看现有目标后重试。`,code:'GOALS_NOT_FOUND' };
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
    if (!text) throw intelError('GOALS_INVALID_PROGRESS');
    return writerSync(this.dir,'GOALS',()=>{
    const data = this.load();
    const { goal, error, code } = this._find(data, ref);
    if (error) throw intelError(code);
    goal.progress.push({ ts: ts(), text });
    this._save(data);
    return goal;
    });
  }

  close(ref) {
    return writerSync(this.dir,'GOALS',()=>{
    const data = this.load();
    const { goal, error, code } = this._find(data, ref);
    if (error) throw intelError(code);
    if (goal.status !== "active") throw intelError('GOALS_ALREADY_CLOSED');
    goal.status = "closed";
    goal.closed = ts();
    this._save(data);
    return goal;
    });
  }
}
