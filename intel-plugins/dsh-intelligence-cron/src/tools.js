// tools.js —— cron_create / cron_list / cron_delete（工具名沿用 Python 版 custom_cron）
import { defineTool } from "@deepseek-ai/dsh-tools";
import { intelError, toolError } from '../../shared/tool-error.js';

const textBlock = (text) => [{ type: "text", text }];
const jsonOutput = (render) => ({ schema: { type: "json" }, render });

function agentOf(exec) {
  const agent = exec?.agent;
  if (!agent || !agent.session) throw intelError('CRON_SESSION_REQUIRED');
  return agent;
}

export function cronCreateTool(scheduler) {
  return defineTool({
    name: "cron_create",
    description:
      "创建用户的定时任务。schedule 用中文时间描述：" +
      "每天9点 / 每天9:30 / 每天9点半 / 每2小时 / 每30分钟 / 每小时 / 每半小时 / " +
      "每周一9点 / 每周三18:30 / 30分钟后 / 2小时后 / 明天8点。" +
      "注意：提醒是 session-local 的——只有创建它的会话保持活跃时才会到点跟进；" +
      "每N分钟/每N小时类换算后不得小于 5 分钟。",
    parameters: {
      name: { type: "string", required: true, description: "任务名称，如\"喝水提醒\"" },
      prompt: { type: "string", required: true, description: "到点要做的事，如\"提醒我喝水\"" },
      schedule: { type: "string", required: true, description: "中文时间描述，如\"每天9点\"" },
    },
    output: jsonOutput((args, value) => textBlock(value.text)),
    execute: async (args, exec) => {
      try {
        const text = await scheduler.create(agentOf(exec), args.name, args.prompt, args.schedule);
        return { ok: true, text };
      } catch (err) {
        throw toolError(err,'CRON_OPERATION_FAILED');
      }
    },
  });
}

export function cronListTool(scheduler) {
  return defineTool({
    name: "cron_list",
    description: "列出当前 DSH_HOME 下的自定义定时任务（id、名称、重复规则、下次触发、状态）。",
    parameters: {},
    output: jsonOutput((args, value) => textBlock(value.text)),
    execute: async (args, exec) => {
      try {
        const text = await scheduler.list(agentOf(exec));
        return { ok: true, text };
      } catch (err) {
        throw toolError(err,'CRON_OPERATION_FAILED');
      }
    },
  });
}

export function cronDeleteTool(scheduler) {
  return defineTool({
    name: "cron_delete",
    description:
      "删除一个自定义定时任务。ref 可以是任务 id（如 c1）或名称关键词；" +
      "名称命中多个时必须用 id。会同时删除底层提醒并停止重武装。",
    parameters: {
      ref: { type: "string", required: true, description: "任务 id（如 c1）或名称关键词" },
    },
    output: jsonOutput((args, value) => textBlock(value.text)),
    execute: async (args, exec) => {
      try {
        const text = await scheduler.remove(agentOf(exec), args.ref);
        return { ok: true, text };
      } catch (err) {
        throw toolError(err,'CRON_OPERATION_FAILED');
      }
    },
  });
}
