// manager.js —— hook 注册、prompt 渲染、触发（加锁防并发）与 inbox 轮询。
//
// 复刻 Python 版 agent/hooks.py 的语义：
// - hook = name → { trigger, prompt 模板, desc}，持久化到 hooks.json（defaults 内置）
// - trigger 类型：file（轮询 inbox/ 目录，新文件触发）/ webhook（供外部手动 fire）
// - fire(name)：按 prompt 模板渲染生成任务，加锁防并发串行执行
// - watcher tick：定时扫描 file 触发器目录，name:filename → mtime 去重，只触发一次
//
// 与 Python 的差异（见 README）：dsh 插件侧的"执行"只产出渲染好的任务描述文本
// （内存任务队列 + fires.jsonl），由消费方（如 evolve/heartbeat）按需取用并实际执行。

import {
readdirSync,
statSync,
existsSync,
mkdirSync,
appendFileSync,
readFileSync,
writeFileSync,
} from "node:fs";
import { join, relative} from "node:path";

export const DEFAULT_POLL_INTERVAL_MS = 60000;
export const MAX_HOOKS = 50;

const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

/** 内置 inbox hook（复刻 Python 版默认配置）。 */
export function defaultHooks() {
return {
inbox: {
trigger: { type: "file", dir: "inbox"},
prompt:
"inbox got a new file: {path}. Read it, " +
"figure out what to do and act; " +
"log the result to the daily note.",
desc: "auto-process new files in inbox",
},
};
}

/** 渲染 prompt 模板里的 {key} 占位（复刻 Python _render）。缺键时退化为原文 + [context] 追加。 */
export function renderTemplate(template, ctx) {
const missing = new Set();
const text = String(template?? "").replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (m, key) => {
if (ctx && Object.prototype.hasOwnProperty.call(ctx, key) && ctx[key]!== undefined) {
return String(ctx[key]);
}
missing.add(key);
return m;
});
if (missing.size > 0) {
const dump = JSON.stringify(ctx?? {}, null, 2).slice(0, 2000);
return `${text}\n\n[context]\n${dump}`;
}
return text;
}

function checkName(name) {
const n = (name || "").trim();
if (!NAME_RE.test(n)) throw new Error("hook 名称非法：只允许字母/数字/_/-，1-64 字符。");
return n;
}

