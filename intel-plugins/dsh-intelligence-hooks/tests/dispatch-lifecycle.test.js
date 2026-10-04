import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { HookManager } from "../src/manager.js";
import { HookStore } from "../src/store.js";
import { hookListTool } from "../src/tools.js";

async function until(check) {
  const deadline = Date.now() + 10000;
  while (!check()) {
    assert.ok(Date.now() < deadline, "timed out awaiting observable state");
    await delay(10);
  }
}

function fixture(t, dispatch = {}, missing = false) {
  const dir = mkdtempSync(join(tmpdir(), "hooks-lifecycle-"));
  const children = [];
  const exits = [];
  const warnings = [];
  const script = join(dir, "task.cjs");
  writeFileSync(script, `const fs = require('node:fs');
const path = require('node:path');
const dir = __dirname;
fs.appendFileSync(path.join(dir, 'started.jsonl'), JSON.stringify(process.argv.slice(2)) + '\\n');
const timer = setInterval(() => {
  if (fs.existsSync(path.join(dir, 'release'))) {
    clearInterval(timer);
    process.exit(Number(fs.readFileSync(path.join(dir, 'release'), 'utf8')));
  }
}, 10);
`);
  const manager = new HookManager({
    store: new HookStore(join(dir, "data")),
    ctx: { logger: { warn(message) { warnings.push(message); } } },
    pollIntervalMs: 60000,
    dispatch: { cooldownMs: 60000, maxConcurrent: 1, ...dispatch },
    spawnFn: (bin, args, opts) => {
      const child = missing ? spawn(join(dir, "missing-executable"), args, opts)
        : spawn(process.execPath, [script, ...args], { ...opts, detached: false });
      // Tests own and await this process; keep its real handle referenced through close.
      child.unref = () => {};
      children.push(child);
      exits.push(new Promise(r => child.once("close", r)));
      return child;
    },
  });
  t.after(async () => {
    manager.stop();
    for (const child of children) if (child.exitCode === null && child.signalCode === null) child.kill();
    await Promise.all(exits);
    assert.ok(resolve(dir).startsWith(resolve(tmpdir()) + requireSeparator()), "cleanup stays in temporary root");
    rmSync(dir, { recursive: true, force: true });
  });
  function register(name, auto = true) {
    manager.register(name, { trigger: { type: "webhook" }, prompt: "task {payload}", auto });
  }
  function starts() {
    const file = join(dir, "started.jsonl");
    if (!existsSync(file)) return [];
    const text = readFileSync(file, "utf8");
    // A child can create the append log before its first write becomes visible.
    // Only newline-terminated records prove a complete start notification.
    const completed = text.slice(0, text.lastIndexOf("\n") + 1);
    return completed ? completed.trimEnd().split("\n").map(JSON.parse) : [];
  }
  return { manager, dir, children, warnings, register, starts, exits,
    executableAvailable() { missing = false; } };
}

function requireSeparator() { return process.platform === "win32" ? "\\" : "/"; }

test("start observation waits for complete append records and still rejects corrupt records", t => {
  const f = fixture(t);
  const file = join(f.dir, "started.jsonl");
  writeFileSync(file, "");
  assert.deepEqual(f.starts(), []);
  writeFileSync(file, '["pending"]');
  assert.deepEqual(f.starts(), []);
  writeFileSync(file, '["ready"]\n["pending"');
  assert.deepEqual(f.starts(), [["ready"]]);
  writeFileSync(file, 'broken\n');
  assert.throws(() => f.starts(), SyntaxError);
});

test("real async ENOENT retains the original pending batch and never records successful dispatch", async t => {
  const f = fixture(t, {}, true);
  f.register("a");
  await f.manager.fire("a", { payload: "original" });
  await f.exits[0];
  assert.equal(f.manager._readPending("a").length, 1);
  assert.match(f.manager._readPending("a")[0].prompt, /original/);
  const log = join(f.manager.dataDir, "dispatch.log");
  if (existsSync(log)) assert.ok(!readFileSync(log, "utf8").includes('"status":"started"'));
  assert.ok(f.warnings.some(message => message.includes("ENOENT")));
});

