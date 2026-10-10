# dsh-intelligence-cron

[English](README.md) | 中文

cron_create 解析中文时间并创建提醒，cron_list 列出提醒状态，cron_delete 删除 wrapper 记录及底层 schedule。创建、列表和删除显示已用容量与固定有效上限 20，包括已完成及其他会话记录。达到或超过上限只拒绝新增；删除、对账及重武装仍可执行。Config 只有 dataDir 和 timeZone，没有 limit 覆盖项。

## 调度

每天和每周表达式转换为单次 at 调度，触发后重新安排。固定间隔使用 every_seconds，至少需要 300 秒。相对延迟和明天表达式创建一次性提醒。自带的 cordis.patch.yml 将 @deepseek-ai/dsh-schedule 作为插件加载；它本身不是 profile bundle。

提醒属于创建它的会话，仅在会话存活时跟进。对账恢复下一个未来触发点，不补发错过的提醒。wrapper 元数据位于 $DSH_HOME/intel-cron/cron.json。工具和监听器使用 ctx.effect。

timeZone 默认为 Asia/Shanghai，空值沿用默认；非空无效 IANA 时区在构造 scheduler 时拒绝。每天、每周及明天表达式使用配置的墙钟时间，间隔和相对延迟使用经过秒数。纽约 DST 切换前后的有效墙钟时间随偏移变化。不存在或重复的 DST 墙钟时间沿用现有 Intl 换算和每日期一个槽位的行为；插件不承诺重复小时投递两次。

## 持久化

cron.json 为权威数据。writer 保存 `{version:1, seq, jobs}`，ID 高水位不下降。旧数组可读，下一次成功变更转换并保留所有记录及扩展字段；当前会话已见 schedule ID 也防止旧数据删除后复用 ID。旧版仅数组代码不能读取新 envelope，代码降级前须保留匹配 JSON 备份。坏 JSON、无效 UTF8、非法记录字段、重复 ID 及读取错误均拒绝；只有可访问目录中的真实缺失文件才初始化为空。不自动丢弃或修复损坏记录。

读改写操作，包括跨 agent 重武装和对账，持有既有 `.writer.sqlite` 排他锁。同进程按规范目录排队，跨进程异步获取最多等待三秒，不阻塞事件循环。JSON 发布使用同目录独占且仅拥有者可访问的临时文件，完整写入、文件 fsync、rename 及目录 fsync。rename 前的写入、同步或 rename 失败保留完整旧 JSON。只清理 writer 独占创建的临时文件。直接 CronStore.save 是底层替换操作，可能并发写入时必须在 transaction 内调用。

JSON 与会话 schedule 是分别提交的。创建/删除先发布 JSON 再追加及 flush 事件；重武装先 flush 新槽位再发布指针。`CRON_*` 错误只暴露安全 errno 和 committed 状态，不含路径及提醒正文。rename 后目录同步或随后的 schedule flush 失败可能 `committed:true`，应读取记录并对账，不盲重放非幂等创建。原子替换与排他不承诺断电 exactly-once 投递，也不协调忽略锁的旧 writer。

dispatch 重武装失败向 [系统事件](../dsh-intelligence-sysevents/README.zh.md) 发射 `cron.rearm_failed`。事件以稳定派生 run ID 与安全代码标识会话、任务和已触发槽位，不包含提醒名称、提示词或原始错误正文。同槽位重复报告去重。flush 失败保留旧槽位的包装元数据；重复 dispatch 在保存元数据前，重试已追加新槽位的持久化屏障。事件写失败独立告警并带原始代码。显式 test scope 保持隔离；正常 dispatch 和成功恢复不发异常通知。

## 移除与验证

卸载前从任务所属会话调用 cron_delete，移除未触发的调度。从其他会话删除任务仅移除 wrapper 元数据，已持久化的提醒仍然有效。仅卸载会移除重新安排的监听器，但已持久化的单次提醒仍可能由 DSH Schedule 触发。运行 node --test tests/parse.test.js tests/scheduler.test.js；tests/integration.sh 使用隔离 profile、真实计时器和模拟 LLM 另行验证。

工具失败使用不同的 CRON_* 错误码区分无效输入、未找到或歧义引用、容量和存储问题。执行器失败正文保留安全 errno 与已证实的 JSON committed 状态。容量拒绝同时显示观察到的已用数量和有效上限。部分提交后先读取与对账，不重复 create/delete。成功保留 {ok:true,text}。
