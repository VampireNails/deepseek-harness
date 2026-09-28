import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { FeedStore } from "../src/feed.js";

function freshStore() {
  const dir = mkdtempSync(join(tmpdir(), "feed-test-"));
  const store = new FeedStore(dir);
  return { store, cleanup: () => rmSync(dir, { recursive: true, force: true }) };
}

test("addPost 写入并返回递增 id", () => {
  const { store, cleanup } = freshStore();
  try {
    const id1 = store.addPost("标题1", "正文1", "测试");
    const id2 = store.addPost("标题2", "正文2");
    assert.equal(id1, "f1");
    assert.equal(id2, "f2");
    const posts = store.listPosts(10);
    assert.equal(posts.length, 2);
    assert.equal(posts[0].id, "f2"); // 新在前
    assert.equal(posts[0].title, "标题2");
    assert.equal(posts[1].source, "测试");
    assert.match(posts[0].ts, /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
  } finally {
    cleanup();
  }
});

test("上限 100 条，超量丢弃最旧", () => {
  const { store, cleanup } = freshStore();
  try {
    for (let i = 0; i < 105; i++) store.addPost(`t${i}`, `b${i}`);
    const posts = store.listPosts(200);
    assert.equal(posts.length, 100);
    assert.equal(posts[0].title, "t104");
    assert.equal(posts[99].title, "t5"); // t0..t4 被丢弃
  } finally {
    cleanup();
  }
});

test("listPosts 默认行为与空库", () => {
  const { store, cleanup } = freshStore();
  try {
    assert.deepEqual(store.listPosts(), []);
    store.addPost("a", "b");
    store.addPost("c", "d");
    store.addPost("e", "f");
    assert.equal(store.listPosts(2).length, 2);
    assert.equal(store.listPosts(0).length, 0);
  } finally {
    cleanup();
  }
});

test("持久化：重建 Store 后数据仍在", () => {
  const dir = mkdtempSync(join(tmpdir(), "feed-test-"));
  try {
    new FeedStore(dir).addPost("持久", "内容");
    const posts = new FeedStore(dir).listPosts(5);
    assert.equal(posts.length, 1);
    assert.equal(posts[0].id, "f1");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
