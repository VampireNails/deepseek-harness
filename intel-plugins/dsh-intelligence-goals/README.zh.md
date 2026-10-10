# dsh-intelligence-goals

[English](README.md) | 中文

GoalsStore 管理 `$DSH_HOME/intel-goals/goals.json`，内容为 `{goals, seq}`。goal_create、goal_progress、goal_close 和 goal_list 与 intelGoals 服务共享存储。ID 单调递增；JSON 保留每条进展，Markdown 展示每个活跃目标最近十条进展。保留既有 JSON 格式和扩展字段。非法 JSON、UTF8、记录字段、序号、重复 ID 或权限错误均拒绝，不返回空库。只有可访问目录中真实缺失的文件才初始化为空。

变更从读取、修改到发布始终持有既有 `.writer.sqlite` 跨进程 writer 锁。目标同目录内的私有、仅拥有者可访问、独占临时文件完整写入并 fsync 后 rename，再 fsync 目录。rename 前失败保留完整旧 JSON。JSON 是权威数据，goals.md 是 JSON 之后分别提交、可重新生成的投影。投影失败报告 `GOALS_PROJECTION_FAILED`、安全 errno 及 `committed:true`；JSON rename 后目录同步失败也报告已提交。错误不含原始路径和内容。读取 JSON 或 goal_list 确认已记录目标/进展后再决定变更，不盲目重复创建或记进展。

load、findGoal 和 listGoals 只读权威 JSON，不依赖 Markdown 或写锁。rebuildMarkdown 只修复投影。goal_list 尝试修复，Markdown 或 writer 锁仍不可用时仍展示 JSON 记录并附警告。投影修复和列表都不重放业务变更。两个文件不是事务；原子 rename 和 writer 排他不承诺断电 exactly-once 效果，也不保护忽略锁的旧 writer。既有目标时间戳沿用进程本地时区。

在本插件目录运行 `node --test tests/goals.test.js`。cron 拥有者测试还覆盖共享持久化故障、独立进程，以及隔离的受支持 DSH profile；使用真实 registry/executor，不发送 provider 请求。

所有失败的目标操作均返回 isError=true 与稳定 GOALS_* 错误码。goal_list 在 JSON 可读而 Markdown 投影不可用时明确为部分可用：失败结果仍包含可读目标及 Markdown 未同步提示。修复投影不会重复进展或创建。正常成功输出保留 {text}。
