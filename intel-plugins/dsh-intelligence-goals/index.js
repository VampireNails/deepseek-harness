import z from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { GoalsStore, goalsDir } from "./src/goals.js";

export const name = "dsh-intelligence-goals";
export const inject = ["agents", "tools"];

export const Config = z.object({}).default({});

const textBlock = (text) => [{ type: "text", text }];

function fmtGoal(g) {
  const lines = [`[${g.id}] ${g.title} (${g.status})`, `  创建：${g.created}`];
  if (g.desc) lines.push(`  描述：${g.desc}`);
  const recent = (g.progress || []).slice(-10);
  for (const p of recent) lines.push(`  - ${p.ts}: ${p.text}`);
  if (g.status !== "active" && g.closed) lines.push(`  关闭：${g.closed}`);
  return lines.join("\n");
}

export function apply(ctx, config) {
  const goals = new GoalsStore(goalsDir());
  ctx.provide("goals", goals);

  ctx.effect(
    function* () {
      yield ctx.tools.register(
        defineTool({
          name: "goal_create",
          description: "创建一个长期目标（标题+可选描述），返回目标 id。",
          parameters: {
            title: { type: "string", required: true, description: "目标标题" },
            desc: { type: "string", description: "目标描述（可选）" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => {
            const id = goals.create(args.title, args.desc || "");
            return { text: `已创建目标 ${id}：${args.title.trim()}` };
          },
        })
      );
      yield ctx.tools.register(
        defineTool({
          name: "goal_progress",
          description: "给一个目标记一条带时间戳的进展。ref 可以是目标 id 或标题关键字（模糊匹配，歧义或找不到时报错）。",
          parameters: {
            ref: { type: "string", required: true, description: "目标 id 或标题关键字" },
            text: { type: "string", required: true, description: "进展内容" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => {
            try {
              const g = goals.logProgress(args.ref, args.text);
              return { text: `已记进展到 ${g.title}[${g.id}]` };
            } catch (e) {
              return { text: e.message };
            }
          },
        })
      );
      yield ctx.tools.register(
        defineTool({
          name: "goal_close",
          description: "关闭一个长期目标（移入 Closed 分区）。ref 可以是目标 id 或标题关键字（模糊匹配，歧义或找不到时报错）。",
          parameters: {
            ref: { type: "string", required: true, description: "目标 id 或标题关键字" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => {
            try {
              const g = goals.close(args.ref);
              return { text: `已关闭目标 ${g.title}[${g.id}]` };
            } catch (e) {
              return { text: e.message };
            }
          },
        })
      );
      yield ctx.tools.register(
        defineTool({
          name: "goal_list",
          description: "列出长期目标，支持按状态过滤。",
          parameters: {
            status: {
              type: "string",
              description: "过滤：active（进行中）/ closed（已关闭）/ all（全部），默认 all",
            },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => {
            const status = (args.status || "all").trim().toLowerCase();
            if (!["active", "closed", "all"].includes(status)) {
              return { text: `status 只能是 active/closed/all，收到：${args.status}` };
            }
            const gs = goals.listGoals(status);
            if (!gs.length) return { text: `暂无${status === "all" ? "" : status}目标` };
            return { text: gs.map(fmtGoal).join("\n\n") };
          },
        })
      );
    },
    "intelGoals.tools"
  );
}
