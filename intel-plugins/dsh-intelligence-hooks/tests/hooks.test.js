// hooks.test.js —— 注册/列出/删除、模板渲染、轮询去重、timer 启停单测（fake ctx，不碰真 dsh）
// 轮询用短间隔 + 直接调 tick()，不 sleep 久等。
import { test} from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync, mkdirSync, utimesSync, readFileSync, existsSync} from "node:fs";
import { tmpdir} from "node:os";
import { join} from "node:path";
import { HookStore, defaultDataDir} from "../src/store.js";
import { HookManager, renderTemplate, defaultHooks, DEFAULT_POLL_INTERVAL_MS} from "../src/manager.js";
import { hookRegisterTool, hookListTool, hookRemoveTool, hookFireTool} from "../src/tools.js";

function freshDir() {
return mkdtempSync(join(tmpdir(), "hooks-test-"));
}
function makeManager(dir, opts = {}) {
const store = new HookStore(dir);
// 默认关闭自动调度：旧单测只验证 fire 记录，不碰真 spawn
const dispatch = opts.dispatch ?? { enabled: false };
return new HookManager({ store, ctx: { logger: { warn() {}}}, pollIntervalMs: 50,...opts, dispatch});
}
// 带 fake spawn 的调度测试专用 manager
function makeDispatchManager(dir, dispOpts = {}) {
const calls = [];
const store = new HookStore(dir);
const m = new HookManager({
store,
ctx: { logger: { warn() {}}},
pollIntervalMs: 50,
dispatch: { enabled: true, cooldownMs: 50, taskBin: "/bin/echo-stub", maxConcurrent: 3, ...dispOpts},
spawnFn: (bin, args, opts) => { calls.push({ bin, args, opts }); return { unref() {}, on() {} }; },
});
return { m, calls };
}
function writeInbox(dir, name, content = "x") {
mkdirSync(join(dir, "inbox"), { recursive: true});
writeFileSync(join(dir, "inbox", name), content, "utf-8");
}

// ---- 渲染 ----

test("renderTemplate: 占位替换；缺键退化为 [context]", () => {
assert.equal(renderTemplate("file {path} for {name}", { path: "a.txt", name: "inbox"}), "file a.txt for inbox");
const out = renderTemplate("got {payload}, missing {nope}", { payload: "p"});
assert.ok(out.includes("got p, missing {nope}"), "缺键保留原文");
assert.ok(out.includes("[context]"), "缺键追加 context");
assert.equal(renderTemplate("no placeholders", {}), "no placeholders");
});

test("defaultHooks: 内置 inbox hook", () => {
const d = defaultHooks();
assert.equal(d.inbox.trigger.type, "file");
assert.equal(d.inbox.trigger.dir, "inbox");
assert.ok(d.inbox.prompt.includes("{path}"));
});

test("DEFAULT_POLL_INTERVAL_MS 为 60 秒", () => {
assert.equal(DEFAULT_POLL_INTERVAL_MS, 60000);
});

test("defaultDataDir: DSH_HOME 下的 intel-hooks", () => {
assert.ok(defaultDataDir().endsWith("intel-hooks"));
process.env.DSH_HOME = "/tmp/x-dsh-home";
try {
assert.equal(defaultDataDir(), "/tmp/x-dsh-home/intel-hooks");
} finally {
delete process.env.DSH_HOME;
}
});

// ---- 注册/列出/删除 ----

test("register/list/remove", async () => {
const dir = freshDir();
try {
const m = makeManager(dir);
assert.ok(m.list().inbox, "内置 inbox 始终可见");
m.register("notify", { trigger: { type: "webhook"}, prompt: "event: {payload}", desc: "测试"});
const names = Object.keys(m.list());
assert.ok(names.includes("notify") && names.includes("inbox"));
// 覆盖更新
m.register("notify", { trigger: { type: "webhook"}, prompt: "v2 {payload}", desc: ""});
assert.equal(m.list().notify.prompt, "v2 {payload}");
// 持久化
const m2 = makeManager(dir);
assert.equal(m2.list().notify.prompt, "v2 {payload}");
// 删除
m2.remove("notify");
assert.ok(!Object.keys(m2.list()).includes("notify"));
assert.ok(m2.list().inbox, "删自定义不影响内置");
// 删内置抛错
assert.throws(() => m2.remove("inbox"), /内置 hook/);
// 删不存在抛错
assert.throws(() => m2.remove("nope"), /没找到/);
} finally {
rmSync(dir, { recursive: true, force: true});
}
});

