import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, mkdirSync, writeFileSync, chmodSync, existsSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { aggregateInWorker } from "../src/runner.js";

test("worker 返回只读聚合结果并释放后返回", async t => {
  const sessionsRoot = mkdtempSync(join(tmpdir(), "toklog-worker-"));
  t.after(() => rmSync(sessionsRoot, { recursive: true, force: true }));
  assert.deepEqual(await aggregateInWorker(1, { sessionsRoot }),
    { days: 1, sessions: 0, skipped: 0, byTask: {}, byDay: {} });
});

test("已取消的调用不启动 worker", async () => {
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(aggregateInWorker(1, { signal: controller.signal }), /TOKENLOG_CANCELLED/);
});

test("扫描期间取消并等待 worker 退出", async t => {
  const sessionsRoot = mkdtempSync(join(tmpdir(), "toklog-cancel-"));
  t.after(() => rmSync(sessionsRoot, { recursive: true, force: true }));
  const controller = new AbortController();
  const pending = aggregateInWorker(1, { sessionsRoot, signal: controller.signal });
  controller.abort();
  await assert.rejects(pending, /TOKENLOG_CANCELLED/);
});

test("worker 运行错误投影固定安全错误", async t => {
  const sessionsRoot = mkdtempSync(join(tmpdir(), "toklog-error-"));
  t.after(() => rmSync(sessionsRoot, { recursive: true, force: true }));
  await assert.rejects(aggregateInWorker(1n, { sessionsRoot }), /TOKENLOG_READ_FAILED/);
});

test("超时终止 worker 且不会继续扫描", async t => {
  const sessionsRoot = mkdtempSync(join(tmpdir(), "toklog-timeout-"));
  t.after(() => rmSync(sessionsRoot, { recursive: true, force: true }));
  await assert.rejects(aggregateInWorker(1, { sessionsRoot, timeoutMs: 0 }), /TOKENLOG_TIMEOUT/);
});

function slowDecompression(t) {
  const root = mkdtempSync(join(tmpdir(), "toklog-slow-"));
  const previous = process.env.PATH;
  t.after(() => {
    if (previous === undefined) delete process.env.PATH;
    else process.env.PATH = previous;
    rmSync(root, { recursive: true, force: true });
  });
  const sessionsRoot = join(root, "sessions");
  mkdirSync(join(sessionsRoot, "one"), { recursive: true });
  writeFileSync(join(sessionsRoot, "one", "session.v4.jsonl.zstd"), "");
  const marker = join(root, "started");
  const command = join(root, "zstd");
  writeFileSync(command, `#!/bin/sh\nprintf '%s' "$$" > ${marker}\nexec sleep 300\n`);
  chmodSync(command, 0o700);
  process.env.PATH = root + ":" + previous;
  return { sessionsRoot, marker };
}

async function started(marker) {
  const deadline = Date.now() + 5000;
  while (!existsSync(marker)) {
    if (Date.now() >= deadline) throw new Error("Decompression did not start");
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  return Number(readFileSync(marker, "utf8"));
}

test("实际解压开始后的取消会清理 worker 和子进程", { skip: process.platform !== "linux" }, async t => {
  const { sessionsRoot, marker } = slowDecompression(t);
  const controller = new AbortController();
  const pending = aggregateInWorker(1,
    { sessionsRoot, signal: controller.signal, timeoutMs: 10000, decompressTimeoutMs: 1000 });
  const rejected = assert.rejects(pending, /TOKENLOG_CANCELLED/);
  try {
    const pid = await started(marker);
    controller.abort();
    await rejected;
    assert.throws(() => process.kill(pid, 0), { code: "ESRCH" });
  } finally {
    controller.abort();
    await pending.catch(() => {});
  }
});

test("实际解压开始后的总超时不会返回成功或遗留子进程", { skip: process.platform !== "linux" }, async t => {
  const { sessionsRoot, marker } = slowDecompression(t);
  const pending = aggregateInWorker(1, { sessionsRoot, timeoutMs: 2000, decompressTimeoutMs: 5000 });
  const rejected = assert.rejects(pending, /TOKENLOG_TIMEOUT/);
  const pid = await started(marker);
  await rejected;
  assert.throws(() => process.kill(pid, 0), { code: "ESRCH" });
});
