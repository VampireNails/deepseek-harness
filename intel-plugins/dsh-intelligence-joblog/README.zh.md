# JobLog

[English](README.md) | 中文

JobLog 使用 Node 内置 SQLite，在 `intel-joblog/job_history.sqlite` 保存完成的运行。每次完成包含稳定 `runId`、任务 ID、来源、UTC 完成时间、可选开始时间、状态、诊断详情和摘要。`historyLimit` 默认每个来源保留 200 次完成，接受 1–1000 的整数。最新完成在前；fail → fail → ok 保留两次失败直到保留策略淘汰，有限历史不能证明终身成功率。最后状态仍保存在 `job_runs.json` / `job_test_runs.json`；`job_alerts.md` 保留失败，`jobs.log` 最多保留 2000 行包装器日志。进程审计仍在 `route-runs.jsonl`。

新记录明确携带 `production` 或 `test` 来源。`JobLog(dir, { scope: 'test' })` 与插件 `testRecords: true` 选择测试；普通写入默认生产。测试最后状态仍独立保存，同名任务不能覆盖生产。`readHistory()` 与工具默认只选择生产；`includeTests: true` 显式包含测试和来源未标注的旧快照。`readRuns()` 保留兼容的最后状态投影。任务名不决定来源。旧记录保留来源和原日期；旧 latest 条目覆盖前单独保留原快照，不虚构 run ID 或还原更早运行。读取筛选不重写来源文件，安装此功能不迁移生产数据。

共享 SQLite writer 锁串行化写入，等待忙锁最多三秒；历史数据库最多等待五秒。保留历史内重复 `(scope, runId)` 完成只计一次；任务、状态或详情冲突则拒绝。迟到的重复完成不能替换较新最后状态。历史先提交，再在持有 writer 锁时原子 rename 并 fsync latest JSON。提交失败不会推进兼容文件。后续兼容写入失败时已提交 SQLite 仍为权威；调用方收到带 `committed: true` 的存储错误，不能当作持久化成功回执。损坏或不可读状态拒绝，不伪装为成功空视图。日志、完成和告警写入失败均抛出安全 `JOBLOG_*` 码，以及操作和可用的安全 errno。任务失败叠加存储失败时携带原 `taskError` 和 `persistenceCode`；存储错误不改变已完成任务的状态。持久化失败可能发生在部分副作用之后，重试任务前按 run ID 检查。

可信 runner 诊断最多保存 2048 字符，摘要 200 字符，`detailTruncated` 标注截断；常见凭据/prompt 字段脱敏。`guarded()` 仅保存完成/失败分类和安全错误码，不保存任意返回正文或错误消息；调用方仍收到原结果/错误。告警详情缩进，不能引入来源标题。工具告警默认仅显示明确标记的生产块；全部来源读取保留未知旧块。兼容的 `selectAlerts()` 默认保留未标注告警，除非请求 `productionOnly: true`。告警缺失显示空视图；其他读取失败拒绝，不暴露路径。空选择不证明所有任务成功。

停写时将 SQLite 与全部 latest/日志/告警一起备份。旧读取器能显示最后状态和保留告警，不能显示新运行历史。去重仅限保留的运行，已淘汰 ID 不构成永久幂等注册表。来源来自显式写入元数据，不来自任务名。不重新归类或清除任务定义、配对、历史来源或生产记录。

在 Linux 运行 `node --test intel-plugins/dsh-intelligence-joblog/tests/*.test.js`。所属夹具使用临时目录，不调用生产 Host 或模型。
