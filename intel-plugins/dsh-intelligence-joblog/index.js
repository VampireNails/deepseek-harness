import z from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { JobLog, joblogDir } from "./src/joblog.js";

export const name = "dsh-intelligence-joblog";
export const inject = ["agents", "tools"];

export const Config = z.object({}).default({});

const textBlock = (text) => [{ type: "text", text }];

export function apply(ctx, config) {
  const joblog = new JobLog(joblogDir());
  ctx.provide("joblog", joblog);

  ctx.effect(function* () {
    yield ctx.tools.register(
      defineTool({
        name: "joblog_status",
        description: "查看后台任务的最后运行状态（job_runs.json）",
        parameters: {},
        output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
        execute: async () => {
          const runs = joblog.readRuns();
          const lines = Object.entries(runs).map(
            ([id, r]) => `- ${id}: ${r.status}（${r.last}）${r.detail ? " " + r.detail : ""}`
          );
          return { text: lines.length ? lines.join("\n") : "暂无任务运行记录" };
        },
      })
    );
    yield ctx.tools.register(
      defineTool({
        name: "joblog_alerts",
        description: "查看后台任务的失败告警（job_alerts.md）",
        parameters: {},
        output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
        execute: async () => ({ text: joblog.readAlerts() }),
      })
    );
  },
  "intelJoblog.tools"
  );
}
