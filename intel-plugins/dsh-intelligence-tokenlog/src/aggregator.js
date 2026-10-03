// dsh-intelligence-tokenlog —— token 用量聚合核心逻辑。
//
// 数据源：~/.dsh/sessions/--root--/*/session.v4.jsonl.zstd 中 assistant/message
// 事件的 data.usage（DeepSeek API 返回的 usage，原样落盘）。
// 只读日志，不调 API，不烧 token。
//
// 设计上把纯逻辑（parse/identify/aggregate/format）与 IO（扫目录、zstd 解压）
// 分开，decompress 可注入，单测用 mock 数据，不碰真实 session。

import { execFileSync } from "node:child_process";
import { readdirSync, existsSync } from "node:fs";
import { join } from "node:path";

export const SESSIONS_ROOT = "/root/.dsh/sessions/--root--";

// 多模型价格表（元/百万 tokens，空闲时段、未命中缓存）
// deepseek-flash：官方价格（2026-10-02）；chatgpt-plus：包月制，边际成本约 0
export const MODEL_PRICES = {
  "deepseek-flash": { input: 1, output: 4, note: "DeepSeek 官方空闲价" },
  "deepseek-chat": { input: 1, output: 4, note: "同 flash（历史数据）" },
  "chatgpt-plus": { input: 0, output: 0, note: "包月制，边际成本约 0" },
};
// 向后兼容：保留旧常量
export const PRICE_INPUT_PER_M = MODEL_PRICES["deepseek-flash"].input;
export const PRICE_OUTPUT_PER_M = MODEL_PRICES["deepseek-flash"].output;

function priceFor(model) {
  return MODEL_PRICES[model] || MODEL_PRICES["deepseek-flash"];
}

// 关键词 → 任务 id。顺序重要：更具体的放前面（"目标自主执行" 先于 "目标" 类）。
const TASK_KEYWORDS = [
  ["目标自主执行", "goal_act"],
  ["目标进展审查", "goal_review"],
  ["记忆整理", "evolve_upkeep"],
  ["Heartbeat", "heartbeat"],
  ["晨间简报", "morning_briefing"],
  ["学习助手", "studying"],
  ["想法整理", "ideas"],
  ["深夜复盘", "dreaming"],
  ["技能审查", "skill_review"],
];

export const TASK_LABELS = {
  evolve_upkeep: "记忆整理",
  heartbeat: "Heartbeat 巡检",
  morning_briefing: "晨间简报",
  studying: "学习",
  ideas: "想法整理",
  dreaming: "深夜复盘",
  skill_review: "技能审查",
  goal_review: "目标进展审查",
  goal_act: "目标自主执行",
  other: "其他",
};

// 从首条 user/message 文本识别任务
export function identifyTask(firstUserText) {
  const text = firstUserText || "";
  for (const [kw, id] of TASK_KEYWORDS) {
    if (text.includes(kw)) return id;
  }
  return "other";
}

// 解析一个 session 的解压后 JSONL 文本。
// 返回 { input, output, task, messages }；messages 为带 usage 的 assistant 消息数。
export function parseSessionText(jsonlText) {
  let input = 0;
  let output = 0;
  let messages = 0;
  let task = "other";
      let sessionModel = "deepseek-flash";
  let taskLocked = false;
  for (const line of jsonlText.split("\n")) {
    const s = line.trim();
    if (!s) continue;
    let ev;
    try {
      ev = JSON.parse(s);
    } catch {
      continue; // 坏行跳过
    }
    if (!taskLocked && ev.type === "user/message") {
      const c = ev.data && ev.data.content;
      const text = Array.isArray(c) && c[0] && typeof c[0].text === "string" ? c[0].text : "";
      task = identifyTask(text);
      taskLocked = true;
    }
    if (ev.type === "assistant/message" && ev.data && ev.data.usage) {
      const u = ev.data.usage;
      if (typeof u.inputTokens === "number") input += u.inputTokens;
      if (typeof u.outputTokens === "number") output += u.outputTokens;
      messages++;
    }
  }
  return { input, output, task, messages };
}

function defaultDecompress(file) {
  return execFileSync("zstd", ["-dc", file], { maxBuffer: 64 * 1024 * 1024 }).toString("utf8");
}

