// dsh-intelligence-hooks —— 事件自动化 Hooks（复刻 Python 版 agent/hooks.py）
//
// hook = name → { trigger, prompt 模板, desc, auto }，持久化到 DSH_HOME/intel-hooks/hooks.json。
// trigger 类型：
//   - file：轮询数据目录下的子目录（默认 inbox/），新文件只触发一次（mtime 去重）
//   - webhook：由外部事件通过 hook_fire(source=webhook) 触发
// 触发时按 prompt 模板渲染生成任务文本，记入内存任务队列 + fires.jsonl；
//
// 消费方（2026-09-30 闭环）：有界自动调度。fire() 把任务记入 pending/<hook>.jsonl 后，
// _maybeDispatch 按「单 hook 冷却（默认 15 分钟）+ 全局并发上限（默认 3）+ 单 hook 串行」
// 起一个 headless 任务（taskBin，默认 /root/intel/bin/intel-task.sh → dsh --profile evolve），
// 多次触发合并为一批 prompt。相对 Python 版 `action: agent`（无节流）的超越点：
// at-most-once 消费、冷却节流、并发上限、dispatch.log 审计、调度失败保留 pending 重试。
// auto=false 的 hook 只记录不调度。可通过插件配置关闭全局自动调度。
//
// 所有注册走 ctx.effect，可卸载回卷（轮询 timer 停掉）。存储在 DSH_HOME/intel-hooks/。

import z from "@deepseek-ai/schemastery";
import { HookStore, defaultDataDir } from "./src/store.js";
import {
  HookManager,
  DEFAULT_POLL_INTERVAL_MS,
  DEFAULT_DISPATCH_COOLDOWN_MS,
  DEFAULT_TASK_BIN,
  DEFAULT_MAX_CONCURRENT_DISPATCHES,
} from "./src/manager.js";
import {
  hookRegisterTool,
  hookListTool,
  hookRemoveTool,
  hookFireTool,
} from "./src/tools.js";

export const name = "dsh-intelligence-hooks";

export const inject = ["tools"];

export const Config = z.object({
  dataDir: z.string().default(""),
  pollIntervalMs: z.number().default(DEFAULT_POLL_INTERVAL_MS),
  autoDispatch: z.boolean().default(true),
  dispatchCooldownMs: z.number().default(DEFAULT_DISPATCH_COOLDOWN_MS),
  taskBin: z.string().default(DEFAULT_TASK_BIN),
  maxConcurrentDispatches: z.number().default(DEFAULT_MAX_CONCURRENT_DISPATCHES),
});

export function apply(ctx, config) {
  const cfg = config ?? {};
  const store = new HookStore(cfg.dataDir || defaultDataDir());
  const manager = new HookManager({
    store,
    ctx,
    pollIntervalMs: cfg.pollIntervalMs || DEFAULT_POLL_INTERVAL_MS,
    dispatch: {
      enabled: cfg.autoDispatch !== false,
      cooldownMs: cfg.dispatchCooldownMs || DEFAULT_DISPATCH_COOLDOWN_MS,
      taskBin: cfg.taskBin || DEFAULT_TASK_BIN,
      maxConcurrent: cfg.maxConcurrentDispatches || DEFAULT_MAX_CONCURRENT_DISPATCHES,
    },
  });
  ctx.provide("hooks", manager);

  ctx.effect(
    function* () {
      yield ctx.tools.register(hookRegisterTool(manager));
      yield ctx.tools.register(hookListTool(manager));
      yield ctx.tools.register(hookRemoveTool(manager));
      yield ctx.tools.register(hookFireTool(manager));
    },
    "intelHooks.tools"
  );

  // 轮询 watcher：卸载时返回的 disposer 停掉 timer（回卷）
  ctx.effect(() => {
    manager.start();
    return () => manager.stop();
  }, "intelHooks.watcher");
}
