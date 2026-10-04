// dsh-intelligence-tokenlog —— token 用量可见性。
//
// 职责：token_usage_summary(days) 扫描 ~/.dsh/sessions 下 session 日志，
// 聚合 assistant/message 的 data.usage（DeepSeek API 返回的 usage 原样落盘），
// 按任务、天及 provider/model 汇总；不从 token 日志推断价格或套餐。
// 只读日志，不调 API，不烧 token。

import z from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { formatSummary } from "./src/aggregator.js";
import { aggregateInWorker } from "./src/runner.js";

export const name = "dsh-intelligence-tokenlog";
export const inject = ["agents", "tools"];

export const Config = z.object({
  timeoutMs: z.number().min(1).max(300000).default(60000),
  decompressTimeoutMs: z.number().min(1).max(300000).default(5000),
}).default({});

const textBlock = (text) => [{ type: "text", text }];

export function apply(ctx, config) {
  // 编程式入口：其他插件可直接 ctx.get("tokenlog").aggregate(...)
  ctx.provide("tokenlog", {
    aggregate: (sessionsRoot, days = 7, signal) => aggregateInWorker(days,
      { sessionsRoot, signal, timeoutMs: config.timeoutMs, decompressTimeoutMs: config.decompressTimeoutMs }),
    format: formatSummary,
  });

  ctx.effect(
    function* () {
      yield ctx.tools.register(
        defineTool({
          name: "token_usage_summary",
          description:
            "汇总最近 N 天的 token 用量（按任务、按天、按提供商和模型）。不推断费用或套餐。只读 session 日志，不调 API。",
          parameters: {
            days: { type: "number", description: "统计天数，1-30，默认 7" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args, exec) => {
            const raw = args.days == null ? 7 : Math.floor(args.days);
            const days = Math.min(30, Math.max(1, Number.isFinite(raw) ? raw : 7));
            try {
              const agg = await aggregateInWorker(days,
                { signal: exec.signal, timeoutMs: config.timeoutMs, decompressTimeoutMs: config.decompressTimeoutMs });
              return { text: formatSummary(agg, days) };
            } catch (e) {
              return { text: `汇总失败：${e.message}` };
            }
          },
        })
      );
    },
    "intelTokenLog.tools"
  );
}
