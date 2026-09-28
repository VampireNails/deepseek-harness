// dsh-intelligence-soul —— Muse 行为规则复刻
// 每回合第 1 步注入 Soul 文本；$DSH_HOME/intel-soul/soul.md 可覆盖自带版本；
// soul_read / soul_update 工具支持运行时查看与进化。
import z from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { createUserMessage } from "@deepseek-ai/dsh-llm";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { homedir } from "node:os";
import { fileURLToPath } from "node:url";

export const name = "dsh-intelligence-soul";
export const inject = ["agents", "tools"];

export const Config = z.object({}).default({});

const here = dirname(fileURLToPath(import.meta.url));
const textBlock = (text) => [{ type: "text", text }];

export function soulDir() {
  return join(process.env.DSH_HOME || join(homedir(), ".dsh"), "intel-soul");
}

export function soulFile() {
  return join(soulDir(), "soul.md");
}

export function shippedSoul() {
  return readFileSync(join(here, "soul.md"), "utf8").trim();
}

export function readSoul() {
  const f = soulFile();
  if (existsSync(f)) return readFileSync(f, "utf8").trim();
  return shippedSoul();
}

export function updateSoulSection(text, section, content) {
  const header = `## ${section}`;
  const esc = section.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if (text.includes(header)) {
    const re = new RegExp(`## ${esc}\\n([\\s\\S]*?)(?=\\n## |\\s*$)`);
    return text.replace(re, `${header}\n${content}\n`);
  }
  return text ? `${text}\n\n${header}\n${content}\n` : `# Soul\n\n${header}\n${content}\n`;
}

function ensureSeeded() {
  mkdirSync(soulDir(), { recursive: true });
  const f = soulFile();
  if (!existsSync(f)) writeFileSync(f, shippedSoul() + "\n");
}

export function apply(ctx, config) {
  ensureSeeded();
  ctx.provide("soul", { file: soulFile(), read: readSoul });

  // 每回合第 1 步注入行为规则
  ctx.effect(() =>
    ctx.on("agent/pre-step", async ({ agent, turn, step, signal }, next) => {
      const decision = await next();
      if (decision.kind === "reject" || signal.aborted) return decision;
      if (step !== 1) return decision;
      const text = readSoul();
      if (!text) return decision;
      const injected = `以下是你的行为规则（Soul），定义你的为人处事，每回合都要遵守：\n${text}`;
      return {
        ...decision,
        messages: [
          ...decision.messages,
          createUserMessage({
            content: [{ type: "text", text: injected }],
            source: { kind: name, form: "snapshot", sections: [{ name, text: injected }] },
          }),
        ],
      };
    })
  );

  ctx.effect(
    function* () {
      yield ctx.tools.register(
        defineTool({
          name: "soul_read",
          description: "读取智能体的行为规则（Soul）全文。",
          parameters: {},
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async () => ({ text: readSoul() || "Soul 为空" }),
        })
      );
      yield ctx.tools.register(
        defineTool({
          name: "soul_update",
          description: "更新 Soul 的某个小节。只改指定小节，其他内容不动。",
          parameters: {
            section: { type: "string", required: true, description: "小节标题，如做事方式" },
            content: { type: "string", required: true, description: "小节新内容（markdown 列表）" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => {
            const nextText = updateSoulSection(readSoul(), args.section, args.content);
            mkdirSync(soulDir(), { recursive: true });
            writeFileSync(soulFile(), nextText + "\n");
            return { text: `已更新「${args.section}」` };
          },
        })
      );
    },
    "intelSoul.tools"
  );
}
