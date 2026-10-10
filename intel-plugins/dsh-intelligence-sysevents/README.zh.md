# 系统事件

[English](README.md) | 中文

系统事件持久化异常，供 HMC 已认证只读 `muse/system-events/list` 读取。插件提供发射、调试工具及程序化 `sysevents` 服务。runner 与 cron 调度器直接复用同一个 store，失败无需可用模型或已挂载本插件。

## 存储与身份

目录为 `$DSH_HOME/intel-system-events`，默认 `~/.dsh/intel-system-events`。生产使用 `events.jsonl`；显式 `INTEL_JOBLOG_SCOPE=test` 或事件 `scope: test` 使用 `events.test.jsonl`。其他 scope 拒绝。既有未标 scope 的事件仍可读，不按名称重新分类或迁移。默认列表排除显式 test；程序化 `listEvents({includeTests:true})` 包含它们。

任务事件包含稳定 `type`、`source`、`taskId`、`runId`、安全 `code`、退出类别，以及适用的退出/会话/调度字段。runner 的 run ID 对应 JobLog 与路由审计；cron 根据所属会话与已触发槽位派生 ID。title、detail 保留既有客户端字段。生产者排除提示词、名称、凭据、路径和原始错误正文；任意额外字段不持久化。

结构化身份在 scope 内决定事件 ID。共享 SQLite 写锁内，相同报告返回首条事件及时间；冲突报告拒绝 `SYSEVENTS_IDENTITY_CONFLICT`。非结构化工具发射仍使用唯一 ID。去重仅在源文件保留期间有效；轮转会丢失旧身份。

## 失败与恢复

写入追加并 fsync 后才返回。源缺失时列表为空；损坏、不可读或过大的源返回安全 `SYSEVENTS_*` 错误。16 MiB 源上限拒绝后续访问，不静默截断去重范围。HMC 扫描有界尾部；完整坏行明确失败，不完整追加等待下次轮询。空或缺失源不代表通道坏，有界读取不保证跨轮转交付。

追加或 fsync 失败时可能已写入字节。保留原始任务结果，重试通知前检查事件 ID；不能重放任务修复遥测。修复或轮转损坏源前停止 writer 并保留目录，包括 `.writer.sqlite`。末尾不完整行需要人工修复；writer 不擦除它或虚构交付成功。旧 writer 不参与共享锁，切换版本前必须停止。

sysevents_emit 与 sysevents_list 使用执行器错误协议：失败结果设置 isError=true，提供稳定 SYSEVENTS_* 错误码和安全恢复正文。未知异常不暴露原文。追加或同步失败的结果尚未确认，不能冒称 JSON 已提交；决定再次发射前先读取来源。成功 {text} 输出保持不变。
