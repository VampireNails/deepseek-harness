// manager.js —— hook 注册、prompt 渲染、触发（加锁防并发）、inbox 轮询、有界自动调度。
//
// 复刻 Python 版 agent/hooks.py 的语义：
// - hook = name → { trigger, prompt 模板, desc, auto}，持久化到 hooks.json（defaults 内置）
// - trigger 类型：file（轮询 inbox/ 目录，新文件触发）/ webhook（供外部手动 fire）
// - fire(name)：按 prompt 模板渲染生成任务，加锁防并发串行执行
// - watcher tick：定时扫描 file 触发器目录，name:filename → mtime 去重，只触发一次
//
// 与 Python 的差异及消费方（2026-09-30 闭环）：
// Python 版 fire 后直接跑 `action: agent`（无节流）；dsh-muse 采用有界自动调度——
// 触发的任务进入 pending 队列（pending/<hook>.jsonl，at-most-once），按「单 hook
// 冷却（默认 15 分钟）+ 全局并发上限（默认 3）+ 单 hook 串行」分批执行，每批通过
// intel-task.sh 起一个 headless 任务（dsh --profile evolve），多次触发合并为一批
// prompt。调度记录记 dispatch.log；fires.jsonl 保留全量审计。调度失败保留 pending
// 下次重试。auto=false 的 hook 只记录不调度。
// 这是对 Python 版的超越：冷却节流 + 并发上限 + 审计 + 失败保留；dsh 插件侧不直接
// 跑 agent 回路，而是复用系统已有的 intel-task.sh 任务通道（与 cron 一致）。

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
import { spawn as nodeSpawn } from "node:child_process";

export const DEFAULT_POLL_INTERVAL_MS = 60000;
export const MAX_HOOKS = 50;
export const DEFAULT_DISPATCH_COOLDOWN_MS = 15 * 60 * 1000;
export const DEFAULT_TASK_BIN = "/root/intel/bin/intel-task.sh";
export const DEFAULT_MAX_CONCURRENT_DISPATCHES = 3;

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
auto: true,
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
constructor({ store, ctx, pollIntervalMs, dispatch = {}, spawnFn = null} = {}) {
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
// 有界自动调度配置
const d = dispatch || {};
this.autoDispatch = d.enabled !== false;
this.dispatchCooldownMs =
Number.isSafeInteger(d.cooldownMs) && d.cooldownMs > 0
? d.cooldownMs
: DEFAULT_DISPATCH_COOLDOWN_MS;
this.taskBin = d.taskBin || DEFAULT_TASK_BIN;
this.maxConcurrent =
Number.isSafeInteger(d.maxConcurrent) && d.maxConcurrent > 0
? d.maxConcurrent
: DEFAULT_MAX_CONCURRENT_DISPATCHES;
this._spawnFn = spawnFn || ((bin, args, opts) => nodeSpawn(bin, args, opts));
this._pendingDir = join(this.dataDir, "pending");
this._dispatchStateFile = join(this.dataDir, "dispatch_state.json");
this._dispatchLogFile = join(this.dataDir, "dispatch.log");
this._inFlight = new Map(); // hook -> child（单 hook 串行）
this._cooldownTimers = new Map(); // hook -> timeout
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

register(name, { trigger, prompt, desc, auto} = {}) {
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
hooks[n] = { trigger: t, prompt: p, desc: (desc || "").trim(), auto: auto !== false};
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
* 触发一个 hook：按 prompt 模板渲染生成任务文本，记入内存任务队列 + fires.jsonl，
* 再入 pending 队列由有界调度器消费（auto=false 的 hook 只记录不调度）。
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
// 有界自动调度：fire 只负责入队，_maybeDispatch 决定是否立即执行。
// 调度失败不影响 fire 本身（已记 fires.jsonl 审计）。
if (this.autoDispatch && h.auto !== false) {
try {
this._enqueuePending(n, record);
} catch (err) {
this.warn(`pending 入队失败: ${err?.message}`);
}
try {
this._maybeDispatch(n);
} catch (err) {
this.warn(`调度触发失败: ${err?.message}`);
}
}
return prompt;
} finally {
release();
if (this._locks.get(n) === gate) this._locks.delete(n);
}
}

/** pending 队列入队（durable；at-most-once：调度成功才消费）。 */
_enqueuePending(hook, record) {
mkdirSync(this._pendingDir, { recursive: true});
appendFileSync(join(this._pendingDir, `${hook}.jsonl`), JSON.stringify(record) + "\n", "utf-8");
}

/** 读某 hook 的全部 pending 记录。 */
_readPending(hook) {
const f = join(this._pendingDir, `${hook}.jsonl`);
if (!existsSync(f)) return [];
try {
return readFileSync(f, "utf-8").split("\n").filter((l) => l.trim()).map((l) => {
try {
return JSON.parse(l);
} catch {
return null;
}
}).filter(Boolean);
} catch {
return [];
}
}

/** 清空某 hook 的 pending（调度成功后调用）。 */
_clearPending(hook) {
try {
writeFileSync(join(this._pendingDir, `${hook}.jsonl`), "", "utf-8");
} catch (err) {
this.warn(`pending 清空失败: ${err?.message}`);
}
}

_loadDispatchState() {
try {
if (existsSync(this._dispatchStateFile)) {
return JSON.parse(readFileSync(this._dispatchStateFile, "utf-8")) || {};
}
} catch {
/* ignore */
}
return {};
}

_saveDispatchState(s) {
try {
mkdirSync(this.dataDir, { recursive: true});
writeFileSync(this._dispatchStateFile, JSON.stringify(s), "utf-8");
} catch (err) {
this.warn(`dispatch state 写入失败: ${err?.message}`);
}
}

