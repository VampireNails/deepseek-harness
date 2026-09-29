import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, mkdirSync, writeFileSync, existsSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ArtifactStore } from "../src/artifacts.js";

function freshStore() {
  const dir = mkdtempSync(join(tmpdir(), "artifacts-test-"));
  const store = new ArtifactStore(dir);
  return { store, dir, cleanup: () => rmSync(dir, { recursive: true, force: true }) };
}

test("首次保存：返回 id/title/kind/version/url，落盘 v1 文件", () => {
  const { store, dir, cleanup } = freshStore();
  try {
    const m = store.save("周报", "# 第一周", "markdown");
    assert.ok(m.id && typeof m.id === "string");
    assert.equal(m.title, "周报");
    assert.equal(m.kind, "markdown");
    assert.equal(m.version, 1);
    assert.equal(m.url, `/artifacts/${m.id}`);
    assert.equal(m.path, m.id);
    assert.ok(existsSync(join(dir, m.id, "v1.md")));
    assert.ok(existsSync(join(dir, m.id, "meta.json")));
    const got = store.get(m.id);
    assert.equal(got.content, "# 第一周");
    assert.equal(got.version, 1);
  } finally {
    cleanup();
  }
});

test("同标题再次保存：复用旧 id、追加为新版本", () => {
  const { store, cleanup } = freshStore();
  try {
    const m1 = store.save("周报", "# 第一周");
    const m2 = store.save("周报", "# 第二周");
    assert.equal(m2.id, m1.id);
    assert.equal(m2.version, 2);
    // 默认取最新
    const latest = store.get(m1.id);
    assert.equal(latest.content, "# 第二周");
    assert.equal(latest.version, 2);
    // 不同标题得到新 id
    const m3 = store.save("月报", "# 一月");
    assert.notEqual(m3.id, m1.id);
    assert.equal(m3.version, 1);
  } finally {
    cleanup();
  }
});

test("指定版本读取：v1 / v2 各取所得", () => {
  const { store, cleanup } = freshStore();
  try {
    const m = store.save("文档", "版本一");
    store.save("文档", "版本二");
    store.save("文档", "版本三");
    assert.equal(store.get(m.id, 1).content, "版本一");
    assert.equal(store.get(m.id, 2).content, "版本二");
    assert.equal(store.get(m.id, 3).content, "版本三");
    assert.equal(store.read(m.id).content, "版本三"); // read = 最新版便捷入口
    assert.equal(store.get(m.id, 99), null); // 不存在的版本
  } finally {
    cleanup();
  }
});

test("版本列表：文本含版本数与每版记录", () => {
  const { store, cleanup } = freshStore();
  try {
    const m = store.save("文档", "v1内容");
    store.save("文档", "v2内容");
    const text = store.versionsText(m.id);
    assert.match(text, /共 2 个版本/);
    assert.match(text, /- v1\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+v1\.md/);
    assert.match(text, /- v2\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+v2\.md/);
    assert.equal(store.versionsText("no-such-id"), "未找到该产物");
  } finally {
    cleanup();
  }
});

test("旧格式兼容：meta 无 versions + artifact.md 被视为 v1", () => {
  const { store, dir, cleanup } = freshStore();
  try {
    // 手工造旧格式目录（Artifacts v1 风格）：有 meta、无 versions、单文件 artifact.md
    const aid = "legacy-dir";
    mkdirSync(join(dir, aid), { recursive: true });
    writeFileSync(
      join(dir, aid, "meta.json"),
      JSON.stringify({ id: aid, title: "旧文档", kind: "markdown", created_at: "2026-01-01 00:00:00" }),
      "utf8"
    );
    writeFileSync(join(dir, aid, "artifact.md"), "旧内容", "utf8");

    const got = store.get(aid);
    assert.equal(got.content, "旧内容");
    assert.equal(got.version, 1);
    assert.equal(store.versionsText(aid), "产物「旧文档」暂无版本记录（旧格式单文件）");

    // 同标题再保存：旧文件计为 v1，新内容成为 v2，id 复用
    const m = store.save("旧文档", "新内容");
    assert.equal(m.id, aid);
    assert.equal(m.version, 2);
    assert.equal(store.get(aid, 1).content, "旧内容");
    assert.equal(store.get(aid).content, "新内容");
  } finally {
    cleanup();
  }
});

test("HTML 产物：元数据标注 kind=html，文件扩展名 .html", () => {
  const { store, dir, cleanup } = freshStore();
  try {
    const m = store.save("看板", "<h1>hi</h1>", "html");
    assert.equal(m.kind, "html");
    assert.equal(m.version, 1);
    assert.ok(existsSync(join(dir, m.id, "v1.html")));
    const got = store.get(m.id);
    assert.equal(got.kind, "html");
    assert.equal(got.content, "<h1>hi</h1>");
  } finally {
    cleanup();
  }
});


test("HTML 产物：同标题追加 v2.html，版本回读正确", () => {
  const { store, dir, cleanup } = freshStore();
  try {
    const m1 = store.save("HTML 看板", "<p>v1</p>", "html");
    const m2 = store.save("HTML 看板", "<p>v2</p>", "html");
    assert.equal(m2.id, m1.id);
    assert.equal(m2.version, 2);
    assert.ok(existsSync(join(dir, m1.id, "v1.html")));
    assert.ok(existsSync(join(dir, m1.id, "v2.html")));
    assert.ok(!existsSync(join(dir, m1.id, "v2.md")));
    assert.equal(store.get(m1.id, 1).content, "<p>v1</p>");
    assert.equal(store.get(m1.id, 2).content, "<p>v2</p>");
    assert.equal(store.read(m1.id).content, "<p>v2</p>");
    assert.match(store.versionsText(m1.id), /v2\.html/);
    const meta = JSON.parse(readFileSync(join(dir, m1.id, "meta.json"), "utf8"));
    assert.equal(meta.kind, "html");
  } finally {
    cleanup();
  }
});

test("kind 大小写不敏感：HTML 归一化为 html", () => {
  const { store, cleanup } = freshStore();
  try {
    const m = store.save("大小写", "<p>x</p>", "HTML");
    assert.equal(m.kind, "html");
  } finally {
    cleanup();
  }
});

test("kind 非法值回退 markdown；路径穿越被拒绝", () => {
  const { store, cleanup } = freshStore();
  try {
    const m = store.save("怪", "x", "weird-kind");
    assert.equal(m.kind, "markdown");
    assert.equal(store.get("../dsh-intelligence-artifacts"), null);
    assert.equal(store.get(""), null);
    assert.equal(store.versionsText("../../etc"), "未找到该产物");
    assert.equal(store.get("no-such-id"), null);
  } finally {
    cleanup();
  }
});

test("持久化：重建 Store 后版本与内容仍在", () => {
  const dir = mkdtempSync(join(tmpdir(), "artifacts-test-"));
  try {
    const s1 = new ArtifactStore(dir);
    const m = s1.save("持久", "一", "text");
    s1.save("持久", "二", "text");
    const s2 = new ArtifactStore(dir);
    assert.equal(s2.read(m.id).content, "二");
    assert.equal(s2.get(m.id, 1).content, "一");
    assert.match(s2.versionsText(m.id), /共 2 个版本/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
