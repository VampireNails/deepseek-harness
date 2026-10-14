// dsh-intelligence-tokenlog —— token 用量聚合核心逻辑。
//
// 数据源：~/.dsh/sessions/--root--/*/session.v4.jsonl.zstd 中 assistant/message
// 事件的 data.usage（DeepSeek API 返回的 usage，原样落盘）。
// 只读日志，不调 API，不烧 token。
//
// 设计上把纯逻辑（parse/identify/aggregate/format）与 IO（扫目录、zstd 解压）
// 分开，decompress 可注入，单测用 mock 数据，不碰真实 session。

import { execFileSync } from "node:child_process";
import { readdirSync, lstatSync, openSync, closeSync } from "node:fs";
import { join, resolve } from "node:path";
import { homedir } from "node:os";
import { readAttribution, nativeIdentity, scopeIdentity } from "./attribution.js";
import { createHash } from "node:crypto";

/** @returns Retained root-session log directory for the configured DSH home. */
export function defaultSessionsRoot() {
  return resolve(process.env.DSH_HOME || join(homedir(), '.dsh'), 'sessions', '--root--');
}
export const SESSIONS_ROOT = defaultSessionsRoot();

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
  evolve_dreaming: "深夜复盘",
  evolve_skill_review: "技能审查",
  evolve_studying: "学习",
  evolve_ideas: "想法整理",
  evolve_goal_review: "目标进展审查",
  evolve_goal_act: "目标自主执行",
  memory_upkeep: "记忆整理",
  upkeep: "记忆整理",
  morning: "晨间简报",
};

// 从首条 user/message 文本识别任务
export function identifyTask(firstUserText) {
  const text = typeof firstUserText === "string" ? firstUserText : "";
  for (const [kw, id] of TASK_KEYWORDS) {
    if (text.includes(kw)) return id;
  }
  return "other";
}

// Parse retained durable JSON strictly; sequence duplicates are idempotent only when identical.
function events(text) {
  const seen = new Map(), rows = [];
  for (const line of text.split('\n')) {
    if (!line.trim()) continue;
    let ev;
    try { ev = JSON.parse(line); } catch { throw Error('TOKENLOG_LOG_INVALID'); }
    if (!ev || typeof ev !== 'object' || Array.isArray(ev)) throw Error('TOKENLOG_LOG_INVALID');
    if (ev.seq !== undefined) {
      if (!Number.isSafeInteger(ev.seq) || ev.seq < 0) throw Error('TOKENLOG_LOG_INVALID');
      const previous = seen.get(ev.seq);
      if (previous !== undefined) {
        if (previous !== line) throw Error('TOKENLOG_DUPLICATE_CONFLICT');
        continue;
      }
      seen.set(ev.seq, line);
    }
    rows.push(ev);
  }
  return rows;
}
const numericFields = ['input', 'output', 'messages', 'cacheRead', 'cacheWrite', 'cacheReadRecords', 'cacheWriteRecords'];
const zero = () => Object.fromEntries(numericFields.map(key => [key, 0]));
function add(target, value) {
  for (const key of numericFields) {
    target[key] += value[key];
    if (!Number.isSafeInteger(target[key]) || target[key] < 0) throw Error('TOKENLOG_LOG_INVALID');
  }
}
function usage(value) {
  const result = zero();
  for (const [from, to] of [['inputTokens','input'], ['outputTokens','output'], ['cacheReadTokens','cacheRead'], ['cacheWriteTokens','cacheWrite']]) {
    if (value[from] === undefined && from.startsWith('cache')) continue;
    if (!Number.isSafeInteger(value[from]) || value[from] < 0) throw Error('TOKENLOG_LOG_INVALID');
    result[to] = value[from];
    if (from.startsWith('cache')) result[to + 'Records'] = 1;
  }
  result.messages = 1;
  return result;
}
// This reader keeps the existing uncached input/output meaning; cache counts remain separate.
export function parseSessionText(text) {
  const rows = events(text), counts = zero();
  const first = rows.find(ev => ev.type === 'user/message');
  const task = identifyTask(first?.data?.content?.[0]?.text);
  for (const ev of rows) if (ev.type === 'assistant/message' && ev.data?.usage) add(counts, usage(ev.data.usage));
  return {input: counts.input, output: counts.output, task, messages: counts.messages};
}

