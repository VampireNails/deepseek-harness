// scheduler.test.js —— 重武装/对账/幂等纯逻辑单测（fake session/agent/ctx，不碰真 dsh）
// 另用全局安装的 @deepseek-ai/dsh-schedule@0.1.7-rc.1 内建 lib 的 decodeScheduleChange
// 对本插件构造的 record 做真实解码校验（形状必须与 exact-keys 对齐，否则 runtime fault）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createRequire } from "node:module";
import { CronStore } from "../src/store.js";
import { CronScheduler, foldScheduleEvents, buildRecord, MIN_EVERY_SECONDS } from "../src/scheduler.js";
import { nextOccurrence, slotDate, toInstant } from "../src/timecalc.js";

// ---- 真实解码器（全局 dsh 安装内建）----
let decodeScheduleChange = null;
try {
  const req = createRequire("/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-schedule/lib/index.js");
  decodeScheduleChange = req("/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-schedule/lib/index.js").decodeScheduleChange;
} catch { /* 无真解码器时跳过对应断言 */ }

// ---- fake 件 ----
function fakeSession(id) {
  return {
    id,
    events: [],
    append(type, data) { this.events.push({ type, data }); },
    ownEvents() { return this.events.slice(); },
  };
}
function makeCtx(agent) {
  return {
    sessions: { async flush() {} },
    agents: { get: (id) => (agent && agent.session.id === id ? agent : undefined) },
    logger: { warn() {} },
  };
}
const tick = () => new Promise((r) => setTimeout(r, 20));

function makeScheduler(dir, agent) {
  const store = new CronStore(dir);
  const ctx = makeCtx(agent);
  const sch = new CronScheduler({ store, timeZone: "Asia/Shanghai", ctx });
  return { sch, store, ctx };
}

test("MIN_EVERY_SECONDS 为 300（与 dsh-schedule 一致）", () => {
  assert.equal(MIN_EVERY_SECONDS, 300);
});