/**
* 有界调度决策：pending 非空、hook 无在途任务、冷却到期、全局并发未满 → 执行。
* 冷却中则设一次性 timer 到期重踢；在途则等待完成回调重踢；
* 全局并发满则等待 tick 扫尾或在途任务完成回调重扫。
*/
_maybeDispatch(hook) {
if (!this.autoDispatch) return;
if (this._inFlight.has(hook)) return;
let pending;
try {
pending = this._readPending(hook);
} catch (err) {
this.warn(`pending 读取失败: ${err?.message}`);
return;
}
if (pending.length === 0) return;
if (this._inFlight.size >= this.maxConcurrent) return;
const state = this._loadDispatchState();
const last = Number(state[hook]) || 0;
const wait = this.dispatchCooldownMs - (Date.now() - last);
if (wait > 0) {
if (!this._cooldownTimers.has(hook)) {
const t = setTimeout(() => {
this._cooldownTimers.delete(hook);
try {
this._maybeDispatch(hook);
} catch (err) {
this.warn(`冷却重踢失败: ${err?.message}`);
}
}, wait);
if (typeof t.unref === "function") t.unref();
this._cooldownTimers.set(hook, t);
}
return;
}
this._doDispatch(hook, pending);
}

/** 把一批 pending 记录渲染成单个任务 prompt。 */
_buildBatchPrompt(hook, records) {
const parts = records.map((r, i) => {
const meta = [r.at || "", r.source ? `source=${r.source}` : "", r.path ? `path=${r.path}` : ""].filter(Boolean).join("，");
return `--- 任务 ${i + 1}/${records.length}${meta ? `（${meta}）` : ""} ---\n${r.prompt || ""}`;
});
return `[hook 自动任务] hook "${hook}" 共触发 ${records.length} 次，以下是待处理任务。` +
`请逐个执行，保持简洁；结果写入今日笔记（daily note）。不要向用户发送消息，除非任务明确要求。\n\n` +
parts.join("\n\n");
}

/**
* 执行一次调度：spawn intel-task.sh（detached，不阻塞插件）。
* 成功才消费 pending（at-most-once）；失败保留 pending、冷却照算，下次 tick/触发重试。
*/
_doDispatch(hook, pending) {
const prompt = this._buildBatchPrompt(hook, pending);
const ts = new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14);
const jobId = `hook_${hook}_${ts}`;
let child = null;
try {
child = this._spawnFn(this.taskBin, [jobId, prompt], { detached: true, stdio: "ignore"});
} catch (err) {
this.warn(`hook 调度失败 ${hook}: ${err?.message}，保留 pending 下次重试`);
const state = this._loadDispatchState();
state[hook] = Date.now();
this._saveDispatchState(state);
return;
}
// spawn 成功才消费 pending
const state = this._loadDispatchState();
state[hook] = Date.now();
this._saveDispatchState(state);
this._clearPending(hook);
if (child && typeof child.unref === "function") {
try {
child.unref();
} catch {
/* ignore */
}
}
this._inFlight.set(hook, child || true);
try {
mkdirSync(this.dataDir, { recursive: true});
appendFileSync(this._dispatchLogFile,
JSON.stringify({ at: new Date().toISOString(), hook, job: jobId, tasks: pending.length }) + "\n",
"utf-8");
} catch (err) {
this.warn(`dispatch log 写入失败: ${err?.message}`);
}
const done = () => {
if (!this._inFlight.has(hook)) return;
this._inFlight.delete(hook);
// 完成后重扫所有 hook：处理排队 pending 与释放的全局并发
for (const n of Object.keys(this.list())) {
try {
this._maybeDispatch(n);
} catch (err) {
this.warn(`完成重扫失败: ${err?.message}`);
}
}
};
if (child && typeof child.on === "function") {
child.on("exit", done);
child.on("error", done);
} else {
// 测试 stub 无事件：同步视为完成
done();
}
}

/**
* 轮询一次：扫描所有 file 触发器的目录。新文件只触发一次——
* 用 hook_state.json 的 name:filename → mtime 去重（复刻 Python _watcher_loop）。
* 先记账再触发：同进程内重复 tick 不会重复触发。
* 末尾做调度扫尾：失败重试 / 冷却到期 / 并发释放后的 pending 由此兜底。
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
// 调度扫尾：_maybeDispatch 自带冷却/并发/在途保护，可安全重入
for (const n of Object.keys(hooks)) {
try {
this._maybeDispatch(n);
} catch (err) {
this.warn(`tick 调度扫尾失败 ${n}: ${err?.message}`);
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
mkdirSync(this._pendingDir, { recursive: true});
// 重启恢复：pending 有残留则尝试调度（_maybeDispatch 自带冷却/并发保护）
try {
for (const fn of readdirSync(this._pendingDir)) {
if (!fn.endsWith(".jsonl")) continue;
const hook = fn.slice(0, -".jsonl".length);
try {
this._maybeDispatch(hook);
} catch (err) {
this.warn(`重启恢复调度失败 ${hook}: ${err?.message}`);
}
}
} catch (err) {
this.warn(`pending 扫描失败: ${err?.message}`);
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
if (this._timer) {
clearInterval(this._timer);
this._timer = null;
}
for (const t of this._cooldownTimers.values()) {
try {
clearTimeout(t);
} catch {
/* ignore */
}
}
this._cooldownTimers.clear();
}

/** 内存任务队列（只读拷贝）：触发时生成的任务描述。 */
deliveredTasks() {
return this._delivered.slice();
}
}
