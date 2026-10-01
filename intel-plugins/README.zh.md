# Intel 插件

[English](README.md) | 中文

Intel 插件通过既有插件接口为 DSH Host 添加 Muse 能力，HMC 经服务桥接访问这些能力。DSH 基线记录在 ../dsh-baseline.json。

将插件目录加入 DSH profile 前，先在各插件目录运行 npm ci 安装锁定依赖。克隆仓库不会包含被忽略的 node_modules。插件工具和监听器使用 ctx.effect，卸载时会移除注册。凭据保存在 Git 之外。

cron 插件提供 Schedule overlay，approvals 插件对工具执行分级；artifacts、browser、Feed、Goals、memory、profile、Soul、Hooks 和 upkeep 插件提供各自的工具与数据。配置、运行限制和测试命令由各插件 README 说明。
