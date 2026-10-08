// dsh-intelligence-memory —— 长期记忆插件（阶段 1）
//
// 照 @deepseek-ai/dsh-time-context 的结构：
//   - agent/pre-step 做每步召回注入（仅 step 1，sessionProjections 节流）
//   - memory_write / memory_read / memory_search 三个工具
//   - 所有注册走 ctx.effect，可卸载回卷
// 存储：DSH_HOME/intel-memory/memory.db（SQLite + FTS5），不进 workspace。
import z from "@deepseek-ai/schemastery";
import { z as zod } from "zod";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { createUserMessage } from "@deepseek-ai/dsh-llm";
import { openStore } from "./src/store.js";
import { FtsRetriever, DEFAULT_QUERY_LIMITS } from "./src/retriever.js";
import { DEFAULT_SYNONYM_GROUPS, DEFAULT_RANKING_LIMITS, DEFAULT_RANKING_RATIOS, DEFAULT_PREFERENCE_SUBJECTS } from './src/ranking.js';
import { memoryWriteTool, memoryReadTool, memorySearchTool } from "./src/tools.js";
import { registerMemoryCorrection, memoryManagement } from "./src/correction.js";

export const name = "dsh-intelligence-memory";

export const inject = ["agents", "sessionProjections", "tools"];

export const Config = z.object({
  dataDir: z.string().default(""),
  recallLimit: z.number().default(3),
  minQueryChars: z.number().default(2),
  maxQueryChars: z.number().min(1).max(Number.MAX_SAFE_INTEGER).step(1).default(DEFAULT_QUERY_LIMITS.maxQueryChars),
  maxQueryTerms: z.number().min(1).max(Number.MAX_SAFE_INTEGER).step(1).default(DEFAULT_QUERY_LIMITS.maxQueryTerms),
  maxCandidates: z.number().min(1).max(Number.MAX_SAFE_INTEGER).step(1).default(DEFAULT_RANKING_LIMITS.maxCandidates),
  maxCandidateBytes: z.number().min(1).max(Number.MAX_SAFE_INTEGER).step(1).default(DEFAULT_RANKING_LIMITS.maxCandidateBytes),
  synonymGroups: z.array(z.array(z.string())).default(DEFAULT_SYNONYM_GROUPS),
  preferenceSubjects: z.array(z.string()).default(DEFAULT_PREFERENCE_SUBJECTS),
  minimumScoreRatio: z.number().min(0).max(1).default(DEFAULT_RANKING_RATIOS.minimumScoreRatio),
  duplicatePatternPenalty: z.number().min(0).max(1).default(DEFAULT_RANKING_RATIOS.duplicatePatternPenalty),
});

const stateSchema = zod.object({
  currentTurn: zod.number().nullable(),
  lastInjectTurn: zod.number().nullable(),
  lastInjectAt: zod.number().nullable(),
});

function defaultDataDir() {
  const home = process.env.DSH_HOME || join(homedir(), ".dsh");
  return join(home, "intel-memory");
}

function extractQuery(messages) {
  if (!Array.isArray(messages)) return "";
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (!m || m.role !== "user") continue;
    // 只取真人用户的消息：跳过 system-reminder / 其他插件注入的 user 消息
    if (m.source && m.source.kind !== "user") continue;
    const c = m.content;
    if (typeof c === "string") return c;
    if (Array.isArray(c)) {
      return c
        .filter((b) => b && b.type === "text" && typeof b.text === "string")
        .map((b) => b.text)
        .join("\n");
    }
  }
  return "";
}

export function apply(ctx, config) {
  const cfg = config ?? {};
  const dataDir = cfg.dataDir || defaultDataDir();
  const recallLimit = cfg.recallLimit ?? 3;
  const minQueryChars = cfg.minQueryChars ?? 2;

  const db = openStore(dataDir);
  ctx.effect(() => () => db.close());
  const retriever = new FtsRetriever(db, {
    maxQueryChars: cfg.maxQueryChars ?? DEFAULT_QUERY_LIMITS.maxQueryChars,
    maxQueryTerms: cfg.maxQueryTerms ?? DEFAULT_QUERY_LIMITS.maxQueryTerms,
    maxCandidates: cfg.maxCandidates ?? DEFAULT_RANKING_LIMITS.maxCandidates,
    maxCandidateBytes: cfg.maxCandidateBytes ?? DEFAULT_RANKING_LIMITS.maxCandidateBytes,
    synonymGroups: cfg.synonymGroups ?? DEFAULT_SYNONYM_GROUPS,
    preferenceSubjects: cfg.preferenceSubjects ?? DEFAULT_PREFERENCE_SUBJECTS,
    minimumScoreRatio: cfg.minimumScoreRatio ?? DEFAULT_RANKING_RATIOS.minimumScoreRatio,
    duplicatePatternPenalty: cfg.duplicatePatternPenalty ?? DEFAULT_RANKING_RATIOS.duplicatePatternPenalty,
  });
  ctx.provide('memoryReferences', {
    directory: resolve(dataDir),
    validate(ids) { return ids.every(id => retriever.get(id)); },
  });
  ctx.provide('memoryManagement', memoryManagement(retriever));
  registerMemoryCorrection(ctx,retriever);

  ctx.effect(() =>
    ctx.sessionProjections.register({
      key: "intelMemory",
      stateVersion: 1,
      stateSchema,
      init: () => ({ currentTurn: null, lastInjectTurn: null, lastInjectAt: null }),
      apply: (state, event) => {
        if (event.type === "turn/start") {
          const turn = event.data?.turn;
          return typeof turn === "number" && state.currentTurn !== turn
            ? { ...state, currentTurn: turn }
            : state;
        }
        if (event.type === "user/message" && event.data?.source?.kind === name) {
          return { ...state, lastInjectTurn: state.currentTurn, lastInjectAt: event.time };
        }
        return state;
      },
    })
  );

  ctx.effect(() =>
    ctx.on(
      "agent/pre-step",
      async ({ agent, turn, step, signal }, next) => {
        const decision = await next();
        if (decision.kind === "reject" || signal.aborted) return decision;
        if (step !== 1) return decision;
        let state;
        try {
          state = ctx.sessionProjections.stateOf(agent.session, "intelMemory");
        } catch {
          return decision;
        }
        if (state.lastInjectTurn === turn) return decision;
        const query = extractQuery(decision.messages);
        if (query.replace(/\s/g, "").length < minQueryChars) return decision;
        let hits;
        try {
          hits = retriever.search(query, recallLimit);
        } catch (error) {
          if (['MEMORY_QUERY_TOO_LARGE', 'MEMORY_RECALL_TOO_LARGE'].includes(error?.code)) ctx.logger(name).warn(error.message);
          return decision;
        }
        if (hits.length === 0) return decision;
        const text = [
          "以下是与当前问题相关的长期记忆（系统自动召回，仅供参考）：",
          ...hits.map((h, i) => (i + 1) + ". " + h.text),
        ].join("\n");
        return {
          ...decision,
          messages: [
            ...decision.messages,
            createUserMessage({
              content: [{ type: "text", text }],
              source: { kind: name, form: "snapshot", sections: [{ name, text }] },
            }),
          ],
        };
      },
      { prepend: true }
    )
  );

  ctx.effect(
    function* () {
      yield ctx.tools.register(memoryWriteTool(retriever));
      yield ctx.tools.register(memoryReadTool(retriever));
      yield ctx.tools.register(memorySearchTool(retriever));
    },
    "intelMemory.tools"
  );
}