export class HookManager {
constructor({ store, ctx, pollIntervalMs} = {}) {
if (!store) throw new Error("HookManager 需要 store");
this.store = store;
this.ctx = ctx;
this.dataDir = store.dataDir;
this.pollIntervalMs =
Number.isSafeInteger(pollIntervalMs) && pollIntervalMs > 0
? pollIntervalMs
: DEFAULT_POLL_INTERVAL_MS;
this.stateFile = join(this.dataDir, "hook_state.json");
this.fireLogFile = join(this.dataDir, "fires.jsonl");
this._timer = null;
this._locks = new Map(); // name -> promise tail（加锁防并发）
this._delivered = []; // 内存任务队列：{ at, hook, source, path, prompt}
}

warn(msg) {
try {
this.ctx?.logger?.warn?.(`dsh-intelligence-hooks: ${msg}`);
} catch {
/* ignore */
}
}

/** 内置 + 自定义（自定义同名覆盖内置）。 */
list() {
return {...defaultHooks(),...this.store.load()};
}

register(name, { trigger, prompt, desc} = {}) {
const n = checkName(name);
if (!trigger || typeof trigger!== "object" || Array.isArray(trigger)) {
throw new Error("trigger 必须是对象，如 {type:'file',dir:'inbox'} 或 {type:'webhook'}。");
}
const type = trigger.type;
if (type!== "file" && type!== "webhook") {
throw new Error(`trigger.type 只支持 file / webhook，收到: ${type}`);
}
const p = (prompt || "").trim();
if (!p) throw new Error("prompt 模板不能为空。");
const t = { type};
if (type === "file") {
const dir = (trigger.dir || "inbox").trim() || "inbox";
if (dir.startsWith("/") || dir.includes("..")) {
throw new Error("trigger.dir 不能是绝对路径或包含..");
}
t.dir = dir;
}
const hooks = this.store.load();
if (!Object.prototype.hasOwnProperty.call(hooks, n) &&
Object.keys(hooks).length + Object.keys(defaultHooks()).length >= MAX_HOOKS) {
throw new Error(`hook 已达上限（${MAX_HOOKS} 个），删掉不用的再注册。`);
}
hooks[n] = { trigger: t, prompt: p, desc: (desc || "").trim()};
this.store.save(hooks);
return n;
}

remove(name) {
const n = checkName(name);
const hooks = this.store.load();
if (!Object.prototype.hasOwnProperty.call(hooks, n)) {
if (Object.prototype.hasOwnProperty.call(defaultHooks(), n)) {
throw new Error(`是内置 hook，不能删除；可以用 hook_register 覆盖它的配置。`);
}
throw new Error(`没找到 hook：${n}`);
}
delete hooks[n];
this.store.save(hooks);
return n;
}

/**
* 触发一个 hook：按 prompt 模板渲染生成任务文本，记入内存任务队列 + fires.jsonl。
* source: "manual"（任意触发器）| "file"（仅 file 触发器）| "webhook"（仅 webhook 触发器）。
* 同名 hook 并发 fire 时串行执行（加锁防并发）。
*/
async fire(name, { payload = null, source = "manual", path = ""} = {}) {
const n = checkName(name);
const hooks = this.list();
const h = hooks[n];
if (!h) throw new Error(`没找到 hook：${n}`);
const ttype = h.trigger?.type;
if (source === "file" && ttype!== "file") {
throw new Error(`hook是 ${ttype} 触发，不接受 file 事件。`);
}
if (source === "webhook" && ttype!== "webhook") {
throw new Error(`hook是 ${ttype} 触发，不接受 webhook 事件。`);
}
if (!["manual", "file", "webhook"].includes(source)) {
throw new Error(`未知 source：${source}`);
}
const tail = this._locks.get(n)?? Promise.resolve();
let release;
const gate = new Promise((r) => (release = r));
this._locks.set(n, tail.then(() => gate));
await tail;
try {
const ctx = {
name: n,
path: path || "",
payload:
payload == null
? ""
: (typeof payload === "string"? payload: JSON.stringify(payload)).slice(0, 4000),
};
const prompt = renderTemplate(h.prompt, ctx);
const record = { at: new Date().toISOString(), hook: n, source, path: ctx.path, prompt};
this._delivered.push(record);
try {
mkdirSync(this.dataDir, { recursive: true});
appendFileSync(this.fireLogFile, JSON.stringify(record) + "\n", "utf-8");
} catch (err) {
this.warn(`fire log 写入失败: ${err?.message}`);
}
return prompt;
} finally {
release();
if (this._locks.get(n) === gate) this._locks.delete(n);
}
}

/**
* 轮询一次：扫描所有 file 触发器的目录。新文件只触发一次——
* 用 hook_state.json 的 name:filename → mtime 去重（复刻 Python _watcher_loop）。
* 先记账再触发：同进程内重复 tick 不会重复触发。
*/
async tick() {
const hooks = this.list();
let state = {};
try {
if (existsSync(this.stateFile)) {
state = JSON.parse(readFileSync(this.stateFile, "utf-8")) || {};
}
} catch {
state = {};
}
let changed = false;
for (const [n, h] of Object.entries(hooks)) {
if (h.trigger?.type!== "file") continue;
const dir = join(this.dataDir, h.trigger.dir || "inbox");
if (!existsSync(dir)) continue;
let entries;
try {
entries = readdirSync(dir).sort();
} catch (err) {
this.warn(`扫描 ${dir} 失败: ${err?.message}`);
continue;
}
for (const fn of entries) {
if (fn.startsWith(".")) continue;
const full = join(dir, fn);
let st;
try {
st = statSync(full);
} catch {
continue;
}
if (!st.isFile()) continue;
const key = `${n}:${fn}`;
const mtime = st.mtimeMs;
if (state[key] === mtime) continue;
state[key] = mtime;
changed = true;
try {
await this.fire(n, { source: "file", path: relative(this.dataDir, full) || fn});
} catch (err) {
this.warn(`hook触发失败: ${err?.message}`);
}
}
}
if (changed) {
try {
mkdirSync(this.dataDir, { recursive: true});
writeFileSync(this.stateFile, JSON.stringify(state), "utf-8");
} catch (err) {
this.warn(`state 写入失败: ${err?.message}`);
}
}
}

/** 启动轮询（幂等）。卸载时 stop() 回卷。 */
start() {
if (this._timer) return;
mkdirSync(this.dataDir, { recursive: true});
for (const h of Object.values(this.list())) {
if (h.trigger?.type === "file") {
mkdirSync(join(this.dataDir, h.trigger.dir || "inbox"), { recursive: true});
}
}
this._timer = setInterval(() => {
this.tick().catch((err) => this.warn(`tick 失败: ${err?.message}`));
}, this.pollIntervalMs);
if (typeof this._timer.unref === "function") this._timer.unref();
}

get running() {
return this._timer!== null;
}

/** 停止轮询（ctx.effect 卸载回卷）。 */
stop() {
if (!this._timer) return;
clearInterval(this._timer);
this._timer = null;
}

/** 内存任务队列（只读拷贝）：触发时生成的任务描述。 */
deliveredTasks() {
return this._delivered.slice();
}
}
