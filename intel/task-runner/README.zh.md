# Linux 任务运行器

[English](README.md) | 中文

任务最终失败发射 `task.failed`，携带 JobLog 的 run ID、安全代码和退出类别。事件写失败独立写入 stderr 与路由审计，保留任务原非零退出。事件或 JobLog 写错误不能把成功任务改记失败。真实锁超时发射 `runner.lock_timeout`，不创建已完成任务运行。routes 配置的 `lockTimeoutSeconds` 支持 1–610 秒，默认 610；私有获取标记区分 flock 冲突退出 75 与任务自身退出 75。正常 upkeep 准入/退避及成功任务不发失败事件。隔离、去重和源恢复见 [系统事件](../../intel-plugins/dsh-intelligence-sysevents/README.zh.md)。

ChatGPT cron 路由显式为每个一次性进程使用 SSE。否则，已安装 SDK 的默认 WebSocket 缓存会在回复后继续保留进程五分钟。此单次调用 patch 不改变 web profile。路由的 `transport` 接受 `sse`、`websocket`、`websocket-cached` 或 `auto`；fallback 请求使用其自身路由的传输方式。

此运行器从演进功能当前的 `TASKS` 模板中解析内置 cron 任务 ID。内置任务会忽略可选的旧提示词参数；自定义任务必须提供明确的提示词。`routes.json` 提供任务路由、默认提供方与模型，以及超时时间。运行器仅启动 `evolve` DSH profile，并显式传入模型 patch。它依赖已安装的 profile 依赖和正常的提供方凭据，不会安装提供方或图像工具。

`route-runs.jsonl` 记录请求及运行时解析的 provider/model、模板、原进程退出码、信号、稳定失败类别和 fallback 原因，不记录提示词或凭据。额度、限流、鉴权、服务、网络、模型配置、工具和结果未知分别保留。退出码 1 及 provider/工具文字不能单独证明额度耗尽。provider stderr 只读取不转发，因为它可能包含请求数据。超时终止进程组，结果仍未知。

JobLog 完成记录携带与路由审计相同的 run ID 和开始时间，以及最终退出码、选路原因、请求/实际选择、尝试次数和降级原因。未配置的自定义任务保留兼容默认路由，并显示 `unconfigured-task`；缺少实际身份时保持 `unknown`。`INTEL_JOBLOG_SCOPE=test` 显式隔离受控测试；缺省选择生产，其他值拒绝。JobLog 持久化错误以非零退出并按安全代码审计，不为已完成任务另造一条失败运行。保留策略、部分持久化、旧快照和备份要求见 [JobLog](../../intel-plugins/dsh-intelligence-joblog/README.zh.md)。

每个任务的 `fallback` 指向一个路由。顶层 `fallback` 必须设置 `enabled: true`、包含主尝试的 `maxAttempts`（1–2）及 `totalTimeoutMs`（1000–600000）。缺失/关闭配置保留单次进程；启用时关闭 profile 的 provider retry 插件，各次尝试共享单调时钟总期限。随仓任务最多采用一次备用路由。只有可靠的首次 HTTP 402/429 额度拒绝或 429 限流拒绝，且完整运行回执证明仅一个会话、一步、无工具/输出/部分流/retry、模型匹配时才允许 fallback。鉴权、5xx、网络中断、超时、信号、损坏/缺失回执及任何工具或输出都禁止重放；`retrySafe` 为兼容仍可配置，但不能绕过限制。两次尝试共享原 run ID/日期/scope，最终只有一条 JobLog 完成记录，失败时产生一条最终事件。

运行器通过支持的 CLI patch 入口插入被动运行时观测。独立继承管道只传安全事实；父进程保留已观察的副作用，即使后续回执重置计数也不能清除。执行前核验解码后的默认选择，分发前核验请求选择。`runtime.defaultModelId`、`runtime.piAiId`、`runtime.retryId` 和 `runtime.headlessId` 可以适配插件 ID；缺省分别对应 `agent-default-model`、`llm-pi-ai`、`llm-retry` 和 `headless-runner`。headless 准入依赖观测就绪；解码选择或请求选择不匹配时，在 provider 分发之前失败。即使 retry 插件 ID 过时，终止请求错误观测也会阻止嵌套恢复。运行时请求记录证明解析后的选择；所属 Linux 测试另以已安装 CLI 和真实 provider 客户端访问 loopback HTTP provider，捕获实际 request model。未提供可靠 HTTP status 的 provider 可分类，但不能授权 fallback。