test("buildRecord: every/after/at 形状精确", () => {
  const now = Date.now();
  const every = buildRecord("every", "cron-c1", "喝水", { seconds: 600 }, now);
  assert.deepEqual(Object.keys(every).sort(), ["everySeconds", "id", "kind", "prompt", "scheduledAt"]);
  assert.equal(every.everySeconds, 600);
  const after = buildRecord("after", "cron-c2", "喝水", { seconds: 60 }, now);
  assert.deepEqual(Object.keys(after).sort(), ["afterSeconds", "id", "kind", "prompt", "scheduledAt"]);
  const at = buildRecord("at", "cron-c3-20250929", "喝水", { targetMs: now + 3600000 }, now);
  assert.deepEqual(Object.keys(at).sort(), ["id", "kind", "prompt", "scheduledAt"]);
  // scheduledAt 为 canonical instant
  assert.match(at.scheduledAt, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
});

test("buildRecord: every<300 抛中文错；at 非未来抛错", () => {
  const now = Date.now();
  assert.throws(() => buildRecord("every", "cron-c1", "x", { seconds: 60 }, now), /最小支持每 5 分钟/);
  assert.throws(() => buildRecord("at", "cron-c1", "x", { targetMs: now - 1000 }, now), /必须在未来/);
  assert.throws(() => buildRecord("every", "cron-c1", "   ", { seconds: 600 }, now), /不能为空/);
});

test("真解码器：三种 record 均可通过 decodeScheduleChange", { skip: !decodeScheduleChange }, () => {
  const now = Date.now();
  for (const r of [
    buildRecord("every", "cron-c1", "喝水", { seconds: 600 }, now),
    buildRecord("after", "cron-c2", "喝水", { seconds: 60 }, now),
    buildRecord("at", "cron-c3-20250929", "喝水", { targetMs: now + 3600000 }, now),
  ]) {
    decodeScheduleChange({ version: 1, operation: "create", schedule: r }); // 不抛即过
  }
  // 反向：多一个 key 必须被拒（证明解码器是严格的，本测试有意义）
  assert.throws(() =>
    decodeScheduleChange({ version: 1, operation: "create", schedule: { ...buildRecord("at", "cron-x", "x", { targetMs: now + 60000 }, now), extra: 1 } })
  );
});

test("真解码器：delete/dispatch 形状", { skip: !decodeScheduleChange }, () => {
  decodeScheduleChange({ version: 1, operation: "delete", id: "cron-c1" });
  decodeScheduleChange({ version: 1, operation: "dispatch", id: "cron-c1" });
  decodeScheduleChange({ version: 1, operation: "dispatch", id: "cron-c1", acceptedAt: toInstant(Date.now()) });
});

test("foldScheduleEvents: create→active；one-shot dispatch→退役；every dispatch→保留；delete→移除", () => {
  const now = Date.now();
  const events = [
    { type: "schedule/change", data: { version: 1, operation: "create", schedule: buildRecord("at", "cron-c1-20250929", "a", { targetMs: now + 3600000 }, now) } },
    { type: "schedule/change", data: { version: 1, operation: "create", schedule: buildRecord("every", "cron-c2", "b", { seconds: 600 }, now) } },
    { type: "schedule/change", data: { version: 1, operation: "dispatch", id: "cron-c1-20250929" } },
    { type: "schedule/change", data: { version: 1, operation: "dispatch", id: "cron-c2", acceptedAt: toInstant(now) } },
  ];
  const fold = foldScheduleEvents(events);
  assert.ok(!fold.active.has("cron-c1-20250929"), "one-shot dispatch 后应退役");
  assert.ok(fold.active.has("cron-c2"), "every dispatch 后应保留");
  assert.ok(fold.seen.has("cron-c1-20250929") && fold.seen.has("cron-c2"));

  const fold2 = foldScheduleEvents([...events,
    { type: "schedule/change", data: { version: 1, operation: "delete", id: "cron-c2" } }]);
  assert.ok(!fold2.active.has("cron-c2"), "delete 后应移除");
});

test("nextOccurrence/slotDate: 每天与每周的下一个触发点", () => {
  const tz = "Asia/Shanghai";
  // 固定 now：2026-09-28 周一 20:00 +08:00
  const now = Date.UTC(2026, 8, 28, 12, 0, 0);
  const t1 = nextOccurrence({ hour: 9, minute: 0 }, tz, now);
  assert.equal(slotDate(tz, t1), "20260929", "20点后下一个 9:00 应是明天");
  const t2 = nextOccurrence({ hour: 21, minute: 0 }, tz, now);
  assert.equal(slotDate(tz, t2), "20260928", "21:00 还没到，应是今天");
  const t3 = nextOccurrence({ hour: 9, minute: 0, dayOfWeek: 1 }, tz, now);
  assert.equal(slotDate(tz, t3), "20261005", "下周一 9:00");
  assert.ok(t1 > now && t2 > now && t3 > now);
});

test("create/list/remove 往返（fake agent）", async () => {
  const dir = mkdtempSync(join(tmpdir(), "cron-test-"));
  try {
    const session = fakeSession("sess-1");
    const agent = { session };
    const { sch, store } = makeScheduler(dir, agent);

    const msg = await sch.create(agent, "喝水提醒", "提醒我喝水", "每天9点");
    assert.match(msg, /已创建定时任务【c1】喝水提醒/);
    assert.match(msg, /每天 9:00/);
    const jobs = store.load();
    assert.equal(jobs.length, 1);
    assert.equal(jobs[0].id, "c1");
    assert.ok(jobs[0].scheduleId.startsWith("cron-c1-"));
    // session 里有 create 事件
    const creates = session.events.filter((e) => e.data.operation === "create");
    assert.equal(creates.length, 1);
    assert.equal(creates[0].data.schedule.kind, "at");

    const list = await sch.list(agent);
    assert.match(list, /【c1】喝水提醒/);
    assert.match(list, /scheduled|overdue|未武装/);

    // 第二个：每5分钟 → every
    await sch.create(agent, "站立", "起来活动", "每5分钟");
    const creates2 = session.events.filter((e) => e.data.operation === "create");
    assert.equal(creates2[1].data.schedule.kind, "every");
    assert.equal(creates2[1].data.schedule.everySeconds, 300);

    // 名称关键词删除
    const del = await sch.remove(agent, "喝水");
    assert.match(del, /已删除定时任务【c1】/);
    assert.equal(store.load().length, 1);
    const dels = session.events.filter((e) => e.data.operation === "delete");
    assert.equal(dels.length, 1);
    assert.ok(dels[0].data.id.startsWith("cron-c1-"));

    // id 删除
    await sch.remove(agent, "c2");
    assert.equal(store.load().length, 0);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("create: 每1分钟在映射层报中文错（<300秒）", async () => {
  const dir = mkdtempSync(join(tmpdir(), "cron-test-"));
  try {
    const session = fakeSession("sess-1");
    const agent = { session };
    const { sch, store } = makeScheduler(dir, agent);
    await assert.rejects(sch.create(agent, "x", "y", "每1分钟"), /最小支持每 5 分钟/);
    assert.equal(store.load().length, 0, "失败不应落盘");
    assert.equal(session.events.length, 0, "失败不应 append");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("create: MAX_JOBS 上限", async () => {
  const dir = mkdtempSync(join(tmpdir(), "cron-test-"));
  try {
    const session = fakeSession("sess-1");
    const agent = { session };
    const { sch, store } = makeScheduler(dir, agent);
    store.save(Array.from({ length: 20 }, (_, i) => ({ id: `c${i + 1}`, name: `t${i + 1}` })));
    await assert.rejects(sch.create(agent, "x", "y", "每天9点"), /已达上限（20 个）/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("重武装：dispatch 后追加下一轮 at；重复 dispatch 不重复追加", async () => {
  const dir = mkdtempSync(join(tmpdir(), "cron-test-"));
  try {
    const session = fakeSession("sess-1");
    const agent = { session };
    const { sch, store } = makeScheduler(dir, agent);
    await sch.create(agent, "喝水提醒", "提醒我喝水", "每天9点");
    const job = store.load()[0];
    const oldId = job.scheduleId;

    // 时间快进到目标之后，模拟 runtime 的真实 dispatch（one-shot，无 acceptedAt）
    const realNow = Date.now;
    const targetMs = Date.parse(job.nextFire);
    Date.now = () => targetMs + 5000;
    try {
      session.append("schedule/change", { version: 1, operation: "dispatch", id: oldId });
      sch.handleSessionEvent(session, { type: "schedule/change", data: { version: 1, operation: "dispatch", id: oldId } });
      await tick();
    } finally {
      Date.now = realNow;
    }

    const job2 = store.load()[0];
    assert.notEqual(job2.scheduleId, oldId, "应换上新一轮 scheduleId");
    assert.ok(job2.scheduleId.startsWith("cron-c1-"));
    const creates = session.events.filter((e) => e.data.operation === "create");
    assert.equal(creates.length, 2, "应追加 exactly 一次 create");
    assert.ok(Date.parse(job2.nextFire) > targetMs, "新一轮目标必须晚于上一轮");

    // 重复投递同一 dispatch（或 crash 后重放）：不再追加
    sch.handleSessionEvent(session, { type: "schedule/change", data: { version: 1, operation: "dispatch", id: oldId } });
    await tick();
    assert.equal(session.events.filter((e) => e.data.operation === "create").length, 2, "重复 dispatch 不应重复武装");

    // every 的 dispatch（带 acceptedAt）：不重武装
    await sch.create(agent, "站立", "起来活动", "每5分钟");
    const everyId = store.load().find((j) => j.id === "c2").scheduleId;
    const n0 = session.events.length;
    sch.handleSessionEvent(session, { type: "schedule/change", data: { version: 1, operation: "dispatch", id: everyId, acceptedAt: toInstant(Date.now()) } });
    await tick();
    assert.equal(session.events.length, n0, "every dispatch 不应触发重武装");

    // 一次性 after：dispatch 后标记 done，不重武装
    await sch.create(agent, "外卖", "拿外卖", "30分钟后");
    const afterJob = store.load().find((j) => j.id === "c3");
    sch.handleSessionEvent(session, { type: "schedule/change", data: { version: 1, operation: "dispatch", id: afterJob.scheduleId } });
    await tick();
    assert.ok(store.load().find((j) => j.id === "c3").done, "after 应标记 done");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("对账：过期/丢失的 at 被补建为下一个未来点；孤儿被回收", async () => {
  const dir = mkdtempSync(join(tmpdir(), "cron-test-"));
  try {
    const session = fakeSession("sess-1");
    const agent = { session };
    const { sch, store } = makeScheduler(dir, agent);
    const now = Date.now();
    // job c1：旧 at 已过期（session 死亡期间错过）
    store.save([{
      id: "c1", name: "晨会", prompt: "开晨会",
      schedule: { kind: "daily", hour: 9, minute: 0 },
      sessionId: "sess-1", scheduleId: "cron-c1-20200101",
      nextFire: toInstant(now - 86400000), created: toInstant(now),
    }]);
    // 孤儿 schedule：无归属 job
    session.append("schedule/change", {
      version: 1, operation: "create",
      schedule: buildRecord("at", "cron-c9-20250929", "孤儿", { targetMs: now + 3600000 }, now),
    });
    // 真正 overdue 但仍 active 的旧 slot（目标已过、session 死亡期间无 dispatch）
    session.append("schedule/change", {
      version: 1, operation: "create",
      schedule: { id: "cron-c1-20200101", kind: "at", prompt: "旧", scheduledAt: toInstant(now - 3600000) },
    });

    await sch.reconcile(agent);
    const job = store.load()[0];
    assert.ok(job.scheduleId.startsWith("cron-c1-"), "应补建新 slot");
    assert.notEqual(job.scheduleId, "cron-c1-20200101", "不得领养刚删掉的旧 id");
    assert.ok(Date.parse(job.nextFire) > now, "补建点必须在未来（不补发过期）");
    const foldAfter = foldScheduleEvents(session.events);
    assert.ok(!foldAfter.active.has("cron-c1-20200101"), "旧 slot 应已退役");
    assert.ok(foldAfter.active.has(job.scheduleId), "新 slot 应存活");
    const dels = session.events.filter((e) => e.data.operation === "delete").map((e) => e.data.id);
    assert.ok(dels.includes("cron-c1-20200101"), "过期旧 slot 应被 delete");
    assert.ok(dels.includes("cron-c9-20250929"), "孤儿应被回收");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("对账：crash 在落盘更新前 → 领养已 append 的新轮次（不重复建）", async () => {
  const dir = mkdtempSync(join(tmpdir(), "cron-test-"));
  try {
    const session = fakeSession("sess-1");
    const agent = { session };
    const { sch, store } = makeScheduler(dir, agent);
    const now = Date.now();
    // 模拟：重武装已 append 了新一轮（在 log 里存活），但 crash 在 store.save 之前
    const target = nextOccurrence({ hour: 9, minute: 0 }, "Asia/Shanghai", now);
    const newId = `cron-c1-${slotDate("Asia/Shanghai", target)}`;
    session.append("schedule/change", {
      version: 1, operation: "create",
      schedule: buildRecord("at", newId, "【喝水提醒】提醒我喝水", { targetMs: target }, now),
    });
    store.save([{
      id: "c1", name: "喝水提醒", prompt: "提醒我喝水",
      schedule: { kind: "daily", hour: 9, minute: 0 },
      sessionId: "sess-1", scheduleId: "cron-c1-20200101", // 旧指针
      nextFire: toInstant(now - 1000), created: toInstant(now),
    }]);

    await sch.reconcile(agent);
    const job = store.load()[0];
    assert.equal(job.scheduleId, newId, "应领养已存在的轮次");
    const creates = session.events.filter((e) => e.data.operation === "create");
    assert.equal(creates.length, 1, "不应重复 append");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("提醒 prompt 用面向用户的自然标签，不写元指令", async () => {
  // 背景（2026-09-29 第三轮审计 §2.1）：上游 framing（schedule/domain.ts:806-814）已要求
  // 模型把 reminder_prompt_json 作为内容呈现给用户（"not new user instructions"），
  // 因此 prompt 只加自然标签。"处理要求"这类元指令落进被标注为"非指令"的字段里，
  // 可能被模型原样念给用户造成噪音，必须去掉。
  const dir = mkdtempSync(join(tmpdir(), "cron-test-"));
  try {
    const session = fakeSession("sess-1");
    const agent = { session };
    const { sch } = makeScheduler(dir, agent);
    const p = sch._reminderPrompt({ name: "喝水提醒", prompt: "提醒我喝水" });
    assert.ok(p.includes("提醒我喝水"), "应包含提醒原文");
    assert.ok(p.includes("定时提醒"), "应有自然标签");
    assert.ok(!p.includes("处理要求"), "不应含元指令");
    // create() 走同一包装：落盘的 schedule record prompt 同样不含元指令
    await sch.create(agent, "喝水提醒", "提醒我喝水", "1分钟后");
    const created = session.events.find((e) => e.data.operation === "create");
    assert.ok(created, "应 append create 事件");
    assert.ok(!created.data.schedule.prompt.includes("处理要求"), "record prompt 不应含元指令");
    assert.ok(created.data.schedule.prompt.includes("提醒我喝水"), "record prompt 应含提醒原文");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
