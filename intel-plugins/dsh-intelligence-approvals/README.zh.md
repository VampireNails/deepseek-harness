# dsh-intelligence-approvals

[English](README.md) | 中文

这个树外插件在执行前对工具分级，并复用 DSH 审批服务。READ 直接允许执行；WRITE 请求审批；EXEC 先检查高危命令模式，再请求审批；BLOCKED 直接拒绝执行，不显示审批卡。未知工具默认为 WRITE。没有审批通道的 profile 会拒绝需要审批的工作。

## 配置

levels 覆盖单个工具的分级，blocked 添加高危正则表达式，sensitive 添加带原因的 EXEC 模式，defaultLevel 指定未知工具级别。monitor 仅记录普通分级而不拦截，BLOCKED 仍拒绝执行。非法级别覆盖和非法正则表达式会被丢弃。

index.js 通过 ctx.effect 注册 approval_classify 和 tools/pre-execute 监听器。src/classify.js 负责分类，src/policy.js 合并配置。profile bundle 加载 cordis.patch.yml，并提供 approvals 服务。

## 验证

运行 npm ci 和 node --test tests/approvals.test.js。分类单测本身不能证明手机审批流程，需要具备审批通道的真实 Host 验证。