test("stop fences completion callbacks and explicit ticks from starting queued children", async t => {
  const f = fixture(t);
  f.register("a"); f.register("b");
  await f.manager.fire("a");
  await until(() => f.starts().length === 1);
  await f.manager.fire("b");
  f.manager.stop();
  writeFileSync(join(f.dir, "release"), "0");
  await f.exits[0];
  await f.manager.tick();
  assert.equal(f.children.length, 1);
  assert.equal(f.manager._readPending("b").length, 1);
});

for (const action of ["disable", "remove"]) {
  test(`restart does not dispatch residual pending after ${action}`, async t => {
    const f = fixture(t);
    f.register("a");
    mkdirSync(f.manager._pendingDir, { recursive: true });
    f.manager._enqueuePending("a", { prompt: "residual", hook: "a" });
    if (action === "disable") f.register("a", false);
    else f.manager.remove("a");
    f.manager.start();
    await f.manager.tick();
    assert.equal(f.children.length, 0);
    assert.equal(f.manager._readPending("a").length, 1);
  });
}

test("records enqueued before the asynchronous spawn event survive consumption of the first batch", async t => {
  const f = fixture(t);
  f.register("a");
  await f.manager.fire("a", { payload: "first" });
  await f.manager.fire("a", { payload: "second" });
  await until(() => f.starts().length === 1);
  assert.equal(f.manager._readPending("a").length, 1);
  assert.match(f.manager._readPending("a")[0].prompt, /second/);
});

test("reload accounts for the old detached task in the global and per-hook limits", async t => {
  const f = fixture(t);
  f.register("a"); f.register("b");
  await f.manager.fire("a");
  await until(() => f.starts().length === 1);
  await f.manager.fire("a", { payload: "next a" });
  await f.manager.fire("b", { payload: "next b" });
  f.manager.stop();
  const reloaded = new HookManager({
    store: new HookStore(f.manager.dataDir),
    dispatch: { cooldownMs: 60000, maxConcurrent: 1 },
    spawnFn: f.manager._spawnFn,
  });
  t.after(() => reloaded.stop());
  reloaded.start();
  assert.equal(f.children.length, 1, "old live task must retain its global slot");
  writeFileSync(join(f.dir, "release"), "0");
  await f.exits[0];
  await reloaded.tick();
  await until(() => f.starts().length === 2);
  assert.match(f.starts()[1][0], /hook_b_/);
  assert.equal(reloaded._readPending("a").length, 1, "old hook cooldown survives reload");
});

test("nonzero task exit retains its batch and outcome without replaying partial side effects", async t => {
  const f = fixture(t, { cooldownMs: 1 });
  f.register("a");
  await f.manager.fire("a", { payload: "partial side effect" });
  await until(() => f.starts().length === 1);
  writeFileSync(join(f.dir, "release"), "7");
  await f.exits[0];
  await f.manager.fire("a", { payload: "new event" });
  await f.manager.tick();
  assert.equal(f.children.length, 1, "failed batch requires explicit recovery review");
  const status = f.manager.dispatchStatus("a");
  assert.equal(status.status, "failed");
  assert.equal(status.code, 7);
  assert.match(status.records[0].prompt, /partial side effect/);
  assert.equal(status.pending, 1);
});

test("durable journal write failure prevents execution and preserves pending", async t => {
  const f = fixture(t);
  f.register("a");
  writeFileSync(join(f.manager.dataDir, "dispatch-runs"), "blocks journal directory");
  await f.manager.fire("a");
  assert.equal(f.children.length, 0);
  assert.equal(f.manager._readPending("a").length, 1);
});

test("pending consumption failure after spawn never replays the already started batch", async t => {
  const f = fixture(t, { cooldownMs: 1 });
  f.register("a");
  const realClear = f.manager._clearPending.bind(f.manager);
  f.manager._clearPending = () => { throw new Error("injected storage failure"); };
  await f.manager.fire("a");
  await until(() => f.starts().length === 1);
  writeFileSync(join(f.dir, "release"), "0");
  await f.exits[0];
  f.manager._clearPending = realClear;
  await f.manager.tick();
  assert.equal(f.children.length, 1);
  assert.equal(f.manager.dispatchStatus("a").status, "uncertain");
  assert.equal(f.manager._readPending("a").length, 1);
});