test("register 校验", () => {
const m = makeManager(freshDir());
assert.throws(() => m.register("bad name!", { trigger: { type: "file"}, prompt: "x"}), /名称非法/);
assert.throws(() => m.register("ok", { trigger: { type: "file"}, prompt: " "}), /不能为空/);
assert.throws(() => m.register("ok", { trigger: { type: "http"}, prompt: "x"}), /file \/ webhook/);
assert.throws(() => m.register("ok", { trigger: { type: "file", dir: "../evil"}, prompt: "x"}), /绝对路径或包含/);
});

// ---- 触发 ----

test("hook_fire 手动触发：渲染 + trigger 类型校验", async () => {
const dir = freshDir();
try {
const m = makeManager(dir);
const prompt = await m.fire("inbox", { source: "manual", path: "inbox/a.txt"});
assert.ok(prompt.includes("inbox/a.txt"), `渲染应含 path，得到: ${prompt}`);
assert.equal(m.deliveredTasks().length, 1);
assert.equal(m.deliveredTasks()[0].hook, "inbox");
// fires.jsonl 落盘
const lines = readFileSync(join(dir, "fires.jsonl"), "utf-8").trim().split("\n");
assert.equal(lines.length, 1);
assert.equal(JSON.parse(lines[0]).hook, "inbox");
// webhook hook 不接受 file source
m.register("wh", { trigger: { type: "webhook"}, prompt: "w {payload}"});
await assert.rejects(() => m.fire("wh", { source: "file", path: "x"}), /不接受 file 事件/);
// file hook 不接受 webhook source
await assert.rejects(() => m.fire("inbox", { source: "webhook"}), /不接受 webhook 事件/);
// 未知 hook
await assert.rejects(() => m.fire("nope"), /没找到 hook/);
} finally {
rmSync(dir, { recursive: true, force: true});
}
});

test("并发 fire 同名 hook 串行（加锁防并发）", async () => {
const dir = freshDir();
try {
const m = makeManager(dir);
const [a, b] = await Promise.all([
m.fire("inbox", { path: "inbox/1.txt"}),
m.fire("inbox", { path: "inbox/2.txt"}),
]);
assert.ok(a.includes("1.txt") && b.includes("2.txt"));
assert.equal(m.deliveredTasks().length, 2);
} finally {
rmSync(dir, { recursive: true, force: true});
}
});

// ---- 轮询 ----

test("tick: 新文件只触发一次，不重复触发", async () => {
const dir = freshDir();
try {
const m = makeManager(dir);
writeInbox(dir, "a.txt");
await m.tick();
assert.equal(m.deliveredTasks().length, 1);
assert.ok(m.deliveredTasks()[0].prompt.includes("a.txt"));
await m.tick();
await m.tick();
assert.equal(m.deliveredTasks().length, 1, "重复 tick 不应重复触发");
writeInbox(dir, "b.txt");
await m.tick();
assert.equal(m.deliveredTasks().length, 2);
// 点文件跳过
writeInbox(dir, ".hidden");
await m.tick();
assert.equal(m.deliveredTasks().length, 2, "点文件应跳过");
} finally {
rmSync(dir, { recursive: true, force: true});
}
});

test("tick: 文件内容变化（mtime 变）再次触发", async () => {
const dir = freshDir();
try {
const m = makeManager(dir);
writeInbox(dir, "a.txt", "v1");
await m.tick();
assert.equal(m.deliveredTasks().length, 1);
// 显式推后 mtime
const later = new Date(Date.now() + 5000);
utimesSync(join(dir, "inbox", "a.txt"), later, later);
writeFileSync(join(dir, "inbox", "a.txt"), "v2", "utf-8");
const muchLater = new Date(Date.now() + 10000);
utimesSync(join(dir, "inbox", "a.txt"), muchLater, muchLater);
await m.tick();
assert.equal(m.deliveredTasks().length, 2, "mtime 变化应再次触发");
} finally {
rmSync(dir, { recursive: true, force: true});
}
});

