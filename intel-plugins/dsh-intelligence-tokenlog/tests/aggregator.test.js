import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

import {
  identifyTask,
  parseSessionText,
  aggregateSessions,
  formatSummary,
} from "../src/aggregator.js";

const NOW = Date.now();
let sequence = 0;
const ev = (type, time, data) => JSON.stringify({ type, seq: ++sequence, time, data });
const userEv = (text, time) =>
  ev("user/message", time, { content: [{ type: "text", text }] });
const asstEv = (input, output, time) =>
  ev("assistant/message", time, { usage: { inputTokens: input, outputTokens: output } });

test("identifyTask：关键词识别晨间简报", () => {
  assert.equal(identifyTask("生成今日晨间简报：查新闻"), "morning_briefing");
});

test("identifyTask：更具体的关键词优先（目标自主执行 vs 目标进展审查）", () => {
  assert.equal(identifyTask("目标自主执行助手（goal act）"), "goal_act");
  assert.equal(identifyTask("目标进展审查助手（goal review）"), "goal_review");
});

test("identifyTask：匹配不上返回 other", () => {
  assert.equal(identifyTask("随便聊聊"), "other");
  assert.equal(identifyTask(""), "other");
  assert.equal(identifyTask(null), "other");
});

test("parseSessionText：累加 usage 并识别任务", () => {
  const text = [
    userEv("记忆整理助手：检查今天的对话", NOW),
    asstEv(1000, 200, NOW + 1),
    asstEv(500, 100, NOW + 2),
    ev("assistant/message", NOW + 3, { message: "无 usage 的事件" }),
  ].join("\n");
  const r = parseSessionText(text);
  assert.equal(r.input, 1500);
  assert.equal(r.output, 300);
  assert.equal(r.task, "evolve_upkeep");
  assert.equal(r.messages, 2);
});

test("parseSessionText：无 user/message 时任务为 other", () => {
  const r = parseSessionText(asstEv(10, 5, NOW));
  assert.equal(r.task, "other");
  assert.equal(r.input, 10);
});

// 用注入的 decompress 做聚合测试，不碰真实 session、不依赖 zstd
function mockDecompress(map) {
  return (file) => {
    if (map[file] === null) throw new Error("decompress boom");
    return map[file];
  };
}

function makeSessionDir(root, name) {
  const d = join(root, name);
  mkdirSync(d, { recursive: true });
  const f = join(d, "session.v4.jsonl.zstd");
  writeFileSync(f, ""); // 占位：decompress 是 mock 的，但 existsSync 需要文件存在
  return f;
}

test("aggregateSessions：按任务+按天汇总", () => {
  const root = mkdtempSync(join(tmpdir(), "toklog-"));
  const f1 = makeSessionDir(root, "s1");
  const f2 = makeSessionDir(root, "s2");
  const t1 = NOW - 1000;
  const t2 = NOW - 25 * 3600 * 1000; // 25 小时前：超出 days=1 窗口
  const de = mockDecompress({
    [f1]: [userEv("你是 Heartbeat 巡检助手", t1), asstEv(2000, 400, t1 + 1)].join("\n"),
    [f2]: [userEv("记忆整理助手", t2), asstEv(9999, 9999, t2 + 1)].join("\n"),
  });
  const agg = aggregateSessions(root, 1, de);
  assert.equal(agg.sessions, 1);
  assert.equal(agg.skipped, 0);
  assert.ok(agg.byTask.heartbeat, "heartbeat 应被统计");
  assert.equal(agg.byTask.heartbeat.input, 2000);
  assert.ok(!agg.byTask.evolve_upkeep, "窗口外的 session 不应计入");
  assert.equal(Object.keys(agg.byDay).length, 1);
});

test("aggregateSessions：损坏 session 不返回假成功", () => {
  const root = mkdtempSync(join(tmpdir(), "toklog-"));
  const bad = makeSessionDir(root, "bad");
  const missing = join(root, "nodir"); // 不存在的目录名不会出现在 readdir，造一个无文件的 dir
  mkdirSync(join(root, "empty"), { recursive: true });
  const de = mockDecompress({ [bad]: null });
  assert.throws(() => aggregateSessions(root, 7, de), /TOKENLOG_DECOMPRESS_FAILED/);
});

test("formatSummary：中文输出含三段式+成本估算", () => {
  const agg = {
    days: 7,
    sessions: 2,
    skipped: 1,
    byTask: { heartbeat: { input: 2000, output: 400, sessions: 1 } },
    byDay: { "2026-10-02": { input: 2000, output: 400, sessions: 1 } },
  };
  const out = formatSummary(agg, 7);
  assert.ok(out.includes("近 7 天共 2 个会话"), "一句话总结");
  assert.ok(out.includes("Heartbeat 巡检"), "按任务表");
  assert.ok(out.includes("2026-10-02"), "按天表");
  assert.ok(out.includes("成本估算"), "成本估算");
  assert.ok(out.includes("估算值"), "标注估算值");
  assert.ok(out.includes("1 个 session 跳过"), "跳过计数");
});

test("多模型用量：未提供计费依据时不推断套餐或价格", () => {
  const agg = {
    days: 1,
    sessions: 3,
    skipped: 0,
    byTask: { heartbeat: { input: 3000, output: 600, sessions: 3 } },
    byDay: { "2026-10-03": { input: 3000, output: 600, sessions: 3 } },
    byModel: {
      "deepseek-flash": { input: 2000, output: 400, sessions: 2 },
      "chatgpt-plus": { input: 1000, output: 200, sessions: 1 },
    },
  };
  const out = formatSummary(agg, 1);
  assert.ok(out.includes("deepseek-flash"), "显示 flash 分组");
  assert.ok(out.includes("chatgpt-plus"), "显示 chatgpt 分组");
  assert.ok(out.includes("分模型"));
  assert.ok(!out.includes("¥"));
  assert.ok(!out.includes("包月制"));
});

test("同一会话切换模型时，每条 usage 归属当时的 provider/model", t => {
  const root = mkdtempSync(join(tmpdir(), "toklog-switch-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const file = makeSessionDir(root, "switch");
  const header = (provider, model) => ev("request/header", NOW, { header: { config: { provider, model } } });
  const text = [header("deepseek-official", "deepseek-flash"), asstEv(100, 10, NOW),
    header("openai-codex", "gpt-5.6-luna"), asstEv(200, 20, NOW),
    header("deepseek-official", "deepseek-flash"), asstEv(300, 30, NOW)].join("\n");
  const agg = aggregateSessions(root, 1, mockDecompress({ [file]: text }));
  assert.deepEqual(Object.fromEntries(Object.entries(agg.byModel).map(([id,v])=>[id,{input:v.input,output:v.output,sessions:v.sessions}])), {
    "deepseek-official/deepseek-flash": { input: 400, output: 40, sessions: 1 },
    "openai-codex/gpt-5.6-luna": { input: 200, output: 20, sessions: 1 },
  });
});

test("缺少 request/header 的用量不假设为 DeepSeek", t => {
  const root = mkdtempSync(join(tmpdir(), "toklog-unknown-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const file = makeSessionDir(root, "unknown");
  const agg = aggregateSessions(root, 1, mockDecompress({ [file]: asstEv(100, 10, NOW) }));
  assert.deepEqual(Object.fromEntries(Object.entries(agg.byModel).map(([id,v])=>[id,{input:v.input,output:v.output,sessions:v.sessions}])), { unknown: { input: 100, output: 10, sessions: 1 } });
});
