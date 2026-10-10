# Intel 插件

[English](README.md) | 中文

Intel 插件通过既有插件接口为 DSH Host 添加 Muse 能力，HMC 经服务桥接访问这些能力。DSH 基线记录在 ../dsh-baseline.json。

将插件目录加入 DSH profile 前，先在各插件目录运行 npm ci 安装锁定依赖。克隆仓库不会包含被忽略的 node_modules。插件工具和监听器使用 ctx.effect，卸载时会移除注册。凭据保存在 Git 之外。

cron 插件提供 Schedule overlay，approvals 插件对工具执行分级；artifacts、browser、Feed、Goals、memory、profile、Soul、Hooks 和 upkeep 插件提供各自的工具与数据。配置、运行限制和测试命令由各插件 README 说明。

Cron、Goals、memory_write 与系统事件工具通过受支持的 HarnessError 报告稳定错误码。执行器记录 isError=true 和 error.info.code；模型与默认用户视图收到固定安全正文，不包含原始异常。JSON errno 与 committed 状态仅对拥有者构造的持久化故障显示在正文中，不是额外的 ToolErrorInfo 字段。成功输出值、schema 和 memoryWrite 元信息保留既有字段。其他 intel 工具保持自身语义；成功的空搜索不是操作失败。
