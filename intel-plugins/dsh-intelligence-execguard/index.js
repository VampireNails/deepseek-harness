// dsh-intelligence-execguard —— 无头执行守卫（evolve profile 专用）。
//
// 背景：dsh-intelligence-approvals 只挂 web profile。无头（evolve）下若挂全量
// approvals，WRITE/EXEC 的 ask 会被 dsh 转为拒绝（fail-closed），会把现有
// cron 任务（morning_briefing 等要 feed_post/memory_write）全掐死，所以
// approvals 不能直接上 evolve。但此前 evolve 的 bash 对高危命令（删根、
// fork 炸弹、关机等）没有任何拦截，自主执行引擎上线前必须补上这条红线。
//
// 做法：复用，不复制。直接 import approvals 的 src/classify.js（纯函数，
// 无副作用），在 tools/pre-execute 只拦 BLOCKED，其余一律 next() 放行。
// 现有 cron 任务行为零变化，只补高危硬拦截。定策略的口（policy.js 的
// buildPolicy）仍在 approvals 侧，本插件只消费 DEFAULT_POLICY。
//
// 挂法：evolve profile bundles 中 dsh-headless 之后（尽早注册，deny 是终结性的，顺序不敏感）。

import {
  LEVELS,
  classify,
  DEFAULT_POLICY,
} from "../dsh-intelligence-approvals/src/classify.js";

export const name = "dsh-intelligence-execguard";
export const inject = ["tools"];

function warn(ctx, msg) {
  try { ctx.logger?.warn?.(`dsh-intelligence-execguard: ${msg}`); } catch { /* ignore */ }
}

export function apply(ctx) {
  ctx.effect(
    () =>
      ctx.on("tools/pre-execute", async (exec, next) => {
        let r;
        try {
          r = classify(exec.name, exec.arguments, DEFAULT_POLICY);
        } catch (err) {
          // 分类器自身异常（理论上只可能是代码 bug）：fail-open 放行并大声记录。
          // 理由：本层是纵深防御的加分项，主防线是 dsh 沙盒（workspace-write）；
          // fail-closed 会把所有无头任务（含 cron）一次性打死，blast radius 更大。
          warn(ctx, `classify 异常，fail-open 放行：${err?.message || err}`);
          return next();
        }
        if (r.level === LEVELS.BLOCKED) {
          warn(ctx, `BLOCKED ${exec.name}: ${r.reason}`);
          return {
            kind: "deny",
            reason: r.reason,
            info: { name: exec.name, code: "EXECGUARD_BLOCKED", reason: r.reason },
          };
        }
        return next();
      }),
    "intelExecGuard.preExecute"
  );
}
