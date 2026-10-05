import z from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import { TASKS, chunk } from "./src/tasks.js";
import { AutomationSessionStore, runnerIdentity, readAutomationSessions } from "./src/session-origin.js";
import { createUpkeepQuery } from './src/upkeep-host-source.js';
import { captureUpkeepSource, inspectUpkeepRun } from './src/upkeep-source.js';
import { startUpkeepServer } from './src/upkeep-socket.js';

export const name = "dsh-intelligence-evolution";
export const inject = ["agents", "tools", "sessions"];

export const Config = z.object({
  sessionIndexBusyTimeoutMs: z.number().min(1).max(30000).default(3000),
  upkeepToolBudget: z.number().min(1).max(20).default(6),
  upkeepSourceSocket: z.string().default(''),
  upkeepSourceTimeoutMs: z.number().min(1).max(300000).default(30000),
  upkeepMaxSessionBytes: z.number().min(1).max(536870912).default(67108864),
}).default({});

export function dataDir() {
  return join(process.env.DSH_HOME || join(homedir(), ".dsh"), "intel-evolution");
}

export function heartbeatFile() {
  return join(dataDir(), "HEARTBEAT.md");
}

const textBlock = (text) => [{ type: "text", text }];

function readChecklist() {
  const file = heartbeatFile();
  if (!existsSync(file)) return [];
  return readFileSync(file, "utf8")
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith("#") && !l.startsWith("_"))
    .map((l) => l.replace(/^[-*]\s+/, ""));
}

/**
 * Mount evolution tools and await the optional Host source before reporting startup success.
 * @param ctx - Cordis context owning all registrations and asynchronous cleanup.
 * @param config - Resolved evolution configuration.
 * @returns Startup completion; unavailable or invalid source configuration rejects.
 */
export async function apply(ctx, config) {
  const identity = runnerIdentity();
  if (config?.upkeepSourceSocket) {
    if (identity) throw Error('upkeep source socket belongs to the existing Host profile');
    const directory = join(process.env.DSH_HOME || join(homedir(), '.dsh'), 'intel-joblog');
    const maxSessionBytes = config.upkeepMaxSessionBytes ?? 67108864;
    await ctx.effect(async () => {
      // Resolve providers before accepting requests; an absent reader cannot become an empty cut.
      createUpkeepQuery(ctx, { maxSessionBytes });
      const server = await startUpkeepServer({ socketPath: config.upkeepSourceSocket,
        timeoutMs: config.upkeepSourceTimeoutMs ?? 30000,
        source: { async capture(request, { signal }) {
          const query = createUpkeepQuery(ctx, { signal, maxSessionBytes });
          const captured = await captureUpkeepSource(query, readAutomationSessions(directory), request);
          const automatic = new Set(readAutomationSessions(directory).map(row => row.sessionId));
          // A task root can be created after the initial index read and before the corpus read.
          const retained = captured.messages.filter(message => !automatic.has(message.sessionId));
          captured.counts.filteredEvents += captured.messages.length - retained.length;
          captured.counts.newMessages = retained.length;
          return { ...captured, messages: retained };
        } },
        inspect: (run, { signal }) => inspectUpkeepRun(createUpkeepQuery(ctx, { signal, maxSessionBytes }),
          readAutomationSessions(directory), run),
      });
      return () => server.close();
    }, 'intelEvolution.upkeepSource');
  }
  if (identity && ['evolve_upkeep', 'memory_upkeep'].includes(identity.taskId)) {
    const budget = config?.upkeepToolBudget ?? 6;
    if (!Number.isSafeInteger(budget) || budget < 1 || budget > 20) throw Error('invalid upkeep tool budget');
    const calls = new WeakMap();
    ctx.effect(() => ctx.on('tools/pre-execute', async (exec, next) => {
      if (!exec.agent || exec.parent !== undefined || !['memory_search', 'memory_write'].includes(exec.name))
        return { kind: 'deny', reason: '记忆维护只允许原生记忆搜索和写入。', info: { name: 'UpkeepToolError', code: 'UPKEEP_TOOL_NOT_ALLOWED' } };
      const count = calls.get(exec.agent) ?? 0;
      if (count >= budget)
        return { kind: 'deny', reason: '本轮记忆维护已达到工具调用上限。', info: { name: 'UpkeepToolError', code: 'UPKEEP_TOOL_BUDGET' } };
      calls.set(exec.agent, count + 1);
      return await next();
    }), 'intelEvolution.upkeepTools');
  }
  if (identity) {
    const directory = join(process.env.DSH_HOME || join(homedir(), ".dsh"), "intel-joblog");
    ctx.effect(() => {
      const store = new AutomationSessionStore(directory, { busyTimeoutMs: config?.sessionIndexBusyTimeoutMs ?? 3000 });
      let dispose;
      try {
        dispose = ctx.on("session/created", session => {
          if (session.header.origin === "subagent") return;
          store.record(session.id, identity);
        }, { global: true });
      } catch (error) { store.close(); throw error; }
      return () => { dispose(); store.close(); };
    }, "intelEvolution.sessionOrigin");
  }
  ctx.provide("evolution", { tasks: TASKS, heartbeatFile: heartbeatFile() });

  ctx.effect(
    function* () {
      yield ctx.tools.register(
        defineTool({
          name: "heartbeat_check",
          description:
            "读取 HEARTBEAT.md 检查清单。清单为空返回'清单为空，跳过'；否则返回分块（每块≤3项）的清单供逐项执行。",
          parameters: {},
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async () => {
            const items = readChecklist();
            if (!items.length) return { text: "清单为空，跳过" };
            const chunks = chunk(items, 3);
            const text = chunks
              .map((c, i) => "第 " + (i + 1) + " 批\n" + c.map((t, j) => (j + 1) + ". " + t).join("\n"))
              .join("\n");
            return { text: text };
          },
        })
      );
    },
    "intelEvolution.tools"
  );
}
