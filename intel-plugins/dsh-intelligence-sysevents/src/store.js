// store.js —— 系统事件落盘。纯函数+文件 IO，可单测。
import { mkdirSync, readFileSync, writeFileSync, appendFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import { randomUUID } from "node:crypto";

export function sysEventsDir() {
  const d = join(homedir(), ".dsh", "intel-system-events");
  mkdirSync(d, { recursive: true });
  return d;
}

export const SEVERITIES = Object.freeze({ low: "low", normal: "normal", high: "high" });

/** 事件类型（供给 HMC 端分类）：heartbeat.anomaly / task.failed / goalact.proposal / ... */
export function emitEvent(entry, dir = sysEventsDir()) {
  const { type, severity, title, detail } = entry || {};
  if (typeof type !== "string" || !type.trim()) throw new Error("type 不能为空");
  if (typeof title !== "string" || !title.trim()) throw new Error("title 不能为空");
  const sev = SEVERITIES[severity] || SEVERITIES.normal;
  const ev = {
    id: randomUUID(),
    ts: new Date().toISOString(),
    type: type.trim(),
    severity: sev,
    title: title.trim(),
    detail: typeof detail === "string" ? detail.trim().slice(0, 2000) : "",
  };
  mkdirSync(dir, { recursive: true });
  appendFileSync(join(dir, "events.jsonl"), JSON.stringify(ev) + "\n");
  return ev;
}

/** 读最近事件（供 HMC muse/system-events/list）：since 为 ISO 时间戳游标，limit 上限。 */
export function listEvents({ since = null, limit = 50 } = {}, dir = sysEventsDir()) {
  let lines = [];
  try {
    lines = readFileSync(join(dir, "events.jsonl"), "utf8").split("\n").filter(Boolean);
  } catch { return []; }
  // 只保留尾部 500 行，防文件无限增长拖慢读取
  if (lines.length > 500) lines = lines.slice(-500);
  const out = [];
  for (const ln of lines) {
    try {
      const ev = JSON.parse(ln);
      if (since && !(ev.ts >= since)) continue; // >=：同毫秒事件不丢，App 按 id 去重
      out.push(ev);
    } catch {}
  }
  return out.slice(-Math.max(1, Math.min(limit, 200)));
}
