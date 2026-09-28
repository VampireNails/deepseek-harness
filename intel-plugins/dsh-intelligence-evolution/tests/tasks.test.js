import { test } from "node:test";
import assert from "node:assert/strict";
import { chunk, TASKS } from "../src/tasks.js";

test("chunk 切分", () => {
  assert.deepEqual(chunk([1, 2, 3, 4], 3), [[1, 2, 3], [4]]);
  assert.deepEqual(chunk([1, 2, 3], 3), [[1, 2, 3]]);
  assert.deepEqual(chunk([], 3), []);
  assert.deepEqual(chunk([1], 5), [[1]]);
});

test("五个进化任务 prompt 都存在", () => {
  const keys = Object.keys(TASKS);
  for (const k of ["memory_upkeep", "studying", "idea_curation", "dreaming", "skill_review", "heartbeat"]) {
    assert.ok(keys.includes(k), `缺少任务 ${k}`);
    assert.ok(TASKS[k].prompt.length > 20, `${k} prompt 过短`);
  }
});

test("heartbeat prompt 要求先调 heartbeat_check", () => {
  assert.ok(TASKS.heartbeat.prompt.includes("heartbeat_check"));
});
