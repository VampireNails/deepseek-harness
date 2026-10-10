import z from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { JobLog, joblogDir } from "./src/joblog.js";

export const name = "dsh-intelligence-joblog";
export const inject = ["agents", "tools"];

export const Config = z.object({ testRecords: z.boolean().default(false),historyLimit:z.number().min(1).max(1000).step(1).default(200) }).default({});

const textBlock = (text) => [{ type: "text", text }];

export function apply(ctx, config) {
  const joblog = new JobLog(joblogDir(), { scope: config.testRecords ? 'test' : 'production',historyLimit:config.historyLimit??200 });
  ctx.provide("joblog", joblog);

  ctx.effect(function* () {
    yield ctx.tools.register(
      defineTool({
        name: "joblog_status",
        description: "查看后台任务的有限运行历史和来源；默认只显示生产，可选查看测试与未标注旧记录。",
        parameters: { includeTests: { type: 'boolean', description: '查看全部来源，包括测试与未标注历史，默认 false' } },
        output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
        execute: async (args) => {
          const runs = joblog.readHistory({ includeTests: args.includeTests === true });
          const lines = runs.map(
            r => `- ${r.id}: ${r.status}（${r.last}）来源：${r.scope === 'production' ? '生产' : r.scope === 'test' ? '测试' : '未标注'}${r.runId ? ' runId='+r.runId : ' 旧快照'}${r.summary||r.detail ? " " + (r.summary??r.detail) : ""}`
          );
          return { text: lines.length ? lines.join("\n") : "暂无任务运行记录" };
        },
      })
    );
    yield ctx.tools.register(
      defineTool({
        name: "joblog_alerts",
        description: "查看后台任务失败告警及原始日期；默认只显示生产，可选查看测试与未标注旧告警。",
        parameters: { includeTests: { type: 'boolean', description: '同时查看明确标记的测试告警，默认 false' } },
        output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
        execute: async (args) => ({ text: joblog.readAlerts({ includeTests: args.includeTests === true,productionOnly:true }) }),
      })
    );
  },
  "intelJoblog.tools"
  );
}
