// dsh-intelligence-hooks —— 事件自动化 Hooks（复刻 Python 版 agent/hooks.py）
//
// hook = name → { trigger, prompt 模板, desc }，持久化到 DSH_HOME/intel-hooks/hooks.json。
// trigger 类型：
//   - file：轮询数据目录下的子目录（默认 inbox/），新文件只触发一次（mtime 去重）
//   - webhook：由外部事件通过 hook_fire(source=webhook) 触发
// 触发时按 prompt 模板渲染生成任务文本，记入内存任务队列 + fires.jsonl；
// dsh 插件侧不直接跑 agent 回路（与 Python 版"action: agent" 的执行语义差异见 README）。
//
// 所有注册走 ctx.effect，可卸载回卷（轮询 timer 停掉）。存储在 DSH_HOME/intel-hooks/。

import z from "@deepseek-ai/schemastery";
import { HookStore, defaultDataDir } from "./src/store.js";
import { HookManager, DEFAULT_POLL_INTERVAL_MS } from "./src/manager.js";
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
});

export function apply(ctx, config) {
  const cfg = config ?? {};
  const store = new HookStore(cfg.dataDir || defaultDataDir());
  const manager = new HookManager({
    store,
    ctx,
    pollIntervalMs: cfg.pollIntervalMs || DEFAULT_POLL_INTERVAL_MS,
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