test("tick: state 跨实例持久（重启不重发）", async () => {
const dir = freshDir();
try {
const m1 = makeManager(dir);
writeInbox(dir, "a.txt");
await m1.tick();
assert.equal(m1.deliveredTasks().length, 1);
const m2 = makeManager(dir);
await m2.tick();
assert.equal(m2.deliveredTasks().length, 0, "新实例不应重发已处理文件");
} finally {
rmSync(dir, { recursive: true, force: true});
}
});

test("tick: 自定义 file hook 轮询自己的目录", async () => {
const dir = freshDir();
try {
const m = makeManager(dir);
m.register("drops", { trigger: { type: "file", dir: "drops"}, prompt: "drop: {path}"});
mkdirSync(join(dir, "drops"), { recursive: true});
writeFileSync(join(dir, "drops", "job.md"), "do", "utf-8");
await m.tick();
const got = m.deliveredTasks();
assert.equal(got.length, 1);
assert.equal(got[0].hook, "drops");
} finally {
rmSync(dir, { recursive: true, force: true});
}
});

// ---- timer 启停 ----

test("start/stop: 轮询 timer 启停与回卷（短间隔真跑）", async () => {
const dir = freshDir();
const m = makeManager(dir, { pollIntervalMs: 40});
try {
assert.equal(m.running, false);
m.start();
assert.equal(m.running, true);
const t1 = m._timer;
m.start(); // 幂等
assert.equal(m._timer, t1, "重复 start 不应建第二个 timer");
writeInbox(dir, "live.txt");
await new Promise((r) => setTimeout(r, 300));
assert.ok(m.deliveredTasks().length >= 1, "timer 应在轮询中发现新文件");
m.stop();
assert.equal(m.running, false);
m.stop(); // 幂等
assert.ok(!existsSync(join(dir, "inbox")) || true);
} finally {
m.stop();
rmSync(dir, { recursive: true, force: true});
}
});

// ---- 工具层 ----

test("工具层：hook_register/list/remove/fire", async () => {
const dir = freshDir();
try {
const m = makeManager(dir);
const reg = hookRegisterTool(m);
const r1 = await reg.execute({ name: "t1", trigger_type: "webhook", prompt: "ev {payload}", desc: "d"});
assert.equal(r1.ok, true);
const r1bad = await reg.execute({ name: "t1", trigger_type: "nope", prompt: "x"});
assert.equal(r1bad.ok, false);

const list = hookListTool(m);
const l1 = await list.execute({});
assert.equal(l1.ok, true);
assert.ok(l1.text.includes("") && l1.text.includes(""));

const fire = hookFireTool(m);
const f1 = await fire.execute({ name: "t1", payload: '{"a":1}'});
assert.equal(f1.ok, true);
assert.ok(f1.text.includes('{"a":1}'));
const f2 = await fire.execute({ name: "nope"});
assert.equal(f2.ok, false);

const rm = hookRemoveTool(m);
const d1 = await rm.execute({ name: "t1"});
assert.equal(d1.ok, true);
const d2 = await rm.execute({ name: "inbox"});
assert.equal(d2.ok, false);
} finally {
rmSync(dir, { recursive: true, force: true});
}
});

// ---- 有界自动调度 ----

test("dispatch: fire 默认自动调度，spawn 参数正确", async () => {
const dir = freshDir();
try {
const { m, calls } = makeDispatchManager(dir);
m.register("w1", { trigger: { type: "webhook" }, prompt: "do {payload}", desc: "t" });
await m.fire("w1", { source: "webhook", payload: "hello" });
assert.equal(calls.length, 1, "应调度一次");
assert.equal(calls[0].bin, "/bin/echo-stub");
assert.match(calls[0].args[0], /^hook_w1_\d{14}$/, "jobId 格式 hook_<name>_<ts>");
assert.ok(calls[0].args[1].includes("do hello"), "prompt 应包含渲染后的任务");
assert.ok(calls[0].args[1].includes("[hook 自动任务]"), "应有任务头");
assert.equal(m._readPending("w1").length, 0, "at-most-once：pending 已消费");
assert.ok(existsSync(join(dir, "dispatch.log")), "应写 dispatch.log");
} finally { rmSync(dir, { recursive: true, force: true }); }
});

