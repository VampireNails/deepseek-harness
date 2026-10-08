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

test("search retains a Chinese subject after conversational context", () => {
  const { r, cleanup } = freshRetriever();
  try {
    const { id } = r.add({ text: "青铜齿轮装配规范" });
    r.add({ text: "这些说明是旧实验记录，请先阅读。" });
    assert.ok(r.search("请根据前面全部说明为这个特殊实验重新查找青铜齿轮装配规范", 5)
      .some(hit => hit.id === id), "The queried subject must survive conversational prefix");
  } finally {
    cleanup();
  }
});

test("search retains a Latin subject after more than six distinct words", () => {
  const { r, cleanup } = freshRetriever();
  try {
    const { id } = r.add({ text: "heliostat calibration handbook" });
    assert.ok(r.search("please review our previous project notes and find heliostat calibration handbook", 5)
      .some(hit => hit.id === id), "The Latin subject must survive conversational prefix");
  } finally {
    cleanup();
  }
});

test("long distinct-term queries retain their final subject and support either word order", () => {
  const { r, cleanup } = freshRetriever();
  try {
    const { id } = r.add({ text: "heliostat calibration handbook" });
    const context = Array.from({ length: 1000 }, (_, index) => "context" + index);
    const query = [...context, "heliostat", "calibration", "handbook"];
    const forward = r.search(query.join(" "), 3).map(hit => hit.id);
    assert.deepEqual(forward, [id]);
    assert.deepEqual(r.search(query.reverse().join(" "), 3).map(hit => hit.id), forward);
    assert.deepEqual(r.search(context.join(" "), 3), []);
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
