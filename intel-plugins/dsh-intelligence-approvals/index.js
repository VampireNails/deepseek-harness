// dsh-intelligence-approvals —— 审批四级分级（dsh 服务端分级优化）。
//
// 背景：HMC 侧新会话默认 workspace-write + ask 已能弹审批卡；本插件是 dsh
// 服务端的分级优化，不是替代品——把"一刀切弹卡"细化为四级：
//   READ    只读工具 → pre-execute 直接 next() 放行，免审批
//   WRITE   写工具   → pre-execute 返回 { kind: "ask" }，走 HMC/Web 审批卡
//   EXEC    执行命令 → 同上先扫高危模式，再 ask
//   BLOCKED 高危命令 → pre-execute 返回 { kind: "deny" }，硬拦截，不弹卡
//
// 挂法：dsh 0.1.7-rc.1 的 tools/pre-execute waterfall（见
// packages/core/tools/src/index.ts 的 Events 声明与 interception.spec.ts 的
// "native-plugin permission pattern"）。所有注册走 ctx.effect，卸载自动回卷；
// 绝不修改 dsh 核心 packages/。

import z from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { LEVELS, classify } from "./src/classify.js";
import { buildPolicy } from "./src/policy.js";

export const name = "dsh-intelligence-approvals";

export const inject = ["tools"];

const LevelSchema = () => z.union([
  z.const(LEVELS.READ), z.const(LEVELS.WRITE),
  z.const(LEVELS.EXEC), z.const(LEVELS.BLOCKED),
]);

export const Config = z.object({
  levels: z.dict(LevelSchema(), z.string()).default({}),
  blocked: z.array(z.string()).default([]),
  defaultLevel: LevelSchema().default(LEVELS.WRITE),
  monitor: z.boolean().default(false),
}).default({});

const textBlock = (text) => [{ type: "text", text }];

function warn(ctx, msg) {
  try { ctx.logger?.warn?.(`dsh-intelligence-approvals: ${msg}`); } catch { /* ignore */ }
}

export function apply(ctx, config) {
  const policy = buildPolicy(config ?? {});

  // 给其他插件/智能体用的编程式入口
  ctx.provide("approvals", {
    policy,
    levels: LEVELS,
    classify: (toolName, args) => classify(toolName, args, policy),
  });

  // 分级查询工具：供智能体或上层在调用前预判处置
  ctx.effect(
    function* () {
      yield ctx.tools.register(
        defineTool({
          name: "approval_classify",
          description:
            "审批四级分级查询：输入工具名+参数，返回级别（READ/WRITE/EXEC/BLOCKED）" +
            "与处置建议（allow/approve/block）。",
          parameters: {
            tool: { type: "string", required: true, description: "工具名" },
            args: { type: "object", additionalProperties: true, description: "工具参数（用于高危命令模式检测）" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => {
            const r = classify(args.tool, args.args, policy);
            return { text: JSON.stringify(r, null, 2) };
          },
        })
      );
    },
    "intelApprovals.tools"
  );

  // pre-execute 硬拦截/审批门：BLOCKED → deny；READ → 放行；WRITE/EXEC → ask
  ctx.effect(
    () =>
      ctx.on("tools/pre-execute", async (exec, next) => {
        const r = classify(exec.name, exec.arguments, policy);
        if (r.level === LEVELS.READ) return next();
        if (r.level === LEVELS.BLOCKED) {
          warn(ctx, `BLOCKED ${exec.name}: ${r.reason}`);
          return {
            kind: "deny",
            reason: r.reason,
            info: { name: exec.name, code: "APPROVAL_BLOCKED", reason: r.reason },
          };
        }
        // Quiet issues a bounded capability to its exact in-process child.
        // BLOCKED still wins; ordinary/remote/forged children remain ask.
        if (r.level === LEVELS.WRITE && exec.name === "memory_write" &&
            await ctx.get?.("quietMemoryAuthority")?.authorize(exec)) return next();
        if (policy.monitor) {
          warn(ctx, `monitor: ${exec.name} → ${r.level}（仅记录，未拦截）`);
          return next();
        }
        // ask：有审批通道（HMC/Web）时弹审批卡；无审批通道时 dsh 会转为拒绝
        // （fail-closed，见 tools/index.ts 的 pre-execute 事件注释）。
        return { kind: "ask", reason: r.reason || `${exec.name} 需要用户批准（${r.level}）` };
      }),
    "intelApprovals.preExecute"
  );
}
