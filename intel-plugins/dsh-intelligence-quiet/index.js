import z from "@deepseek-ai/schemastery";
import { createHash } from "node:crypto";
import { homedir } from "node:os";
import { join } from "node:path";
import { QuietState } from "./src/state.js";
import { buildTranscript } from "./src/reflect.js";

export const name = "dsh-intelligence-quiet";
export const inject = ["agents", "sessionQuery", "subagents"];

export const Config = z
  .object({
    cooldownSec: z.number().default(300),
    minUserChars: z.number().default(8),
    provider: z.string().default("spawn"),
  })
  .default({});

// Marker: reflection children carry this in their prompt. If it ever shows up
// in a transcript, that turn belongs to a reflection child -> skip (no recursion).
export const REFLECT_MARKER = "[quiet-moment-reflection]";

function dataDir() {
  return join(process.env.DSH_HOME || join(homedir(), ".dsh"), "intel-quiet");
}

const REFLECT_PROMPT = (transcript) => `${REFLECT_MARKER}
你是"静默复盘"助手。主智能体刚刚结束了一轮与用户的对话，对话内容如下：

---
${transcript}
---

请判断其中是否有值得**长期记住**的内容：用户的偏好、个人事实、习惯、重要决定、待办事项。
- 如果有：对每条调用 memory_write 工具保存。text 用简明中文一句话，kind 从 preference/fact/habit/decision/todo 里选最贴切的一个。最多调用 2 次。
- 如果没有值得记住的内容：直接回复"无"并结束，不要调用任何工具。

只做复盘和记忆写入，不要做其他事。`;

export function apply(ctx, config) {
  const state = new QuietState(dataDir());

  ctx.on("agent/turn-stopping", async ({ agent, turn }) => {
    try {
      await maybeReflect(ctx, config, state, agent, turn);
    } catch (e) {
      console.error("[dsh-intelligence-quiet]", e?.message || e);
    }
  });
}

async function maybeReflect(ctx, config, state, agent, turn) {
  const sessionId = agent?.session?.id;
  if (!sessionId) return;

  // 1. 冷却：5 分钟内已复盘则跳过
  const now = Date.now();
  if (now - state.get("lastAt", 0) < config.cooldownSec * 1000) return;

  // 2. 取本 turn 的真实用户消息 + 助手回复，拼 transcript
  const transcript = await buildTranscript(ctx, sessionId, turn, config.minUserChars);
  if (!transcript) return;
  if (transcript.includes(REFLECT_MARKER)) return; // 反射子智能体自己的 turn

  // 3. 去重：内容与上次复盘相同则跳过
  const hash = createHash("sha256").update(transcript).digest("hex");
  if (state.get("lastHash") === hash) return;

  // 4. 起复盘子智能体：只允许 memory_write，不继承父上下文，不再派生
  const provider =
    ctx.subagents.getProvider(config.provider) || ctx.subagents.getProvider("spawn");
  if (!provider) {
    console.error("[dsh-intelligence-quiet] no subagent provider available");
    return;
  }
  const controller = new AbortController();
  // 复盘限时 90 秒，超时放弃（不阻塞主流程收尾）
  const timer = setTimeout(() => controller.abort("quiet reflection timeout"), 90000);
  let run;
  try {
    run = await ctx.subagents.start(provider.name, {
      label: "quiet-moment reflection",
      prompt: [{ type: "text", text: REFLECT_PROMPT(transcript) }],
      parent: agent,
      toolFilter: { allow: ["memory_write"] },
      maxDepth: 1,
      signal: controller.signal,
    });
    // 注意：start() 只表示发布成功；run.result 才是子智能体真正跑完
    await run.result;
  } finally {
    clearTimeout(timer);
    if (run) await run.dispose().catch(() => {});
  }

  state.set("lastAt", now);
  state.set("lastHash", hash);
}
