// parse.test.js —— 中文时间解析单测（移植 Python custom_cron.py 全部 parse 用例 + 非法输入 + 新增一次性表达）
import { test } from "node:test";
import assert from "node:assert/strict";
import { parseSchedule, describeSchedule, MAX_JOBS } from "../src/parse.js";

test("MAX_JOBS 为 20（与 Python 一致）", () => {
  assert.equal(MAX_JOBS, 20);
});

test("每天9点 → daily 9:00", () => {
  assert.deepEqual(parseSchedule("每天9点"), { kind: "daily", hour: 9, minute: 0 });
});

test("每天9点半 → daily 9:30", () => {
  assert.deepEqual(parseSchedule("每天9点半"), { kind: "daily", hour: 9, minute: 30 });
});

test("每天9:30 → daily 9:30", () => {
  assert.deepEqual(parseSchedule("每天9:30"), { kind: "daily", hour: 9, minute: 30 });
});

test("每天9：30（全角冒号）→ daily 9:30", () => {
  assert.deepEqual(parseSchedule("每天9：30"), { kind: "daily", hour: 9, minute: 30 });
});

test("每日8点 → daily 8:00", () => {
  assert.deepEqual(parseSchedule("每日8点"), { kind: "daily", hour: 8, minute: 0 });
});

test("每2小时 → interval 7200", () => {
  assert.deepEqual(parseSchedule("每2小时"), { kind: "interval", seconds: 7200 });
});

test("每30分钟 → interval 1800", () => {
  assert.deepEqual(parseSchedule("每30分钟"), { kind: "interval", seconds: 1800 });
});

test("每小时 → interval 3600", () => {
  assert.deepEqual(parseSchedule("每小时"), { kind: "interval", seconds: 3600 });
});

test("每半小时 → interval 1800", () => {
  assert.deepEqual(parseSchedule("每半小时"), { kind: "interval", seconds: 1800 });
});

test("每1分钟 → interval 60（解析通过，dsh 映射层再报<300秒中文错）", () => {
  assert.deepEqual(parseSchedule("每1分钟"), { kind: "interval", seconds: 60 });
});

test("每周一9点 → weekly 周一 9:00", () => {
  assert.deepEqual(parseSchedule("每周一9点"), { kind: "weekly", dayOfWeek: 1, hour: 9, minute: 0 });
});

test("每周三18:30 → weekly 周三 18:30", () => {
  assert.deepEqual(parseSchedule("每周三18:30"), { kind: "weekly", dayOfWeek: 3, hour: 18, minute: 30 });
});

test("星期天10点 → weekly 周日 10:00", () => {
  assert.deepEqual(parseSchedule("星期天10点"), { kind: "weekly", dayOfWeek: 7, hour: 10, minute: 0 });
});

test("每周五21点半 → weekly 周五 21:30", () => {
  assert.deepEqual(parseSchedule("每周五21点半"), { kind: "weekly", dayOfWeek: 5, hour: 21, minute: 30 });
});

// ---- 新增一次性表达（Python 版没有）----

test("30分钟后 → after 1800", () => {
  assert.deepEqual(parseSchedule("30分钟后"), { kind: "after", seconds: 1800 });
});

test("2小时后 → after 7200", () => {
  assert.deepEqual(parseSchedule("2小时后"), { kind: "after", seconds: 7200 });
});

test("明天8点 → once 8:00", () => {
  assert.deepEqual(parseSchedule("明天8点"), { kind: "once", hour: 8, minute: 0 });
});

test("明天9:30 → once 9:30", () => {
  assert.deepEqual(parseSchedule("明天9:30"), { kind: "once", hour: 9, minute: 30 });
});

// ---- 非法输入（中文报错）----

function throwsChinese(fn, fragment) {
  assert.throws(fn, (err) => {
    assert.ok(err instanceof Error, "应抛 Error");
    assert.ok(/[\u4e00-\u9fa5]/.test(err.message), `报错应为中文，实际: ${err.message}`);
    if (fragment) assert.ok(err.message.includes(fragment), `报错应含「${fragment}」，实际: ${err.message}`);
    return true;
  });
}

test("每天（不带时间）→ 抛中文错", () => {
  throwsChinese(() => parseSchedule("每天"), "每天任务需要指定时间");
});

test("每0小时 → 抛中文错", () => {
  throwsChinese(() => parseSchedule("每0小时"), "1-24");
});

test("每25小时 → 抛中文错", () => {
  throwsChinese(() => parseSchedule("每25小时"), "1-24");
});

test("每0分钟 → 抛中文错", () => {
  throwsChinese(() => parseSchedule("每0分钟"), "1-1440");
});

test("每周一（不带时间）→ 抛中文错", () => {
  throwsChinese(() => parseSchedule("每周一"), "每周任务需要指定时间");
});

test("空字符串 → 抛中文错", () => {
  throwsChinese(() => parseSchedule(""), "不能为空");
});

test("每天25点 → 时间不合法", () => {
  throwsChinese(() => parseSchedule("每天25点"), "0-23");
});

test("完全看不懂 → 抛中文错并列出支持的说法", () => {
  throwsChinese(() => parseSchedule("帮我订个闹钟"), "支持的说法");
});

// ---- describeSchedule ----

test("describeSchedule 中文描述", () => {
  assert.equal(describeSchedule({ kind: "daily", hour: 9, minute: 5 }), "每天 9:05");
  assert.equal(describeSchedule({ kind: "weekly", dayOfWeek: 1, hour: 9, minute: 0 }), "每周一 9:00");
  assert.equal(describeSchedule({ kind: "interval", seconds: 7200 }), "每 2 小时");
  assert.equal(describeSchedule({ kind: "interval", seconds: 1800 }), "每 30 分钟");
  assert.equal(describeSchedule({ kind: "after", seconds: 1800 }), "30 分钟后（一次性）");
  assert.equal(describeSchedule({ kind: "once", hour: 8, minute: 0 }), "明天 8:00（一次性）");
});