test("pending byte limit reports rejection and keeps the previous event intact", async t => {
  const f = fixture(t, { maxPendingBytes: 500 });
  f.register("a");
  f.manager.stop();
  await f.manager.fire("a", { payload: "first" });
  await assert.rejects(f.manager.fire("a", { payload: "x".repeat(600) }), /pending.*上限/);
  assert.equal(f.manager._readPending("a").length, 1);
  assert.match(f.manager._readPending("a")[0].prompt, /first/);
});

test("batch record limit leaves excess events queued and memory history remains bounded", async t => {
  const f = fixture(t, { maxBatchRecords: 2, maxDeliveredTasks: 3 });
  f.register("a");
  f.manager.stop();
  for (let i = 0; i < 5; i++) await f.manager.fire("a", { payload: String(i) });
  f.manager.start();
  await until(() => f.starts().length === 1);
  assert.match(f.starts()[0][1], /共触发 2 次/);
  assert.equal(f.manager._readPending("a").length, 3);
  assert.equal(f.manager.deliveredTasks().length, 3);
});

test("malformed durable pending fails closed instead of discarding records", async t => {
  const f = fixture(t);
  f.register("a");
  mkdirSync(f.manager._pendingDir, { recursive: true });
  const file = join(f.manager._pendingDir, "a.jsonl");
  const original = '{"hook":"a","prompt":"valid"}\n{broken\n';
  writeFileSync(file, original);
  f.manager.start();
  await f.manager.tick();
  assert.equal(f.children.length, 0);
  assert.equal(readFileSync(file, "utf8"), original);
  assert.ok(f.warnings.some(message => /pending/.test(message)));
});

test("hook_list identifies the hook, backlog, failed job and exit result without exposing its prompt", async t => {
  const f = fixture(t);
  f.register("a");
  await f.manager.fire("a", { payload: "private prompt marker" });
  await until(() => f.starts().length === 1);
  writeFileSync(join(f.dir, "release"), "7");
  await f.exits[0];
  const result = await hookListTool(f.manager).execute({});
  assert.equal(result.ok, true);
  assert.match(result.text, /a.*failed.*退出码=7/);
  assert.match(result.text, /hook_a_/);
  assert.ok(!result.text.includes("private prompt marker"));
});

test("real ENOENT recovery retries only after cooldown and executes the retained batch once", async t => {
  const f = fixture(t, { cooldownMs: 150 }, true);
  f.register("a");
  await f.manager.fire("a", { payload: "retained" });
  await f.exits[0];
  f.executableAvailable();
  f.manager.start();
  assert.equal(f.children.length, 1, "cooldown must still apply to ENOENT");
  await until(() => f.starts().length === 1);
  assert.match(f.starts()[0][1], /retained/);
  assert.equal(f.children.length, 2);
  writeFileSync(join(f.dir, "release"), "0");
  await f.exits[1];
  assert.equal(f.manager.dispatchStatus("a").status, "succeeded");
  assert.equal(f.manager._readPending("a").length, 0);
});

test("manual recovery requires the stopped host, matching job and explicit duplicate risk acknowledgement", async t => {
  const f = fixture(t, { cooldownMs: 1 });
  f.register("a");
  await f.manager.fire("a", { payload: "review before retry" });
  await until(() => f.starts().length === 1);
  const job = f.manager.dispatchStatus("a").job;
  assert.throws(() => f.manager.recoverDispatch("a", { job, action: "retry", confirmStopped: true, acknowledgeDuplicateRisk: true }), /停止/);
  writeFileSync(join(f.dir, "release"), "7");
  await f.exits[0];
  f.manager.stop();
  assert.throws(() => f.manager.recoverDispatch("a", { job: "wrong", action: "retry", confirmStopped: true, acknowledgeDuplicateRisk: true }), /job/);
  assert.throws(() => f.manager.recoverDispatch("a", { job, action: "retry", confirmStopped: true }), /副作用/);
  f.manager.recoverDispatch("a", { job, action: "retry", confirmStopped: true, acknowledgeDuplicateRisk: true });
  assert.equal(f.children.length, 1, "recovery itself must not execute tasks");
  assert.equal(f.manager._readPending("a").length, 1);
  writeFileSync(join(f.dir, "release"), "0");
  f.manager.start();
  await until(() => f.starts().length === 2);
  await f.exits[1];
  assert.equal(f.manager.dispatchStatus("a").status, "succeeded");
  const history = JSON.parse(readFileSync(join(f.manager.dataDir, "dispatch-history", job + ".json"), "utf8"));
  assert.equal(history.status, "failed");
  assert.equal(history.code, 7);
});

