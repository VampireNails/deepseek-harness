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

function defaultDecompress(file, { deadline, decompressTimeoutMs = 5000 } = {}) {
  const remaining = deadline === undefined ? decompressTimeoutMs : deadline - Date.now();
  if (remaining <= 0) throw new Error("TOKENLOG_TIMEOUT");
  return execFileSync("zstd", ["-dc", file], {
    maxBuffer: 64 * 1024 * 1024,
    timeout: Math.max(1, Math.floor(Math.min(remaining, decompressTimeoutMs))),
    killSignal: "SIGKILL",
  }).toString("utf8");
}

function dayKey(ms) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// 扫描并聚合。decompress 可注入（单测用）。
// 返回 { days, sessions, skipped, byTask: {task: {input, output, sessions}}, byDay: {day: {input, output}} }
export function aggregateSessions(sessionsRoot = SESSIONS_ROOT, days = 7, decompress = defaultDecompress, options = {}) {
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
    if (options.deadline !== undefined && Date.now() >= options.deadline) throw new Error("TOKENLOG_TIMEOUT");
    const file = join(sessionsRoot, dir, "session.v4.jsonl.zstd");
    if (!existsSync(file)) {
      result.skipped++;
      continue;
    }
    let text;
    try {
      text = decompress(file, options);
    } catch {
      if (options.deadline !== undefined && Date.now() >= options.deadline) throw new Error("TOKENLOG_TIMEOUT");
      result.skipped++; // 解压失败：记数，不抛错
      continue;
    }
    // 按天过滤：只统计 cutoff 之后的 assistant/message
    let input = 0;
    let output = 0;
    let task = "other";
    let sessionModel = "unknown";
    const sessionModels = {};
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
        const { provider, model } = ev.data.header.config;
        sessionModel = typeof provider === "string" && typeof model === "string"
          ? `${provider}/${model}` : "unknown";
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
        bump(sessionModels, sessionModel, u.inputTokens || 0, u.outputTokens || 0);
      }
    }
    if (!hit) continue; // 该 session 在窗口内无消息：不计入
    result.sessions++;
    bump(result.byTask, task, input, output);
    // 每个模型只计一个会话；用量按消息归属，保留会话内的切换。
    if (!result.byModel) result.byModel = {};
    for (const [model, usage] of Object.entries(sessionModels)) {
      bump(result.byModel, model, usage.input, usage.output);
    }
  }
  if (options.deadline !== undefined && Date.now() >= options.deadline) throw new Error("TOKENLOG_TIMEOUT");
  return result;
}

function fmt(n) {
  return n.toLocaleString("en-US");
}

// 中文输出：按任务、按天、按 provider/model 的用量；日志不能推断费用。
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
    return `  - ${m}：${(v.input/1000).toFixed(1)}K in / ${(v.output/1000).toFixed(1)}K out`;
  });
  lines.push("分模型用量：");
  for (const ml of modelLines) lines.push(ml);
  lines.push("成本估算：未提供计费数据，不输出估算值；以供应商账单为准。");
  if (agg.skipped) lines.push(`（另有 ${agg.skipped} 个 session 跳过：损坏或解压失败）`);
  return lines.join("\n");
}