配置 `visibleOutput: "required"` 的任务必须留下本次运行的 Feed 文章或 Artifact 版本。运行器的最终 dreaming 提示词要求 Feed 发布，保留模板禁止 `artifact_save` 的要求；其他模板保留长报告可选提示。进程退出零但没有可读产出时改记退出码 65，fallback 也适用，并且不会因此重试。运行器按配置时区给发布工具注入任务 ID、唯一运行 ID 和日期。`references` 仅接受已有记忆的整数 ID，写入前核验。手动发布不虚构运行身份。upkeep 没有新信号时可安静成功，无需发布。

带引用的产出保留配置的记忆目录，因此即使 memory 使用自定义 `dataDir`，产出检查也读取同一数据库。演进插件按 runner 身份，把新创建的根会话记入共享 DSH home 下的 `intel-joblog/automation-sessions.sqlite`。它排除标准子智能体和普通会话，保留会话 header，不根据工作区路径或提示词片段猜测自动任务。读取方以只读方式打开索引，缺少身份的历史会话保持未分类。

[历史来源归类](legacy-session-origin.mjs) 是处理可信私有观测的离线维护。只有首条真实用户完整消息匹配确切历史任务模板、header 与消息时间均位于一个配对审计区间，且根会话与区间双向唯一时才可归类。`dry-run --index /absolute/index.sqlite` 从 stdin 读取观测，仅输出哈希；`apply` 还要求未占用的绝对 `--backup` 路径。合成的 `legacy-evidence:` 运行 ID 区分迁移证据与原生 runner ID。来源冲突会回滚整批写入。`rollback` 接收匿名 apply 回执，只删除本次新增且完整身份仍匹配的行，保留后续原生记录。调用方必须提取并核实原始日志；CLI 不验证任意输入观测的真实性，也不推测未知历史。

`deduplicateDaily: true` 在执行前检查已持久化的同任务、同日期产出，并记录 `already-published` 及原运行 ID。Linux `flock` 将各任务从检查、执行到最终审计串行化，不同任务互不阻塞。Feed 与 Artifact 存储也串行写入，拒绝同一发布键对应冲突内容。Feed 展示最近 100 条，发布身份保存在 `.publication-history.sqlite`；Artifact 各版本保留各自的运行身份与记忆引用。

停止写入后备份完整 `intel-feed` 与 `intel-artifacts` 目录，包含隐藏 SQLite 文件。恢复或切换版本时保留 `.publication-history.sqlite`：删除它会丢失展示窗口之外文章的重复保护。切换旧 writer 前停止 cron runner，因为旧版本不参与这些锁。失败运行可能已经保存了产出，手动重试前先检查审计与产出。

部署前先备份 `crontab -l`，再将其输出传给 `node intel/task-runner/cli.mjs --migrate-crontab` 并比较预览。该命令只输出替换结果，从不写入 crontab。它仅迁移已注册、提示词为单个引号字面量的内置任务；自定义任务、复合命令、重定向和依赖特定环境的包装器保持原样，留待人工检查。检查后部署 `run.sh` 并赋予执行权限，保留 cron 的时间安排与时区，再检查首次运行。不要盲目重跑失败的任务。仅添加此目录不会改变现有 profile 配置和生产 cron。

在 Linux 上运行 `node --test intel/task-runner/tests/*.test.mjs`。隔离的 CLI 测试使用假的 `dsh` 可执行文件，从不调用生产模型。

`routes.json.heartbeat.enabled` 启用有界本地预检；缺少配置或为 `false` 时保留旧分发行为。已存在的空清单按 `heartbeat-empty-checklist` 停用；清单缺失、不可读或非空但不受支持时失败。支持的 [清单](../../intel-plugins/dsh-intelligence-evolution/HEARTBEAT.md) 对应服务状态/重启次数、精确 HMC 监听、磁盘和内存阈值、完整近期 journal 错误记录，以及 JobLog/路由历史。来源读取不启动模型，也不写来源数据。未知、截断、损坏或权限拒绝保留原安全代码，通过 runner 失败事件报告；不根据提示词关键词推测结果。近期旧 start 没有 run ID 时仍未确认；选中路由、JobLog 和告警时间晚于观测截止点时失败，不容许时钟偏移。旧来源保持未分类，显式测试行被排除，heartbeat 自身失败由 pending 检查点管理，避免自触发循环。

