import z from "@deepseek-ai/schemastery";
import { createHash } from "node:crypto";
import { homedir } from "node:os";
import { join } from "node:path";
import { QuietState } from "./src/state.js";
import { buildTranscript } from "./src/reflect.js";
import { QuietMemoryAuthority } from "./src/memory-authority.js";
import { ReflectionLifetime } from "./src/reflection-lifetime.js";

export const name = "dsh-intelligence-quiet";
export const inject = ["agents", "sessionQuery", "subagents"];

export const Config = z
  .object({
    cooldownSec: z.number().default(300),
    minUserChars: z.number().default(8),
    provider: z.string().default("spawn"),
    reflectTimeoutMs: z.number().default(90000),
    autoApproveMemoryWrites: z.boolean().default(false),
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
  const authority = new QuietMemoryAuthority();
  const lifetime = new ReflectionLifetime();
  ctx.provide("quietMemoryAuthority", authority);
  ctx.effect(() => async () => { authority.close(); await lifetime.close(); }, "intelQuiet.memoryAuthority");

  // 复刻 Python 版 daemon 线程语义：handler 不 await，复盘在后台跑，
  // 回合结束不被阻塞（评估问题 3 的修复）。
  // 包 ctx.effect：插件卸载时 listener 自动回收。
  ctx.effect(
    () =>
      ctx.on("agent/turn-stopping", ({ agent, turn }) => {
        maybeReflect(ctx, config, state, authority, lifetime, agent, turn).catch((e) => {
          console.error("[dsh-intelligence-quiet]", e?.message || e);
        });
      }),
    "intelQuiet.turnStopping"
  );
}

async function maybeReflect(ctx, config, state, authority, lifetime, agent, turn) {
  const sessionId = agent?.session?.id;
  if (!sessionId || lifetime.closed) return;

  // 1. 冷却：5 分钟内已复盘则跳过
  const now = Date.now();
  if (now - state.get("lastAt", 0) < config.cooldownSec * 1000) return;

  // 2. 取本 turn 的真实用户消息 + 助手回复，拼 transcript
  const transcript = await buildTranscript(ctx, sessionId, turn, config.minUserChars);
  if (!transcript || lifetime.closed) return;
  if (transcript.includes(REFLECT_MARKER)) return; // 反射子智能体自己的 turn

  // 3. 去重：内容与上次复盘相同则跳过；已有 pending 的也不重复触发
  const hash = createHash("sha256").update(transcript).digest("hex");
  if (state.get("lastHash") === hash) return;
  if (state.get("pendingHash") === hash) return;

  // 4. 起复盘子智能体：只允许 memory_write，不继承父上下文，不再派生
  const provider =
    ctx.subagents.getProvider(config.provider) || ctx.subagents.getProvider("spawn");
  if (!provider) {
    console.error("[dsh-intelligence-quiet] no subagent provider available");
    return;
  }

  // 先记 pending（防并发重复触发），start 成功后才落 lastAt/lastHash；
  // start 抛错时清 pending，冷却期过后可重试，不静默丢数据（评估 2.7）
  state.set("pendingHash", hash);

  const owned = lifetime.begin(config.reflectTimeoutMs);
  if (!owned) { state.delete("pendingHash"); return; }
  const {controller} = owned;
  let run;
  const publishGrant = config.autoApproveMemoryWrites
    ? authority.begin(controller.signal, sessionId) : () => {};
  try {
    run = await ctx.subagents.start(provider.name, {
      label: "quiet-moment reflection",
      prompt: [{ type: "text", text: REFLECT_PROMPT(transcript) }],
      parent: agent,
      toolFilter: { allow: ["memory_write"] },
      maxDepth: 1,
      signal: controller.signal,
    });
    if (!await lifetime.publish(owned, run) || lifetime.closed) {
      publishGrant();lifetime.release(owned);state.delete("pendingHash");return;
    }
    publishGrant(run);
  } catch (e) {
    publishGrant();
    lifetime.release(owned);
    state.delete("pendingHash");
    console.error("[dsh-intelligence-quiet] start failed:", e?.message || e);
    return;
  }
  // start 成功：落冷却/去重状态
  state.set("lastAt", now);
  state.set("lastHash", hash);
  state.delete("pendingHash");
  // The turn listener does not await this function, so the parent stays free.
  // Keep ownership until BOTH the child result and native disposal settle.
  try {
    await run.result;
  } catch (e) {
    if (e?.name !== "AbortError")
      console.error("[dsh-intelligence-quiet] reflection failed:", e?.message || e);
  } finally {
    authority.release(run.localAgent);
    try { await lifetime.dispose(owned); }
    finally { lifetime.release(owned); }
  }
}
