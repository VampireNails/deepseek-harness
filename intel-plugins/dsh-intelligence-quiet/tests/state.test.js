import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { QuietState } from "../src/state.js";
import { REFLECT_MARKER } from "../index.js";

test("QuietState 读写 + 持久化", () => {
  const dir = mkdtempSync(join(tmpdir(), "quiet-test-"));
  try {
    const s = new QuietState(dir);
    assert.equal(s.get("lastAt", 0), 0);
    s.set("lastAt", 12345);
    assert.equal(s.get("lastAt", 0), 12345);
    // 重建后仍在
    const s2 = new QuietState(dir);
    assert.equal(s2.get("lastAt", 0), 12345);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("冷却语义：5 分钟内不重复触发", () => {
  const dir = mkdtempSync(join(tmpdir(), "quiet-test-"));
  try {
    const s = new QuietState(dir);
    const now = Date.now();
    s.set("lastAt", now - 60 * 1000); // 1 分钟前
    const cooled = now - s.get("lastAt", 0) < 300 * 1000;
    assert.ok(cooled, "1 分钟前应处于冷却");
    s.set("lastAt", now - 400 * 1000); // 400 秒前
    const cooled2 = now - s.get("lastAt", 0) < 300 * 1000;
    assert.ok(!cooled2, "400 秒前应已过冷却");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("REFLECT_MARKER 存在且为防递归标记", () => {
  assert.ok(REFLECT_MARKER.includes("quiet-moment-reflection"));
});