Heartbeat 配置必须提供 `serviceUnit`、`listenAddress`、`listenPort`、`minDiskBytes`、`minMemoryBytes`、`sourceTimeoutMs`、`maxSourceBytes`、`windowMs`、`pendingAfterMs`、`initialBackoffMs` 和 `maxBackoffMs`。随仓配置使用探针 5 秒期限、每文件/命令 4 MiB 上限、一小时错误窗口、700 秒未完成阈值、2 GiB 磁盘和 100 MiB 内存阈值，以及一至四小时相同异常退避。每次调用先获取全部当前来源，再判断退避。稳定健康阈值结果启动零模型；变化信号、恢复或到期相同异常准入一次运行。健康阈值内的可用资源波动不改变身份。来源/配置变化重置退避；相同异常遇到时钟回退明确失败，变化信号仍可准入并保留单调截止时间。

Heartbeat flock 覆盖预检、准入、分发和审计。生产使用 `intel-joblog/heartbeat/state.json`；显式 test scope 有独立锁与 `test-state.json`。运行器启动前 fsync 持久化 pending 信号/运行 ID，只有进程成功并且完成检查点可靠写入后才清除 pending。失败、崩溃或完成不确定时，重启后仍拒绝同一信号重复分发。新的可靠信号可替代 pending，在审计中保留原身份，不重放原副作用。升级时保留状态；手动修正前检查原运行和系统事件。安静决策记录 reason、清单/信号/来源身份哈希、观测时间和零执行，不创建成功模型 JobLog 运行或异常通知。准入摘要只含状态和安全事件身份，不含 journal 消息或提示词，通过 profile 已记录的任务输入发送。进程成功不代表已核验模型/工具结果或通知交付。

两个 upkeep 别名共用 `memory-upkeep` flock 和私有的 `intel-joblog/upkeep/state.json`。首次调用记录现有原始会话截点，不启动模型，也不把历史视为新增输入。后续调用消费带有 [Host 作者标记](../../intel-plugins/dsh-intelligence-evolution/README.zh.md#understand-the-implementation) 的新增原始用户通道消息；排除已归类的自动任务和标准子智能体。旧来源服务缺少标记时仍视为未知，标记无效或包含机器消息时，分发前拒绝来源批次。同一时刻的消息按原始事件序号区分。完整消息必须符合配置的批次限制；原文过大或来源不可用时失败，不推进检查点。

`routes.json.upkeep` 提供现有 Host 的仅所有者可访问的 `socketPath`、`sourceTimeoutMs`、批次限制，以及初始和最大退避毫秒数。生产默认允许 12 条消息、24,000 字符，空轮次按一、二、四小时退避。退避期间的调用不查询会话，也不启动模型。新增输入在下次到期调用时发现，不会立即重置截止时间。时钟回退时检查点不变。[演进插件](../../intel-plugins/dsh-intelligence-evolution/README.zh.md) 负责来源配置和原始读取限制。

非空批次启动前，运行器将待确认运行身份、哈希和拟提交游标 fsync 持久化。它为 upkeep 将工具设置为 native 模式，通过 profile 已记录的任务输入发送原始批次。只有进程成功且该次运行已持久化的完成轮次与工具结果检查通过后，才推进检查点。已完成的轮次没有值得保存的记忆时可以不写入而提交。缺少完成记录、工具被拒绝、写入失败或进程结果不确定时保留待确认批次。后续调用检查原来的别名和运行，不重复分发待确认副作用。无法确认的待处理运行须由操作人员检查。升级时保留私有状态文件；任何手动修正前检查实际日志和记忆写入，删除状态可能丢失尚未确认的输入。

保留生产路由分工：upkeep 和 heartbeat 使用 Flash；晨间简报及其余六个演进任务使用 ChatGPT。`bash intel/verify-linux.sh` 安装每个插件的锁定依赖，并执行 Linux 插件与运行器回归；应在一次性检出目录中运行，因为它会替换插件依赖目录。
