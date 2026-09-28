// dsh-intelligence-cron —— 用户自建定时任务（P0，REPLICATION #11）
//
// 复刻 Python 版 agent/custom_cron.py：中文时间解析 + cron_create/list/delete。
// 调度层由 @deepseek-ai/dsh-schedule 承担；本插件的 cordis.patch.yml 自带
// Schedule overlay，把 dsh 自带的 @deepseek-ai/dsh-schedule cordis 插件挂载为
// host 服务（profile 无需另配，也不能把它写进 bundles）：
//   - 每N小时/每N分钟 → every（dsh 原生固定频率）
//   - X分钟后/X小时后/明天H点 → after/at 一次性
//   - 每天X/每周X → one-shot at 链（dispatch 后重武装 + 会话初始化对账）
//
// 语义是 session-local：提醒只在创建它的会话 live 时到点跟进。会话死亡期间错过的
// 触发点只防丢（补建下一个未来点），不补发。
// 所有注册走 ctx.effect，可卸载回卷；存储在 DSH_HOME/intel-cron/cron.json。

import z from "@deepseek-ai/schemastery";
import { CronStore, defaultDataDir } from "./src/store.js";
import { CronScheduler } from "./src/scheduler.js";
import { cronCreateTool, cronListTool, cronDeleteTool } from "./src/tools.js";

export const name = "dsh-intelligence-cron";

export const inject = ["agents", "sessions", "tools"];

export const Config = z.object({
  dataDir: z.string().default(""),
  timeZone: z.string().default("Asia/Shanghai"),
});

export function apply(ctx, config) {
  const cfg = config ?? {};
  const store = new CronStore(cfg.dataDir || defaultDataDir());
  const scheduler = new CronScheduler({
    store,
    timeZone: cfg.timeZone || "Asia/Shanghai",
    ctx,
  });

  // 重武装：one-shot dispatch 后为每天/每周任务追加下一轮 at
  ctx.effect(
    () =>
      ctx.on("session/event", (session, event) => {
        try {
          scheduler.handleSessionEvent(session, event);
        } catch (err) {
          scheduler.warn(`session/event 处理失败: ${err?.message}`);
        }
      }),
    "intelCron.sessionEvent()"
  );

  // 对账：新 agent 接管会话时，补建本会话归属 job 的存活 schedule（幂等）
  ctx.effect(
    () =>
      ctx.on("agent/created", ({ agent }) => {
        scheduler.reconcile(agent).catch((err) => scheduler.warn(`对账失败: ${err?.message}`));
      }),
    "intelCron.reconcile()"
  );

  ctx.effect(
    function* () {
      yield ctx.tools.register(cronCreateTool(scheduler));
      yield ctx.tools.register(cronListTool(scheduler));
      yield ctx.tools.register(cronDeleteTool(scheduler));
    },
    "intelCron.tools"
  );
}
