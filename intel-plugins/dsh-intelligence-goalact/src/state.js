// state.js —— 自主执行会话状态与熔断器。纯文件状态，无外部依赖，可单测。
import { mkdirSync, readFileSync, writeFileSync, appendFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

export function goalActDir() {
  const d = join(homedir(), ".dsh", "intel-goalact");
  mkdirSync(d, { recursive: true });
  return d;
}

const STATE_FILE = "state.json";
const PROPOSALS_FILE = "proposals.jsonl";

/** 连续失败熔断阈值：达到后 begin() 拒绝启动，防无意义烧 token。 */
export const CIRCUIT_BREAKER_THRESHOLD = 3;

export function loadState(dir = goalActDir()) {
  const f = join(dir, STATE_FILE);
  try {
    const data = JSON.parse(readFileSync(f, "utf8"));
    if (data && typeof data === "object" && data.goals && typeof data.goals === "object") return data;
  } catch {}
  return { goals: {} };
}

export function saveState(state, dir = goalActDir()) {
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, STATE_FILE), JSON.stringify(state, null, 1));
}

function goalState(state, goalId) {
  if (!state.goals[goalId]) state.goals[goalId] = { consecutiveFailures: 0, lastRun: null, totalRuns: 0 };
  return state.goals[goalId];
}

/** 熔断检查：返回 { allowed, reason }。 */
export function checkCircuit(state, goalId) {
  const g = goalState(state, goalId);
  if (g.consecutiveFailures >= CIRCUIT_BREAKER_THRESHOLD) {
    return {
      allowed: false,
      reason: `目标 ${goalId} 已连续 ${g.consecutiveFailures} 次无实质进展，熔断器跳闸。停止自主执行，去查 job_alerts.md / 记忆找原因；人工确认后再手动清零（删 state.json 对应项）。`,
    };
  }
  return { allowed: true, reason: "" };
}

/** 会话结束记账：progressMade=true 清零失败计数，否则 +1。 */
export function recordFinish(state, goalId, progressMade, ts = new Date().toISOString()) {
  const g = goalState(state, goalId);
  g.totalRuns += 1;
  g.lastRun = ts;
  g.consecutiveFailures = progressMade ? 0 : g.consecutiveFailures + 1;
  return g;
}

/** 提议升级通道：只追加记录，不执行。 */
export function recordProposal(entry, dir = goalActDir()) {
  mkdirSync(dir, { recursive: true });
  const line = JSON.stringify({
    ts: new Date().toISOString(),
    status: "pending",
    goalId: entry.goalId || "",
    action: entry.action || "",
    reason: entry.reason || "",
  });
  appendFileSync(join(dir, PROPOSALS_FILE), line + "\n");
  return line;
}

export function proposalsFile(dir = goalActDir()) {
  return join(dir, PROPOSALS_FILE);
}
