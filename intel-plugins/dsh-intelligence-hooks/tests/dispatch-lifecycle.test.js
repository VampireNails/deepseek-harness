import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { HookManager } from "../src/manager.js";
import { HookStore } from "../src/store.js";

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
    return existsSync(file) ? readFileSync(file, "utf8").trim().split("\n").map(JSON.parse) : [];
  }
  return { manager, dir, children, warnings, register, starts, exits };
}

function requireSeparator() { return process.platform === "win32" ? "\\" : "/"; }

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