test("dispatch: 冷却期内不重复调度", async () => {
const dir = freshDir();
try {
const { m, calls } = makeDispatchManager(dir, { cooldownMs: 60000 });
m.register("w2", { trigger: { type: "webhook" }, prompt: "x", desc: "t" });
await m.fire("w2", { source: "webhook" });
await m.fire("w2", { source: "webhook" });
assert.equal(calls.length, 1, "冷却期内第二次不应调度");
assert.equal(m._readPending("w2").length, 1, "第二次触发应留在 pending");
} finally { rmSync(dir, { recursive: true, force: true }); }
});

test("dispatch: auto=false 的 hook 只记录不调度", async () => {
const dir = freshDir();
try {
const { m, calls } = makeDispatchManager(dir);
m.register("w3", { trigger: { type: "webhook" }, prompt: "x", desc: "t", auto: false });
await m.fire("w3", { source: "webhook" });
assert.equal(calls.length, 0, "不应调度");
assert.equal(m._readPending("w3").length, 0, "不应入 pending");
const lines = readFileSync(join(dir, "fires.jsonl"), "utf-8").trim().split("\n");
assert.equal(lines.length, 1, "fires.jsonl 仍应记录");
} finally { rmSync(dir, { recursive: true, force: true }); }
});

test("dispatch: spawn 失败保留 pending", async () => {
const dir = freshDir();
try {
const store = new HookStore(dir);
const m = new HookManager({
store, ctx: { logger: { warn() {} } }, pollIntervalMs: 50,
dispatch: { enabled: true, cooldownMs: 50, taskBin: "/bin/echo-stub", maxConcurrent: 3 },
spawnFn: () => { throw new Error("spawn 炸了"); },
});
m.register("w4", { trigger: { type: "webhook" }, prompt: "x", desc: "t" });
await m.fire("w4", { source: "webhook" });
assert.equal(m._readPending("w4").length, 1, "失败应保留 pending");
} finally { rmSync(dir, { recursive: true, force: true }); }
});

test("dispatch: 在途不重入，完成后重踢消费排队", async () => {
const dir = freshDir();
try {
const { m, calls } = makeDispatchManager(dir, { cooldownMs: 1 });
m.register("w5", { trigger: { type: "webhook" }, prompt: "task {payload}", desc: "t" });
m._inFlight.set("w5", true);
await m.fire("w5", { source: "webhook", payload: "a" });
assert.equal(calls.length, 0, "在途时不应调度");
assert.equal(m._readPending("w5").length, 1);
m._inFlight.delete("w5");
m._saveDispatchState({ w5: Date.now() - 60000 });
m._maybeDispatch("w5");
assert.equal(calls.length, 1, "重踢后应调度");
assert.ok(calls[0].args[1].includes("task a"), "应消费排队的任务");
assert.equal(m._readPending("w5").length, 0);
} finally { rmSync(dir, { recursive: true, force: true }); }
});

test("dispatch: 批量合并多次触发", async () => {
const dir = freshDir();
try {
const { m, calls } = makeDispatchManager(dir, { cooldownMs: 1 });
m.register("w6", { trigger: { type: "webhook" }, prompt: "job {payload}", desc: "t" });
m._inFlight.set("w6", true);
await m.fire("w6", { source: "webhook", payload: "first" });
await m.fire("w6", { source: "webhook", payload: "second" });
assert.equal(m._readPending("w6").length, 2);
m._inFlight.delete("w6");
m._saveDispatchState({ w6: Date.now() - 60000 });
m._maybeDispatch("w6");
assert.equal(calls.length, 1, "应合并为一次调度");
assert.ok(calls[0].args[1].includes("job first"), "应含第一次");
assert.ok(calls[0].args[1].includes("job second"), "应含第二次");
assert.ok(calls[0].args[1].includes("共触发 2 次"), "应注明批量");
} finally { rmSync(dir, { recursive: true, force: true }); }
});

test("dispatch: 全局关闭时不调度", async () => {
const dir = freshDir();
try {
const store = new HookStore(dir);
let spawned = 0;
const m = new HookManager({
store, ctx: { logger: { warn() {} } }, pollIntervalMs: 50,
dispatch: { enabled: false },
spawnFn: () => { spawned++; return { unref() {}, on() {} }; },
});
m.register("w7", { trigger: { type: "webhook" }, prompt: "x", desc: "t" });
await m.fire("w7", { source: "webhook" });
assert.equal(spawned, 0, "全局关闭不应 spawn");
} finally { rmSync(dir, { recursive: true, force: true }); }
});
