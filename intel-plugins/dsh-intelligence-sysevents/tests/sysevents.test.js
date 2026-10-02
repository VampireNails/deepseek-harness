import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

import { emitEvent, listEvents, SEVERITIES } from "../src/store.js";
import { apply, name } from "../index.js";

test("emit 落盘并返回事件", () => {
  const dir = mkdtempSync(join(tmpdir(), "sysev-"));
  const ev = emitEvent({ type: "heartbeat.anomaly", severity: "high", title: "dsh-prod 挂了", detail: "x" }, dir);
  assert.ok(ev.id);
  assert.equal(ev.severity, "high");
  assert.equal(ev.type, "heartbeat.anomaly");
});

test("severity 非法回落 normal", () => {
  const dir = mkdtempSync(join(tmpdir(), "sysev-"));
  const ev = emitEvent({ type: "t", title: "x", severity: "urgent" }, dir);
  assert.equal(ev.severity, "normal");
});

test("type/title 为空抛错", () => {
  const dir = mkdtempSync(join(tmpdir(), "sysev-"));
  assert.throws(() => emitEvent({ type: "", title: "x" }, dir));
  assert.throws(() => emitEvent({ type: "t", title: " " }, dir));
});

function tickMs() { const t = Date.now(); while (Date.now() === t) {} } // 保证时间戳 ms 不同
test("listEvents 支持 since 游标与 limit", () => {
  const dir = mkdtempSync(join(tmpdir(), "sysev-"));
  const e1 = emitEvent({ type: "a", title: "1" }, dir); tickMs();
  const e2 = emitEvent({ type: "b", title: "2" }, dir); tickMs();
  const e3 = emitEvent({ type: "c", title: "3" }, dir);
  assert.equal(listEvents({}, dir).length, 3);
  assert.equal(listEvents({ since: e1.ts, limit: 10 }, dir).length, 3); // >= 含游标本身
  assert.equal(listEvents({ limit: 1 }, dir).length, 1);
  assert.equal(listEvents({ since: e3.ts }, dir).length, 1); // >= 语义：游标本身被包含，App 按 id 去重
});

test("空目录返回空数组不抛错", () => {
  const dir = mkdtempSync(join(tmpdir(), "sysev-"));
  assert.deepEqual(listEvents({}, dir), []);
});

test("插件 apply 注册两个工具", () => {
  let effectCalled = false;
  const ctx = { effect: () => { effectCalled = true; }, provide: () => {} };
  apply(ctx);
  assert.equal(effectCalled, true);
  assert.equal(name, "dsh-intelligence-sysevents");
  assert.ok(SEVERITIES.high);
});
