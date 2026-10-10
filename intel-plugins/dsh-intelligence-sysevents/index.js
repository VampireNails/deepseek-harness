// dsh-intelligence-sysevents —— 系统事件总线（发射端）。
//
// 职责：sysevents_emit(type, severity, title, detail) 把系统级事件写进
// ~/.dsh/intel-system-events/events.jsonl。HMC 侧 muse/system-events/list
// 读这个文件推送到手机。本插件只做发射与落盘，不做推送通道。
//
// 模型可通过工具发射业务异常；runner/cron 直接复用 store 发射运行故障。
// severity：high（默认推）/ normal / low（默认不推，由 App 端策略决定）。

import z from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { emitEvent, listEvents, SEVERITIES } from "./src/store.js";
import {toolError} from '../shared/tool-error.js';

export const name = "dsh-intelligence-sysevents";
export const inject = ["agents", "tools"];

export const Config = z.object({}).default({});

const textBlock = (text) => [{ type: "text", text }];

export function apply(ctx, config) {
  // 编程式入口：其他插件可直接 ctx.get("sysevents").emit(...)
  ctx.provide("sysevents", { emit: emitEvent, list: listEvents, severities: SEVERITIES });

  ctx.effect(
    function* () {
      yield ctx.tools.register(
        defineTool({
          name: "sysevents_emit",
          description:
            "发射系统事件（heartbeat 异常、任务失败等），写入系统事件日志，供手机推送。severity: high/normal/low。",
          parameters: {
            type: { type: "string", required: true, description: "事件类型，如 heartbeat.anomaly / task.failed" },
            title: { type: "string", required: true, description: "一句话标题（中文）" },
            detail: { type: "string", description: "详情（可选，最多 2000 字）" },
            severity: { type: "string", description: "high/normal/low，默认 normal" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => {
            try {
              const ev = emitEvent(args);
              return { text: `事件已记录：[${ev.severity}] ${ev.type} ${ev.title}（id ${ev.id.slice(0, 8)}）` };
            } catch (e) {
              throw toolError(e,'SYSEVENTS_WRITE_FAILED');
            }
          },
        })
      );
      yield ctx.tools.register(
        defineTool({
          name: "sysevents_list",
          description: "查最近系统事件（调试用）。",
          parameters: {
            since: { type: "string", description: "ISO 时间戳游标，只返回之后的时间" },
            limit: { type: "number", description: "最多条数，默认 50" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => {
            let items;
            try{items=listEvents({ since: args.since || null, limit: args.limit || 50 });}catch(e){throw toolError(e,'SYSEVENTS_READ_FAILED');}
            return { text: JSON.stringify(items, null, 1) };
          },
        })
      );
    },
    "intelSysEvents.tools"
  );
}
