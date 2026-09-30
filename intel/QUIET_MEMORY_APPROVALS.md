# Quiet 后台记忆的定向授权

Quiet 复盘子会话在默认 WRITE 审批下没有后台审批通道，`memory_write` 会被拒绝。新增 `autoApproveMemoryWrites` 显式配置，默认 false，部署者可在已允许自动记忆的常驻 profile 启用。

启用后只有 Quiet 自己调用 `subagents.start` 获得的具体本进程 `Agent` 可以使用最多两次 `memory_write` 授权。用户文字、标签、session 元数据不能产生授权；未知或远程 Agent、其他工具、普通会话、取消、超时、卸载和已释放的复盘不获授权。审批插件首先处理 BLOCKED，再查询此能力，并继续后续策略门；不修改全局 WRITE/EXEC 分类或沙箱权限。

授权以进程内 WeakMap 保存，随本次复盘结束撤回，不写入 Session 格式。原生 provider 可能先运行子会话再发布句柄；尚未发布的同父子会话只等待身份确认，不按元数据授权。已获授权的子会话、普通会话、不同父会话不等待其他复盘；同父并发复盘逐次发布唤醒，不等待慢兄弟。配置未启用时仍为原审批策略。

父回合结束监听仍不等待复盘。插件卸载会关闭授权、取消其控制器、清除计时器，并等待启动回滚、子回合及原生 disposal 完成；在读取 transcript 后再检查关闭状态，避免卸载后启动新复盘。

验证：Quiet 状态、身份/预算/取消/并发/卸载和审批分类的 Node 定向测试 42/42。HMC 自建隔离 Host 的真实模型回合已证明父会话 0 工具调用、Quiet 一次成功 memory_write、0 审批请求、SQLite 一条与本次调用精确匹配的记忆、子会话仍从手机列表隐藏。生产配置和 Linux 候选的最终结果由 HMC 发布回执记录；不把此隔离证据称为生产执行。

复核命令：

```sh
node --test intel-plugins/dsh-intelligence-quiet/tests/*.test.js intel-plugins/dsh-intelligence-approvals/tests/approvals.test.js
```

不改核心 `packages/` 或持久化格式。Hooks 自动消费和 stage2 每小时系统计划不随此修复启用。