test("an overridden built-in inbox cannot be removed into an enabled default", async t => {
  const f = fixture(t);
  f.register("inbox");
  f.manager.stop();
  await f.manager.fire("inbox", { payload: "old override" });
  assert.throws(() => f.manager.remove("inbox"), /内置 hook/);
  f.register("inbox", false);
  f.manager.start();
  assert.equal(f.children.length, 0);
  assert.equal(f.manager._readPending("inbox").length, 1);
});

test("inconsistent active journal cannot release the concurrency barrier", async t => {
  const f = fixture(t);
  f.register("a");
  await f.manager.fire("a");
  await until(() => f.starts().length === 1);
  f.manager.stop();
  const file = join(f.manager.dataDir, "dispatch-runs", "a.json");
  const run = JSON.parse(readFileSync(file, "utf8"));
  writeFileSync(file, JSON.stringify({ ...run, active: false }));
  f.manager._enqueuePending("a", run.records[0]);
  f.manager.start();
  assert.equal(f.children.length, 1);
  assert.throws(() => f.manager.dispatchStatus("a"), /journal 无效/);
});

test("interrupted manual recovery keeps a barrier and resumes without doubling the batch", async t => {
  const f = fixture(t);
  f.register("a");
  await f.manager.fire("a");
  await until(() => f.starts().length === 1);
  writeFileSync(join(f.dir, "release"), "7");
  await f.exits[0];
  f.manager.stop();
  const job = f.manager.dispatchStatus("a").job;
  const write = f.manager._writeRun.bind(f.manager);
  f.manager._writeRun = run => {
    if (run.status === "resolved") throw new Error("injected final write failure");
    write(run);
  };
  const options = { job, action: "retry", confirmStopped: true, acknowledgeDuplicateRisk: true };
  assert.throws(() => f.manager.recoverDispatch("a", options), /injected/);
  assert.equal(f.manager.dispatchStatus("a").status, "recovering");
  f.manager._writeRun = write;
  f.manager.recoverDispatch("a", options);
  assert.equal(f.manager._readPending("a").length, 1);
  assert.equal(f.children.length, 1);
});

for (const corruption of ["recovery-after", "started-as-start-failed", "recovery-downgrade"]) {
  test(`durable lifecycle validation rejects ${corruption} without changing pending`, async t => {
    const f = fixture(t, { cooldownMs: 1 });
    f.register("a");
    await f.manager.fire("a", { payload: "original" });
    await until(() => f.starts().length === 1);
    await f.manager.fire("a", { payload: "other queued event" });
    writeFileSync(join(f.dir, "release"), "7");
    await f.exits[0];
    f.manager.stop();
    const file = join(f.manager.dataDir, "dispatch-runs", "a.json");
    const run = JSON.parse(readFileSync(file, "utf8"));
    const pendingFile = join(f.manager._pendingDir, "a.jsonl");
    const before = readFileSync(pendingFile, "utf8");
    if (corruption === "recovery-after") {
      Object.assign(run, { status: "recovering", active: true,
        recovery: { action: "retry", originalStatus: "failed", before: f.manager._readPending("a"), after: [] } });
    } else if (corruption === "recovery-downgrade") {
      Object.assign(run, { status: "start_failed", active: false, consumed: false,
        pid: undefined, startedAt: undefined,
        recovery: { action: "release", originalStatus: "starting", before: run.records, after: [] } });
    } else Object.assign(run, { status: "start_failed", active: false });
    writeFileSync(file, JSON.stringify(run));
    assert.throws(() => f.manager.dispatchStatus("a"), /journal 无效/);
    if (corruption === "recovery-after") assert.throws(() => f.manager.recoverDispatch("a", {
      job: run.job, action: "retry", confirmStopped: true, acknowledgeDuplicateRisk: true,
    }), /journal 无效/);
    f.manager.start();
    assert.equal(f.children.length, 1);
    assert.equal(readFileSync(pendingFile, "utf8"), before);
  });
}
