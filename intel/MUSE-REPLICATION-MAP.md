# Muse → dsh-muse 能力复刻映射（2026-10-04）

> 基线：Muse 智能体（非模型）2026-10-04 快照
> 目标：一切为了让 dsh-muse 比 Muse 更智能
> 评估标准：按能力、任务完成度，不按"有没有功能"

## 已实现机制（18 个 intelligence 插件，按能力列项）

能力条目与物理插件数量分别统计。Heartbeat 复用 evolution；主动检查是部署脚本。表内状态表示机制存在，任务质量与生产可用性以运行证据为准。

| Muse 能力 | dsh-muse 插件 | 状态 |
|---|---|---|
| 记忆（MEMORY.md） | dsh-intelligence-memory | ✅ 含质量审计 |
| 画像（USER.md） | dsh-intelligence-profile | ✅ step1 注入 |
| 自我认知（SOUL.md） | dsh-intelligence-soul | ✅ |
| 浏览器 | dsh-intelligence-browser | ✅ Playwright |
| Artifacts | dsh-intelligence-artifacts | ✅ |
| 审批 | dsh-intelligence-approvals | ✅ 四级 |
| Hooks | dsh-intelligence-hooks | ✅ 闭环 |
| 目标 | dsh-intelligence-goals | ✅ |
| 目标执行 | dsh-intelligence-goalact | ✅ 熔断 |
| Feed | dsh-intelligence-feed | ✅ |
| 进化 | dsh-intelligence-evolution | ✅ 含 dreaming 学习闭环 |
| Heartbeat | dsh-intelligence-evolution | ✅ 7 检查项 |
| Joblog | dsh-intelligence-joblog | ✅ |
| Token 可见性 | dsh-intelligence-tokenlog | ✅ 分模型 |
| Cron | dsh-intelligence-cron | ✅ |
| Quiet | dsh-intelligence-quiet | ✅ |
| Guard | dsh-intelligence-guard | ✅ fail-closed |
| ExecGuard | dsh-intelligence-execguard | ✅ |
| 系统事件 | dsh-intelligence-sysevents | ✅ |
| 主动检查 | proactive-check.sh（部署脚本） | 条件触发机制，非独立插件 |

## 未复刻（重估后，2026-10-04 11:5x）

| Muse 能力 | 状态 | 重估结论 |
|---|---|---|
| 图片生成 | 未复刻 | 生产 web profile 已声明第三方 `dsh-image-gen` Git 依赖及同名配置 patch，bundles 未列出该插件；有效工具注册、真实生成和手机读取产物尚无验收证据。维持 [REPLICATION.md](REPLICATION.md) A4 范围，不把依赖声明或手写 SVG/PNG 当作生成模型验收。 |
| Gmail、Google Calendar 等外部连接器 | ❌ 维持不复刻 | 本项目未部署相应 OAuth 连接器，不在当前交付范围；不据此判断其它产品能否接入。 |
| 真实 MCP server | ❌ 维持不复刻 | 本项目不交付该集成；不把项目未实现写成其它产品不支持 MCP。 |
| Apple Healthkit、Spotify 等 | ❌ 维持不复刻 | 各服务的授权和平台集成未纳入当前范围。 |

ChatGPT 模型接入不自动提供图片生成工具，也不改变外部连接器的产品范围。未来若启用图片生成，须同时验证工具注册、profile 挂载、真实模型生成和手机展示，才可变更本表状态。

## 超越项评估（按能力/任务完成度，2026-10-04 12:0x）

> 标准：有功能≠超越。需证据证明能力更强或任务完成度更高。

| 候选 | 证据 | 结论 |
|---|---|---|
| 多模型路由 | 静态任务表。未知 test_* 任务默认 flash，不能据此判断其它提供方失效。生产两日聚合有 deepseek-official/deepseek-flash 与 openai-codex/gpt-5.5 的请求及响应用量。 | 多提供方响应有证据；任务路由质量、回退归因与超越仍未证明 |
| 图片生成 | web profile 声明第三方依赖及 patch，缺少有效工具和生成模型成功证据。 | 未复刻，排除超越评估 |
| 24 小时值守 | 生产九项 cron；八项直接使用任务运行器，一项自定义晨报经兼容包装器转发。Dreaming 失败学习闭环尚缺效果验证。 | ⚠️ 部分（机制通，效果未验证） |
| 记忆质量审计 | 100/100 分。但这是指标，不是能力证明。未证明高分→高任务完成度。 | ❌ 未证明 |
| 条件主动性 | 12 次运行 0 触发。无法判断是"无问题"还是"没发现问题"。 | ❌ 未证明 |

**结论**：0 项有充分证据证明超越；图片生成未落地，其余机制已有不同强度的运行证据，不能用一条笼统状态概括。

验证方法、负责角色、时间与判据见 [能力验证计划](VALIDATION-PLAN.md)。

## 第十二轮复验口径

九项生产 cron 中 upkeep、heartbeat 路由到 Flash，其余七项路由到 ChatGPT。`intel-task.sh` 已通过 `exec run.sh "$@"` 转发，因此晨报仍使用新路由表并保留自定义提示词；直接改为不带提示词的 `run.sh morning_briefing` 会因缺少输入而失败。新运行器已有两种提供方的真实受控回复和进程退出证据，但 ChatGPT 任务在部署后的自然定时触发仍须单列观察，不能以受控探针替代长期可靠性。

HMC 的会话模型选择与模型配置、账号授权分别管理单会话下次请求和 Host 提供方配置，保留两个入口是明确的职责划分。真机 HTML 截图已有本地与 Linux 证据，Linux `/root/workspace/audit-evidence/round12-20261004/` 保存第十二轮无私有数据样本及 SHA-256 索引；普通文件不会增加目录的链接数，不能按链接数为 2 判断目录为空。后台提醒仍为用户启用、按会话、有时限的机制，不将其声明为全局默认值守。

## 基线快照

- Docs MD5：见 /root/intel/muse-agent-snapshots/docs.md5
- Skills MD5：见 /root/intel/muse-agent-snapshots/skills.md5
- 快照日期：2026-10-04
- 下次检查：每周一 06:45