function dayKey(ms) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// 扫描并聚合。decompress 可注入（单测用）。
// 返回 { days, sessions, skipped, byTask: {task: {input, output, sessions}}, byDay: {day: {input, output}} }
export function aggregateSessions(sessionsRoot = SESSIONS_ROOT, days = 7, decompress = defaultDecompress) {
  const result = { days, sessions: 0, skipped: 0, byTask: {}, byDay: {} };
  if (!existsSync(sessionsRoot)) return result;
  const cutoff = Date.now() - days * 24 * 3600 * 1000;

  const bump = (bucket, key, input, output) => {
    if (!bucket[key]) bucket[key] = { input: 0, output: 0, sessions: 0 };
    bucket[key].input += input;
    bucket[key].output += output;
    bucket[key].sessions += 1;
  };

  let dirs;
  try {
    dirs = readdirSync(sessionsRoot, { withFileTypes: true })
      .filter((e) => e.isDirectory())
      .map((e) => e.name);
  } catch {
    return result;
  }

  for (const dir of dirs) {
    const file = join(sessionsRoot, dir, "session.v4.jsonl.zstd");
    if (!existsSync(file)) {
      result.skipped++;
      continue;
    }
    let text;
    try {
      text = decompress(file);
    } catch {
      result.skipped++; // 解压失败：记数，不抛错
      continue;
    }
    // 按天过滤：只统计 cutoff 之后的 assistant/message
    let input = 0;
    let output = 0;
    let task = "other";
      let sessionModel = "deepseek-flash";
    let taskLocked = false;
    let hit = false;
    for (const line of text.split("\n")) {
      const s = line.trim();
      if (!s) continue;
      let ev;
      try {
        ev = JSON.parse(s);
      } catch {
        continue;
      }
      if (ev.type === "request/header" && ev.data && ev.data.header && ev.data.header.config) {
        const m = ev.data.header.config.model;
        if (m) sessionModel = m;
      }
      if (!taskLocked && ev.type === "user/message") {
        const c = ev.data && ev.data.content;
        const t = Array.isArray(c) && c[0] && typeof c[0].text === "string" ? c[0].text : "";
        task = identifyTask(t);
        taskLocked = true;
      }
      if (
        ev.type === "assistant/message" &&
        ev.data &&
        ev.data.usage &&
        typeof ev.time === "number" &&
        ev.time >= cutoff
      ) {
        const u = ev.data.usage;
        if (typeof u.inputTokens === "number") input += u.inputTokens;
        if (typeof u.outputTokens === "number") output += u.outputTokens;
        hit = true;
        bump(result.byDay, dayKey(ev.time), u.inputTokens || 0, u.outputTokens || 0);
      }
    }
    if (!hit) continue; // 该 session 在窗口内无消息：不计入
    result.sessions++;
    bump(result.byTask, task, input, output);
    // 记录该 session 的模型（用于分模型计价）
    if (!result.byModel) result.byModel = {};
    if (!result.byModel[sessionModel]) result.byModel[sessionModel] = { input: 0, output: 0, sessions: 0 };
    result.byModel[sessionModel].input += input;
    result.byModel[sessionModel].output += output;
    result.byModel[sessionModel].sessions += 1;
  }
  return result;
}

function fmt(n) {
  return n.toLocaleString("en-US");
}

function fmtCost(input, output, model) {
  const p = priceFor(model);
  return ((input / 1e6) * p.input + (output / 1e6) * p.output).toFixed(2);
}
function fmtCostTotal(byModel) {
  let total = 0;
  for (const [model, v] of Object.entries(byModel || {})) {
    const p = priceFor(model);
    total += (v.input / 1e6) * p.input + (v.output / 1e6) * p.output;
  }
  return total.toFixed(2);
}

// 中文输出：一句话总结 + 按任务表 + 按天表 + 成本估算
export function formatSummary(agg, days) {
  const tasks = Object.entries(agg.byTask).sort((a, b) => b[1].input + b[1].output - (a[1].input + a[1].output));
  const dayKeys = Object.keys(agg.byDay).sort();
  let totalIn = 0;
  let totalOut = 0;
  for (const [, v] of tasks) {
    totalIn += v.input;
    totalOut += v.output;
  }
  const total = totalIn + totalOut;
  const lines = [];
  lines.push(
    `近 ${agg.days} 天共 ${agg.sessions} 个会话产生 token，累计 ${(total / 1000).toFixed(1)}K tokens（输入 ${(totalIn / 1000).toFixed(1)}K / 输出 ${(totalOut / 1000).toFixed(1)}K）。`
  );
  lines.push("");
  lines.push("按任务：");
  lines.push("| 任务 | 会话数 | 输入 | 输出 |");
  lines.push("|---|---|---|---|");
  for (const [task, v] of tasks) {
    lines.push(`| ${TASK_LABELS[task] || task} | ${v.sessions} | ${fmt(v.input)} | ${fmt(v.output)} |`);
  }
  if (!tasks.length) lines.push("|（无）| | | |");
  lines.push("");
  lines.push("按天：");
  lines.push("| 日期 | 输入 | 输出 |");
  lines.push("|---|---|---|");
  for (const d of dayKeys) {
    const v = agg.byDay[d];
    lines.push(`| ${d} | ${fmt(v.input)} | ${fmt(v.output)} |`);
  }
  if (!dayKeys.length) lines.push("|（无）| | |");
  lines.push("");
  const byModel = agg.byModel || {};
  const modelLines = Object.entries(byModel).map(([m, v]) => {
    const p = priceFor(m);
    const cost = fmtCost(v.input, v.output, m);
    const priceNote = p.note ? `（${p.note}）` : "";
    return `  - ${m}：${(v.input/1000).toFixed(1)}K in / ${(v.output/1000).toFixed(1)}K out，约 ¥${cost}${priceNote}`;
  });
  lines.push(`成本估算：约 ¥${fmtCostTotal(byModel)}（分模型）`);
  for (const ml of modelLines) lines.push(ml);
  lines.push(`（估算值，实际按高峰/空闲时段和缓存命中情况计费）`);
  if (agg.skipped) lines.push(`（另有 ${agg.skipped} 个 session 跳过：损坏或解压失败）`);
  return lines.join("\n");
}
