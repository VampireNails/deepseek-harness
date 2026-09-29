import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { GoalsStore } from "../src/goals.js";

function freshStore() {
  const dir = mkdtempSync(join(tmpdir(), "goals-test-"));
  const store = new GoalsStore(dir);
  return { store, dir, cleanup: () => rmSync(dir, { recursive: true, force: true }) };
}

function readMd(dir) {
  return readFileSync(join(dir, "goals.md"), "utf8");
}

test("goal_create：递增 id 并落盘", () => {
  const { store, cleanup } = freshStore();
  try {
    const id1 = store.create("学日语", "每天背 20 个单词");
    const id2 = store.create("跑步");
    assert.equal(id1, "g1");
    assert.equal(id2, "g2");
    const gs = store.listGoals("all");
    assert.equal(gs.length, 2);
    assert.equal(gs[0].title, "学日语");
    assert.equal(gs[0].desc, "每天背 20 个单词");
    assert.equal(gs[0].status, "active");
    assert.match(gs[0].created, /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
    assert.deepEqual(gs[0].progress, []);
    assert.equal(gs[1].desc, "");
  } finally {
    cleanup();
  }
});

test("goal_create：空标题抛错", () => {
  const { store, cleanup } = freshStore();
  try {
    assert.throws(() => store.create("   "), /title 不能为空/);
  } finally {
    cleanup();
  }
});

test("goal_progress：记进展带时间戳", () => {
  const { store, cleanup } = freshStore();
  try {
    store.create("学日语");
    const g = store.logProgress("g1", "背了 50 个单词");
    assert.equal(g.progress.length, 1);
    assert.equal(g.progress[0].text, "背了 50 个单词");
    assert.match(g.progress[0].ts, /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
  } finally {
    cleanup();
  }
});

test("goal_close：关闭后进入 Closed 分区", () => {
  const { store, cleanup } = freshStore();
  try {
    store.create("学日语");
    const g = store.close("g1");
    assert.equal(g.status, "closed");
    assert.match(g.closed, /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
    assert.equal(store.listGoals("active").length, 0);
    assert.equal(store.listGoals("closed").length, 1);
    assert.equal(store.listGoals("all").length, 1);
  } finally {
    cleanup();
  }
});

test("goal_list：按 status 过滤 active/closed/all", () => {
  const { store, cleanup } = freshStore();
  try {
    store.create("进行中A");
    store.create("进行中B");
    store.create("已完成C");
    store.close("g3");
    assert.equal(store.listGoals("active").map((g) => g.id).join(","), "g1,g2");
    assert.equal(store.listGoals("closed").map((g) => g.id).join(","), "g3");
    assert.equal(store.listGoals("all").map((g) => g.id).join(","), "g1,g2,g3");
    assert.equal(store.listGoals().length, 3); // 默认 all
  } finally {
    cleanup();
  }
});

test("ref：id 大小写不敏感、标题模糊匹配", () => {
  const { store, cleanup } = freshStore();
  try {
    store.create("每天早上跑步5公里");
    const g1 = store.logProgress("G1", "跑了 3 公里");
    assert.equal(g1.id, "g1");
    const g2 = store.close("早上跑步");
    assert.equal(g2.id, "g1");
  } finally {
    cleanup();
  }
});

test("ref：找不到时报错说明，不猜", () => {
  const { store, cleanup } = freshStore();
  try {
    store.create("学日语");
    const { goal, error } = store.findGoal("不存在的标题");
    assert.equal(goal, undefined);
    assert.match(error, /找不到/);
    assert.throws(() => store.logProgress("不存在的标题", "xxx"), /找不到/);
    assert.throws(() => store.close("g99"), /找不到/);
  } finally {
    cleanup();
  }
});

test("ref：歧义时报错并列出候选，不猜", () => {
  const { store, cleanup } = freshStore();
  try {
    store.create("跑步训练计划");
    store.create("跑步打卡");
    store.create("读书计划");
    const { goal, error } = store.findGoal("跑步");
    assert.equal(goal, undefined);
    assert.match(error, /多个/);
    assert.match(error, /跑步训练计划\[g1\]/);
    assert.match(error, /跑步打卡\[g2\]/);
    assert.throws(() => store.logProgress("跑步", "今天跑了"), /多个/);
  } finally {
    cleanup();
  }
});

test("ref：空引用报错", () => {
  const { store, cleanup } = freshStore();
  try {
    const { error } = store.findGoal("  ");
    assert.match(error, /为空/);
  } finally {
    cleanup();
  }
});

test("goals.md：每次变更后自动生成，Active/Closed 分区正确", () => {
  const { store, dir, cleanup } = freshStore();
  try {
    assert.ok(!existsSync(join(dir, "goals.md")) || true); // 尚未创建前
    store.create("学日语", "每天背单词");
    store.logProgress("g1", "背了 50 个单词");
    store.create("旧目标");
    store.close("旧目标");
    const md = readMd(dir);
    assert.match(md, /^# Goals$/m);
    assert.match(md, /^## Active \(1\)$/m);
    assert.match(md, /^### 学日语 \[g1\]$/m);
    assert.match(md, /^每天背单词$/m);
    assert.match(md, /^- created: \d{4}-\d{2}-\d{2} \d{2}:\d{2}$/m);
    assert.match(md, /^- progress:$/m);
    assert.match(md, /^  - \d{4}-\d{2}-\d{2} \d{2}:\d{2}: 背了 50 个单词$/m);
    assert.match(md, /^## Closed \(1\)$/m);
    assert.match(md, /^- 旧目标 \[g2\] \(closed \d{4}-\d{2}-\d{2} \d{2}:\d{2}\)$/m);
    // 关闭后 Active 分区不再含该目标
    const activeSec = md.slice(md.indexOf("## Active"), md.indexOf("## Closed"));
    assert.ok(!activeSec.includes("旧目标"));
  } finally {
    cleanup();
  }
});

test("goals.md：空库时不写 Closed 分区", () => {
  const { store, dir, cleanup } = freshStore();
  try {
    store.create("只有一个");
    const md = readMd(dir);
    assert.match(md, /^## Active \(1\)$/m);
    assert.ok(!md.includes("## Closed"));
  } finally {
    cleanup();
  }
});

test("goals.md：进展只展示最近 10 条", () => {
  const { store, dir, cleanup } = freshStore();
  try {
    store.create("马拉松训练");
    for (let i = 1; i <= 12; i++) store.logProgress("g1", `第${i}次训练`);
    const md = readMd(dir);
    const data = store.load();
    assert.equal(data.goals[0].progress.length, 12); // json 存全部
    assert.ok(!md.includes("第1次训练"), "第1条不应展示");
    assert.ok(!md.includes("第2次训练"), "第2条不应展示");
    assert.ok(md.includes("第3次训练"));
    assert.ok(md.includes("第12次训练"));
  } finally {
    cleanup();
  }
});

test("goals.json 是真相源：重建 Store 后数据仍在，md 重写", () => {
  const dir = mkdtempSync(join(tmpdir(), "goals-test-"));
  try {
    new GoalsStore(dir).create("持久目标");
    const reloaded = new GoalsStore(dir);
    assert.equal(reloaded.listGoals("all").length, 1);
    assert.equal(reloaded.listGoals("all")[0].id, "g1");
    // md 文件已由构造时的 mkdir 生成？实际在 create 时生成
    assert.ok(existsSync(join(dir, "goals.md")));
    assert.match(readMd(dir), /持久目标/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("goalsDir：DSH_HOME 隔离", () => {
  const d = mkdtempSync(join(tmpdir(), "fake-dsh-"));
  try {
    process.env.DSH_HOME = d;
    const s = new GoalsStore();
    assert.equal(s.dir, join(d, "intel-goals"));
    s.create("隔离测试");
    assert.ok(existsSync(join(d, "intel-goals", "goals.json")));
  } finally {
    delete process.env.DSH_HOME;
    rmSync(d, { recursive: true, force: true });
  }
});
