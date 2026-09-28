import z from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { createUserMessage } from "@deepseek-ai/dsh-llm";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

export const name = "dsh-intelligence-profile";
export const inject = ["agents", "tools"];

export const Config = z.object({}).default({});

export function profileDir() {
  return join(process.env.DSH_HOME || join(homedir(), ".dsh"), "intel-profile");
}

export function profileFile() {
  return join(profileDir(), "user_profile.md");
}

const textBlock = (text) => [{ type: "text", text }];

function readProfile() {
  const f = profileFile();
  if (!existsSync(f)) return "";
  return readFileSync(f, "utf8").trim();
}

export function apply(ctx, config) {
  mkdirSync(profileDir(), { recursive: true });
  ctx.provide("userProfile", { file: profileFile(), read: readProfile });

  // 每回合第 1 步注入用户画像（仅 Tomas 本人，不建人物/群组档案）
  ctx.effect(() =>
    ctx.on(
      "agent/pre-step",
      async ({ agent, turn, step, signal }, next) => {
        const decision = await next();
        if (decision.kind === "reject" || signal.aborted) return decision;
        if (step !== 1) return decision;
        const text = readProfile();
        if (!text) return decision;
        const injected = `以下是用户（Tomas）的结构化画像，供你参考他的背景、风格和在乎的事：\n${text}`;
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
      }
    )
  );

  ctx.effect(
    function* () {
      yield ctx.tools.register(
        defineTool({
          name: "user_profile_read",
          description: "读取用户（Tomas）的结构化画像。",
          parameters: {},
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async () => {
            const t = readProfile();
            return { text: t || "画像为空" };
          },
        })
      );
      yield ctx.tools.register(
        defineTool({
          name: "user_profile_update",
          description:
            "更新用户画像的某个小节。只改指定小节，其他内容不动。画像仅记录 Tomas 本人，不建他人档案。",
          parameters: {
            section: { type: "string", required: true, description: "小节标题，如'偏好与禁忌'" },
            content: { type: "string", required: true, description: "小节新内容（markdown 列表）" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => {
            let text = readProfile();
            const header = `## ${args.section}`;
            if (text.includes(header)) {
              // 替换该小节到下一小节前的内容
              const re = new RegExp(`## ${args.section.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\n([\s\S]*?)(?=\n## |\s*$)`);
              text = text.replace(re, `${header}\n${args.content}\n`);
            } else {
              text = text ? `${text}\n\n${header}\n${args.content}\n` : `# 用户画像\n\n${header}\n${args.content}\n`;
            }
            mkdirSync(profileDir(), { recursive: true });
            writeFileSync(profileFile(), text);
            return { text: `已更新「${args.section}」` };
          },
        })
      );
    },
    "intelProfile.tools"
  );
}
