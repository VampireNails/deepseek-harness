# Muse → dsh-muse 能力复刻映射（2026-10-04）

> 基线：Muse 智能体（非模型）2026-10-04 快照
> 目标：一切为了让 dsh-muse 比 Muse 更智能
> 评估标准：按能力、任务完成度，不按"有没有功能"

## 已复刻（19 插件）

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
| 主动检查 | proactive-check.sh | ✅ 条件触发 |
| 图片生成 | dsh-image-gen v0.4.1 | ✅ ChatGPT 订阅（2026-10-04 新增） |

## 未复刻（重估后，2026-10-04 11:5x）

| Muse 能力 | 状态 | 重估结论 |
|---|---|---|
| Gmail、Google Calendar 等外部连接器 | ❌ 维持不复刻 | ChatGPT 无 Google OAuth；Tomas 09-27 产品决策不变 |
| 真实 MCP server | ❌ 维持不复刻 | ChatGPT 不提供 MCP；产品决策不变 |
| Apple Healthkit、Spotify 等 | ❌ 维持不复刻 | 需各自 OAuth；ChatGPT 无帮助 |

**重估说明**：4 项原未复刻中，仅图片生成因 ChatGPT 技术解冻而复刻（09-27 是技术原因删除）。其余 3 项是 Tomas 的产品决策（不在复刻范围），ChatGPT 接入不改变该决策。

## 超越项评估（按能力/任务完成度，2026-10-04 12:0x）

> 标准：有功能≠超越。需证据证明能力更强或任务完成度更高。

| 候选 | 证据 | 结论 |
|---|---|---|
| 多模型路由 | 7 任务配置，1 次运行成功（晨报）。路由是静态表，非智能选择。无对比数据。 | ❌ 未证明 |
| 图片生成 | 刚安装，0 次测试。 | ❌ 未证明 |
| 24 小时值守 | 10 cron 运行中。晨报 1 次成功。Dreaming 学习闭环未遇失败，无验证。 | ⚠️ 部分（机制通，效果未验证） |
| 记忆质量审计 | 100/100 分。但这是指标，不是能力证明。未证明高分→高任务完成度。 | ❌ 未证明 |
| 条件主动性 | 12 次运行 0 触发。无法判断是"无问题"还是"没发现问题"。 | ❌ 未证明 |

**结论**：5 项候选中，0 项有充分证据证明超越。均处于"有机制、无实证"阶段。

**要证明超越，需要**：
1. 同一任务，dsh-muse 和 Muse 分别做，对比完成度/质量
2. 长期任务成功率统计（如 30 天 cron 成功率）
3. 用户盲测：分不清哪边做的，或明确偏好 dsh-muse

## 基线快照

- Docs MD5：见 /root/intel/muse-agent-snapshots/docs.md5
- Skills MD5：见 /root/intel/muse-agent-snapshots/skills.md5
- 快照日期：2026-10-04
- 下次检查：每周一 06:45
