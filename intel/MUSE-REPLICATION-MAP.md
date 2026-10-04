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
| 图片生成 | 未复刻 | 未发现插件、依赖或 profile 挂载；已有测试没有 image_gen 工具。维持 [REPLICATION.md](REPLICATION.md) A4 范围，不把手写 SVG/PNG 当作生成模型验收。 |
| Gmail、Google Calendar 等外部连接器 | ❌ 维持不复刻 | ChatGPT 无 Google OAuth；Tomas 09-27 产品决策不变 |
| 真实 MCP server | ❌ 维持不复刻 | ChatGPT 不提供 MCP；产品决策不变 |
| Apple Healthkit、Spotify 等 | ❌ 维持不复刻 | 需各自 OAuth；ChatGPT 无帮助 |

ChatGPT 模型接入不自动提供图片生成工具，也不改变外部连接器的产品范围。未来若启用图片生成，须同时验证工具注册、profile 挂载、真实模型生成和手机展示，才可变更本表状态。

## 超越项评估（按能力/任务完成度，2026-10-04 12:0x）

> 标准：有功能≠超越。需证据证明能力更强或任务完成度更高。

| 候选 | 证据 | 结论 |
|---|---|---|
| 多模型路由 | 静态任务表。未知 test_* 任务默认 flash，不能据此判断其它提供方失效。生产两日聚合有 deepseek-official/deepseek-flash 与 openai-codex/gpt-5.5 的请求及响应用量。 | 多提供方响应有证据；任务路由质量、回退归因与超越仍未证明 |
| 图片生成 | 未安装、未挂载，缺少生成模型成功证据。 | 未复刻，排除超越评估 |
| 24 小时值守 | 10 cron 运行中。晨报 1 次成功。Dreaming 学习闭环未遇失败，无验证。 | ⚠️ 部分（机制通，效果未验证） |
| 记忆质量审计 | 100/100 分。但这是指标，不是能力证明。未证明高分→高任务完成度。 | ❌ 未证明 |
| 条件主动性 | 12 次运行 0 触发。无法判断是"无问题"还是"没发现问题"。 | ❌ 未证明 |

**结论**：0 项有充分证据证明超越；图片生成未落地，其余机制已有不同强度的运行证据，不能用一条笼统状态概括。

验证方法、负责角色、时间与判据见 [能力验证计划](VALIDATION-PLAN.md)。

## 基线快照

- Docs MD5：见 /root/intel/muse-agent-snapshots/docs.md5
- Skills MD5：见 /root/intel/muse-agent-snapshots/skills.md5
- 快照日期：2026-10-04
- 下次检查：每周一 06:45
