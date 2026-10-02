// dsh-intelligence-goalact —— 自主目标执行引擎的护栏插件（evolve profile）。
//
// 定位：只做护栏，不做智能。规划（goal → 步骤）与执行由 LLM 按 cron 任务
// prompt 完成；本插件提供三件套：
//   goal_act_begin(goalId)   开执行会话：查熔断器，返回预算与行为红线。
//   goal_act_propose(...)    升级通道：敏感/不可逆/需人拍板的事项只记录为
//                            提议（proposals.jsonl），不执行、不代批。
//   goal_act_finish(...)     关会话：更新熔断计数；progressMade=true 时
//                            evidence 不能为空（防编造进展）；经 intelGoals
//                            把 summary 写成 goal_progress（原子落盘）。
//
// 安全线（与 Soul / Tomas 拍板一致）：
//   - 高危命令由 dsh-intelligence-execguard 硬拦截（BLOCKED）；
//   - 无头下任何 ask 转拒绝，不存在"等审批"状态；
//   - 不可逆操作（删文件、改服务/cron 配置、git push、对外发送）只提议不执行；
//   - 连续 3 次无实质进展熔断停跑，防无意义烧 token。

import z from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import {
  goalActDir,
  loadState,
  saveState,
  checkCircuit,
  recordFinish,
  recordProposal,
  CIRCUIT_BREAKER_THRESHOLD,
} from "./src/state.js";

export const name = "dsh-intelligence-goalact";
export const inject = ["agents", "tools"];

export const Config = z.object({}).default({});

const textBlock = (text) => [{ type: "text", text }];

const RULES = `自主执行行为红线（违反会被硬拦截或记为违规）：
[允许] 读代码/文档/记忆；跑只读检查与测试；workspace 内写草稿与文档；
        memory_write / feed_post / todo_* / goal_progress 记录类操作。
[提议] 删文件、改服务或 cron 配置、git push、对外发送、装包、改权限——
       一律不执行，用 goal_act_propose 记录为提议等人批。
[禁止] 高危命令（删根、格盘、写裸设备、fork 炸弹、关机）会被 execguard
       直接 deny，不要试；不清 HMC 数据、不删真实用户数据。`;

const BUDGET = `预算：单次会话建议 ≤20 个工具调用；整任务 10 分钟硬上限
（intel-task.sh timeout 600），超时即停，不要赶工。`;

async function getGoals(ctx) {
  try {
    const g = await ctx.get?.("intelGoals");
    if (g && typeof g.logProgress === "function") return g;
  } catch {}
  return null;
}

export function apply(ctx, config) {
  ctx.effect(
    function* () {
      yield ctx.tools.register(
        defineTool({
          name: "goal_act_begin",
          description:
            "为一个目标开启自主执行会话。查熔断器；通过则返回预算与行为红线。",
          parameters: {
            goalId: { type: "string", required: true, description: "目标 id（如 g1）" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => {
            const goalId = String(args.goalId || "").trim();
            if (!goalId) return { text: "goalId 不能为空" };
            const dir = goalActDir();
            const state = loadState(dir);
            const c = checkCircuit(state, goalId);
            if (!c.allowed) return { text: `熔断：${c.reason}` };
            const g = state.goals[goalId] || { consecutiveFailures: 0, totalRuns: 0 };
            return {
              text: [
                `会话已开：目标 ${goalId}（历史运行 ${g.totalRuns} 次，连续无进展 ${g.consecutiveFailures} 次，熔断阈值 ${CIRCUIT_BREAKER_THRESHOLD}）。`,
                BUDGET,
                RULES,
                "流程：读目标 progress 历史 + memory_search 定 1-3 个具体步骤 → todo_add 建计划 → 执行 → 敏感事项 goal_act_propose → goal_act_finish 收尾（必须调，否则本次不记账）。",
              ].join("\n"),
            };
          },
        })
      );
      yield ctx.tools.register(
        defineTool({
          name: "goal_act_propose",
          description:
            "升级通道：把敏感/不可逆/需人拍板的事项记录为提议（proposals.jsonl），不执行。返回确认。",
          parameters: {
            goalId: { type: "string", required: true, description: "目标 id" },
            action: { type: "string", required: true, description: "提议的具体动作" },
            reason: { type: "string", required: true, description: "为什么需要人拍板" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => {
            const action = String(args.action || "").trim();
            if (!action) return { text: "action 不能为空" };
            recordProposal({ goalId: args.goalId, action, reason: args.reason });
            return { text: `已记录为提议（未执行）：${action}。等人拍板，不要自行执行。` };
          },
        })
      );
      yield ctx.tools.register(
        defineTool({
          name: "goal_act_finish",
          description:
            "关闭自主执行会话：更新熔断计数，经 goals 写进展。progressMade=true 时 evidence 不能为空（防编造）。",
          parameters: {
            goalId: { type: "string", required: true, description: "目标 id" },
            summary: { type: "string", required: true, description: "本轮真实进展（一句话中文，无进展就写无实质推进）" },
            evidence: { type: "array", items: { type: "string" }, description: "进展证据（如文件路径、测试结果、commit）。progressMade=true 时必填。" },
            progressMade: { type: "boolean", required: true, description: "本轮是否有实质进展" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => {
            const goalId = String(args.goalId || "").trim();
            const summary = String(args.summary || "").trim();
            const evidence = Array.isArray(args.evidence) ? args.evidence.filter((x) => typeof x === "string" && x.trim()) : [];
            if (!goalId || !summary) return { text: "goalId 与 summary 不能为空" };
            if (args.progressMade && evidence.length === 0) {
              return { text: "拒绝：声称有进展但 evidence 为空。请给出证据，或把 progressMade 设为 false 如实记录。" };
            }
            const dir = goalActDir();
            const state = loadState(dir);
            const g = recordFinish(state, goalId, !!args.progressMade);
            saveState(state, dir);
            const progressText = args.progressMade
              ? `自主执行：${summary}（证据：${evidence.join("；")}）`
              : `自主执行：${summary}`;
            const goals = await getGoals(ctx);
            if (goals) {
              try {
                goals.logProgress(goalId, progressText);
              } catch (e) {
                return { text: `状态已记账（连续无进展 ${g.consecutiveFailures} 次），但写 goal_progress 失败：${e.message}。请手动补记。` };
              }
            }
            return {
              text: `会话关闭：${progressText}（连续无进展 ${g.consecutiveFailures} 次 / 阈值 ${CIRCUIT_BREAKER_THRESHOLD}）。` +
                (goals ? "" : "注意：goals 插件不可用，进展未自动写入，请手动调 goal_progress 补记。"),
            };
          },
        })
      );
    },
    "intelGoalAct.tools"
  );
}
