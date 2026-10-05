import z from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { JobLog, joblogDir } from "./src/joblog.js";

export const name = "dsh-intelligence-joblog";
export const inject = ["agents", "tools"];

export const Config = z.object({ testRecords: z.boolean().default(false) }).default({});

const textBlock = (text) => [{ type: "text", text }];

export function apply(ctx, config) {
  const joblog = new JobLog(joblogDir(), { scope: config.testRecords ? 'test' : 'production' });
  ctx.provide("joblog", joblog);

  ctx.effect(function* () {
    yield ctx.tools.register(
      defineTool({
        name: "joblog_status",
        description: "查看后台任务的最后运行状态及来源；默认保留生产与未标注旧记录，可选查看测试记录。",
        parameters: { includeTests: { type: 'boolean', description: '同时查看明确标记的测试记录，默认 false' } },
        output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
        execute: async (args) => {
          const runs = joblog.readRuns({ includeTests: args.includeTests === true });
          const lines = Object.entries(runs).map(
            ([id, r]) => `- ${args.includeTests === true ? r.id : id}: ${r.status}（${r.last}）来源：${r.scope === 'production' ? '生产' : r.scope === 'test' ? '测试' : '未标注'}${r.detail ? " " + r.detail : ""}`
          );
          return { text: lines.length ? lines.join("\n") : "暂无任务运行记录" };
        },
      })
    );
    yield ctx.tools.register(
      defineTool({
        name: "joblog_alerts",
        description: "查看后台任务失败告警及原始日期；默认保留生产与未标注旧告警，可选查看测试告警。",
        parameters: { includeTests: { type: 'boolean', description: '同时查看明确标记的测试告警，默认 false' } },
        output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
        execute: async (args) => ({ text: joblog.readAlerts({ includeTests: args.includeTests === true }) }),
      })
    );
  },
  "intelJoblog.tools"
  );
}
