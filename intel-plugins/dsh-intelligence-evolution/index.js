import z from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import { TASKS, chunk } from "./src/tasks.js";
import { AutomationSessionStore, runnerIdentity } from "./src/session-origin.js";

export const name = "dsh-intelligence-evolution";
export const inject = ["agents", "tools"];

export const Config = z.object({
  sessionIndexBusyTimeoutMs: z.number().min(1).max(30000).default(3000),
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

export function apply(ctx, config) {
  const identity = runnerIdentity();
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
