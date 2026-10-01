# dsh-intelligence-cron

[English](README.md) | 中文

cron_create 解析中文时间并创建提醒，cron_list 列出提醒状态，cron_delete 删除 wrapper 记录及底层 schedule。插件最多允许 20 个任务，使用 c1、c2 等递增 ID。

## 调度

每天和每周表达式转换为单次 at 调度，触发后重新安排。固定间隔使用 every_seconds，至少需要 300 秒。相对延迟和明天表达式创建一次性提醒。自带的 cordis.patch.yml 将 @deepseek-ai/dsh-schedule 作为插件加载；它本身不是 profile bundle。

提醒属于创建它的会话，仅在会话存活时跟进。对账恢复下一个未来触发点，不补发错过的提醒。wrapper 元数据位于 $DSH_HOME/intel-cron/cron.json。工具和监听器使用 ctx.effect。

## 移除与验证

卸载前从任务所属会话调用 cron_delete，移除未触发的调度。从其他会话删除任务仅移除 wrapper 元数据，已持久化的提醒仍然有效。仅卸载会移除重新安排的监听器，但已持久化的单次提醒仍可能由 DSH Schedule 触发。运行 node --test tests/parse.test.js tests/scheduler.test.js；tests/integration.sh 使用隔离 profile、真实计时器和模拟 LLM 另行验证。
