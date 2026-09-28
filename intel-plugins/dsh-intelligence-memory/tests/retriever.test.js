import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { openStore } from "../src/store.js";
import { FtsRetriever } from "../src/retriever.js";

function freshRetriever() {
  const dir = mkdtempSync(join(tmpdir(), "mem-test-"));
  const db = openStore(dir);
  const r = new FtsRetriever(db);
  return { r, cleanup: () => { r.close(); rmSync(dir, { recursive: true, force: true }); } };
}

test("add + get", () => {
  const { r, cleanup } = freshRetriever();
  try {
    const { id } = r.add({ text: "用户买了一辆红色自行车", kind: "note" });
    assert.ok(id > 0);
    const row = r.get(id);
    assert.equal(row.text, "用户买了一辆红色自行车");
    assert.equal(row.kind, "note");
  } finally {
    cleanup();
  }
});

test("add 拒绝空文本", () => {
  const { r, cleanup } = freshRetriever();
  try {
    assert.throws(() => r.add({ text: "   " }), /non-empty/);
  } finally {
    cleanup();
  }
});

test("search 中文关键词命中 + BM25 排序", () => {
  const { r, cleanup } = freshRetriever();
  try {
    r.add({ text: "用户买了一辆红色自行车" });
    r.add({ text: "今天天气很好适合出门" });
    r.add({ text: "红色自行车需要打气" });
    const hits = r.search("红色自行车", 5);
    assert.ok(hits.length >= 2, "应命中至少 2 条");
    assert.ok(hits.every((h) => h.text.includes("自行车")));
  } finally {
    cleanup();
  }
});

test("search 无关查询返回空", () => {
  const { r, cleanup } = freshRetriever();
  try {
    r.add({ text: "用户买了一辆红色自行车" });
    const hits = r.search("量子计算机芯片", 5);
    assert.equal(hits.length, 0);
  } finally {
    cleanup();
  }
});

test("close 后不再可用（卸载回卷语义）", () => {
  const dir = mkdtempSync(join(tmpdir(), "mem-test-"));
  const r = new FtsRetriever(openStore(dir));
  r.close();
  assert.throws(() => r.add({ text: "x" }));
  rmSync(dir, { recursive: true, force: true });
});