function defaultDecompress(file, { deadline, decompressTimeoutMs = 5000 } = {}) {
  const remaining = deadline === undefined ? decompressTimeoutMs : deadline - Date.now();
  if (remaining <= 0) throw Error('TOKENLOG_TIMEOUT');
  // Check the reader's access before zstd collapses filesystem and format errors into an exit status.
  let fd;
  try { fd = openSync(file, 'r'); }
  catch { throw Error('TOKENLOG_LOG_UNREADABLE'); }
  closeSync(fd);
  let bytes;
  try {
    bytes = execFileSync('zstd', ['-dc', file], {maxBuffer: 64 * 1024 * 1024,
      timeout: Math.max(1, Math.floor(Math.min(remaining, decompressTimeoutMs))), killSignal: 'SIGKILL', stdio:['ignore','pipe','pipe']});
  } catch (error) {
    if (error.code === 'ENOENT') throw Error('TOKENLOG_ZSTD_UNAVAILABLE');
    if (error.code === 'EACCES' || error.code === 'EPERM') throw Error('TOKENLOG_ZSTD_NOT_EXECUTABLE');
    if (error.code === 'ETIMEDOUT' || error.signal === 'SIGKILL') throw Error('TOKENLOG_TIMEOUT');
    throw Error('TOKENLOG_DECOMPRESS_FAILED');
  }
  try { return new TextDecoder('utf-8', {fatal: true}).decode(bytes); }
  catch { throw Error('TOKENLOG_LOG_INVALID_UTF8'); }
}
function dayKey(ms) {
  const d = new Date(ms), p = n => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth()+1)}-${p(d.getUTCDate())}`;
}
function taskIdentity(rows, header, headers, attribution) {
  const native = nativeIdentity(header, headers, attribution), scope = scopeIdentity(native, attribution);
  const {taskConflict, ...scopeFields} = scope;
  if (native?.conflict || taskConflict) return {id:'unknown', classification:'conflict', source:'automation-index-v1', inferred:false, ...scopeFields};
  if (native) return {id:native.task_id, classification:/^legacy-evidence:[a-f0-9]{64}$/.test(native.run_id)?'legacy-evidence':'native',
    source:'automation-index-v1', inferred:false, ...scopeFields};
  const first = rows.find(ev => ev.type === 'user/message');
  const guess = header?.origin === 'subagent' ? 'other' : identifyTask(first?.data?.content?.[0]?.text);
  return {id:guess === 'other' ? 'unknown' : guess, classification:guess === 'other' ? 'unknown' : 'heuristic',
    source:guess === 'other' ? 'none' : 'first-user-keywords', inferred:guess !== 'other', ...scopeFields};
}
function inheritedCut(rows, header) {
  const markers = rows.filter(ev => ev.type === 'session/end-seed' && ev.data?.inherited === true);
  if (!header?.isSeeded && markers.length || header?.isSeeded && !markers.length) throw Error('TOKENLOG_LOG_INVALID');
  if (!markers.length) return -1;
  const cut = markers.at(-1).seq;
  if (!Number.isSafeInteger(cut) || cut < 0 || rows.some(ev => ev.type !== 'session' && (!Number.isSafeInteger(ev.seq) || ev.seq < 0))) throw Error('TOKENLOG_LOG_INVALID');
  return cut;
}
/** Read retained v4 root logs atomically; any unreadable log fails the entire scan. */
export function aggregateSessions(sessionsRoot = defaultSessionsRoot(), days = 7, decompress = defaultDecompress, options = {}) {
  const result = {days, sessions:0, skipped:0, byTask:{}, byScope:{}, byDay:{}, byModel:{},
    taskUsage:{available:true,classificationAvailable:true,scopeAvailable:false,attributionVersion:2}, availability:{status:'complete'}, details:zero()};
  const scanTime = Date.now(), cutoff = options.window?.startMs ?? scanTime - days * 24 * 3600 * 1000, endMs = options.window?.endMs ?? scanTime + 1;
  if (!Number.isSafeInteger(cutoff) || !Number.isSafeInteger(endMs) || cutoff < 0 || endMs <= cutoff || endMs > scanTime + 1) throw Error('TOKENLOG_LOG_INVALID');
  result.window = {startMs:cutoff,endMs,timeZone:'UTC',basis:'assistant-message-time',endExclusive:true};
  let dirs;
  try { dirs = readdirSync(sessionsRoot, {withFileTypes:true}).filter(e => e.isDirectory()).map(e => e.name).sort(); }
  catch { throw Error('TOKENLOG_SOURCE_UNAVAILABLE'); }
  const attribution = readAttribution(sessionsRoot, options), roots = new Map(), logs = [], headers = new Map();
  result.taskUsage.scopeAvailable = attribution.scopeAvailable;
  result.taskUsage.indexAvailable = attribution.indexAvailable;
  function bump(bucket, key, value, sessions, identity) {
    if (!Object.hasOwn(bucket,key)) Object.defineProperty(bucket,key,{value:{...zero(), sessions:0, ...identity}, enumerable:true});
    add(bucket[key], value); bucket[key].sessions += sessions;
  }
  {
    for (const dir of dirs) {
      if (options.deadline !== undefined && Date.now() >= options.deadline) throw Error('TOKENLOG_TIMEOUT');
      const file = join(sessionsRoot, dir, 'session.v4.jsonl.zstd');
      try { const stat=lstatSync(file); if (!stat.isFile()) throw Error('TOKENLOG_LOG_UNREADABLE'); }
      catch(error) { throw Error(error.code === 'ENOENT' ? 'TOKENLOG_LOG_MISSING' : 'TOKENLOG_LOG_UNREADABLE'); }
      let text;
      try { text = decompress(file, options); }
      catch(error) {
        if (error.code === 'EACCES' || error.code === 'EPERM') throw Error('TOKENLOG_LOG_UNREADABLE');
        if (error.message?.startsWith('TOKENLOG_')) throw error;
        throw Error('TOKENLOG_DECOMPRESS_FAILED');
      }
      const rows = events(text); if (!rows.length) throw Error('TOKENLOG_LOG_INVALID');
      const header = rows[0];
      if(header.type!=='session'||header.version!==4||typeof header.id!=='string'||!header.id||header.id.length>1024||
        rows.slice(1).some(ev=>ev.type==='session')||header.origin!==undefined&&header.origin!=='subagent'||
        header.parentSession!==undefined&&(typeof header.parentSession!=='string'||!header.parentSession||header.parentSession.length>1024)||
        header.origin==='subagent'&&header.parentSession===undefined)throw Error('TOKENLOG_LOG_INVALID');
      if (header) {
        const fingerprint = createHash('sha256').update(JSON.stringify(rows)).digest('hex'), previous = roots.get(header.id);
        if (previous !== undefined) { if (previous !== fingerprint) throw Error('TOKENLOG_DUPLICATE_CONFLICT'); continue; }
        roots.set(header.id, fingerprint);
      }
      const first = rows.find(ev => ev.type === 'user/message');
      // Keep only aggregation events after hashing; do not retain complete transcripts across files.
      const retained = rows.filter(ev => ['session/end-seed','assistant/message','request/header'].includes(ev.type)).map(ev=>({
        type:ev.type,seq:ev.seq,time:ev.time,data:ev.type==='assistant/message'?{usage:ev.data?.usage}:
          ev.type==='request/header'?{header:{config:ev.data?.header?.config}}:{inherited:ev.data?.inherited}}));
      if (first) retained.unshift({type:'user/message',seq:first.seq,data:{content:[{text:first.data?.content?.[0]?.text}]}});
      logs.push({rows:retained, header}); if (header) headers.set(header.id, header);
    }
    for (const {rows, header} of logs) {
      const identity = taskIdentity(rows, header, headers, attribution), sessionCounts = zero(), models = new Map(), cut = inheritedCut(rows, header);
      let model = 'unknown';
      for (const ev of rows) {
        if (ev.type === 'request/header') {
          const config = ev.data?.header?.config;
          model = publicModelId(config?.provider) && publicModelId(config?.model) ? config.provider+'/'+config.model : 'unknown';
        }
        if (ev.type !== 'assistant/message' || !ev.data?.usage || ev.seq <= cut) continue;
        const counts = usage(ev.data.usage);
        if (!Number.isSafeInteger(ev.time) || !Number.isFinite(new Date(ev.time).getTime())) throw Error('TOKENLOG_LOG_INVALID');
        if (ev.time < cutoff || ev.time >= endMs) continue;
        add(sessionCounts,counts); bump(result.byDay,dayKey(ev.time),counts,1);
        if (!models.has(model)) models.set(model,zero()); add(models.get(model),counts);
      }
      if (!sessionCounts.messages) continue;
      result.sessions++; add(result.details, sessionCounts);
      // Keep legacy keyword keys while native/audited identities cannot collide with guesses.
      const baseKey=identity.classification === 'heuristic' ? identity.id : identity.classification+':'+identity.id;
      const key=identity.scopeStatus === 'unknown' ? baseKey : baseKey+':'+(identity.scopeStatus === 'conflict'?'conflict':identity.scope);
      bump(result.byTask,key,sessionCounts,1,identity);
      bump(result.byScope,identity.scopeStatus === 'known' ? identity.scope : identity.scopeStatus,sessionCounts,1);
      for (const [id,counts] of models) bump(result.byModel,id,counts,1);
    }
  }
  if (options.deadline !== undefined && Date.now() >= options.deadline) throw Error('TOKENLOG_TIMEOUT');
  result.taskUsage.scopeCoverage = !attribution.scopeAvailable ? 'unavailable' : result.byScope.unknown || result.byScope.conflict ? 'partial' : 'complete';
  return result;
}
function publicModelId(id) { return typeof id === 'string' && id.length <= 128 && /^[A-Za-z0-9][A-Za-z0-9._:@+-]*(?:\/[A-Za-z0-9][A-Za-z0-9._:@+-]*)*$/.test(id) && !/(?:^|\/)[A-Za-z]:/.test(id); }
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
    lines.push(`| ${TASK_LABELS[v.id ?? task] || v.id || '未知任务'}（${({native:'原生身份','legacy-evidence':'历史证据',heuristic:'关键词推断',unknown:'未知',conflict:'身份冲突'})[v.classification] || '旧版推断'}；${({production:'生产',test:'测试'})[v.scope] || (v.scopeStatus === 'conflict' ? '范围冲突' : v.scopeStatus === 'unknown' ? '范围未知' : '范围未记录')}） | ${v.sessions} | ${fmt(v.input)} | ${fmt(v.output)} |`);
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
