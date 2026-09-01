# deepseek-harness 官方插件评估报告

> 评估日期：2026-08-27
> 基座：`dsh-v0.1.1-rc.2`（commit `b150a551b8`）
> 运行时：Cordis 插件框架（vendored `vendor/cordis`）
> 评估视角：宏观数据回溯与预判 Agent（macro-analysis，运行于 `dsh --profile headless`，Windows）
> 关联文档：`sync-fork-upstream-SOP.md`（升级/rebase 流程）、`flywheel-SOP.md`（三飞轮）

---

## 0. 结论先行

1. **harness 已启动验证**：`dsh --profile web` 返回 `HTTP 200`（`127.0.0.1:3080`，PID 见 `outputs/dsh_web.log`）。headless 入口为 `node --expose-internals apps/cli/lib/bin.js --profile headless "<task>"`。
2. **官方插件体系 = 3 层 bundle**：
   - `base`（共享核心，声明 **~78 个 `@deepseek-ai/dsh-*` 插件**）
   - `headless`（在 base 上 +3：code-runtime / headless-startup / headless-runner）
   - `web-app`（在 base 上 + 浏览器 Host 层 + UI 插件 + agent-presets）
   - macro agent 走 **headless**，因此实际可用插件 = base 全集（受平台开关约束）+ headless 3 个。
3. **对 macro agent 最关键、默认可用的官方插件**：`tool-pwsh`、`tool-fs`、`tool-fs-search`、`tool-web`、`skill`/`tool-skill`、`subagent`/`tool-subagent`/`tool-workflow`、`tool-ralph`、`tool-str-replace-editor`、`tool-todo`、`compaction-basic`/`token-meter`/`tool-result-pruner`/`session-checkpoint-policy`、`code-runtime`。
4. **两个关键发现（影响 macro agent 落地）**：
   - ⚠️ **Windows 下 `tool-bash` 被禁用、`tool-pwsh` 启用**（`base/cordis.patch.yml`：`tool-bash disabled: process.platform==='win32'`）。macro 的 Python 脚本必须经 **PowerShell** 调用，与现有每日自动化（venv python）一致；若 agent 试图用 bash 会失败。
   - ⚠️ **`dsh-mcp-client` 与 `tool-cordis` 默认不加载**（仅出现在 `examples/`，不在任何 bundle patch）。若要接外部 MCP 数据源（Tushare / 东财 / 自定义采集器）或运行时自改插件，必须显式接线（自定义 cordis patch 或 `dsh plugin add`）。

---

## 1. 官方插件全景（headless 实际可用，按类别）

| 类别 | 插件 id（@package） | 作用 | headless 可用性 |
|---|---|---|---|
| 运行时核心 | `timer`(@cordis-plugin-timer)、`llm`(@dsh-llm)、`session`(@dsh-session)、`typert-registry/loader/gateway`(@dsh-typert-*)、`subprocess`(@dsh-subprocess-local) | 调度/LLM/会话/类型/子进程 | ✅ |
| 会话持久化 | `session-persistence-jsonl`、`attachment-local`、`session-projection`、`session-title`、`session-title-llm`、`user-questions` | 会话落盘/附件/标题 | ✅ |
| 凭据/模型 | `credentials`(@dsh-credentials-local)、`settings`(@dsh-settings-file)、`llm-deepseek`(@dsh-llm-deepseek)、`llm-pi-ai`( dormant)、`agent-default-model`、`llm-retry` | key 解析/多 provider/默认模型/重试 | ✅ |
| 沙箱/权限 | `sandbox`、`sandbox-policy`、`approval`、`permission`、`shell-env`、`fs-sandbox` | 文件/命令边界与审批 | ✅ |
| **Shell 工具** | `tool-pwsh`(@dsh-tool-pwsh) **启用**；`tool-bash`(@dsh-tool-bash) **Windows 禁用** | 执行 shell 命令 | ⚠️ 仅 pwsh |
| Shell 沙箱 | `pwsh-sandbox` **启用**；`bash-sandbox` 禁用(win32) | PowerShell 执行隔离 | ✅(pwsh) |
| 文件系统 | `tool-fs`、`tool-fs-search`、`fs-observation-policy` | 读写/搜索/监听 | ✅ |
| **技能 Skill** | `skill`、`skill-filesystem`、`tool-skill`；`skill-badge`(disabled) | Markdown 指令技能注册/调用 | ✅ |
| 命令/目标 | `commands`、`command-feedback`、`goal`、`goal-round-driver`、`command-goal`、`plan-mode`、`command-compact` | 斜杠命令/目标驱动/计划模式 | ✅ |
| 上下文管理 | `token-meter`、`compaction-basic`、`tool-result-pruner`、`spill-local`/`spill-policy`、`session-checkpoint-policy` | token/压缩/落盘/检查点 | ✅ |
| **子代理/工作流** | `subagent`、`subagent-spawn/fork-in-process`、`tool-subagent*`、`workflow-worker-thread`、`tool-workflow`、`timeout-policy` | 并行/后台子代理、工作流 | ✅ |
| 任务/迭代 | `tool-todo`、`tool-goal`、`tool-ralph`(maxRounds 64)、`tool-str-replace-editor`、`repeat-tool-reminder` | 待办/目标/自迭代/编辑 | ✅ |
| **Web/搜索** | `web`、`web-search-deepseek`、`tool-web`(fetch off, search on) | 内置 DeepSeek 联网搜索 | ✅ |
| 注册表/编排 | `tools`、`system-prompt`、`agent-loop`、`agent`、`agent-instructions` | 工具树/系统提示/agent 循环 | ✅ |
| headless 专属 | `code-runtime`(@dsh-code-runtime-worker-thread)、`headless-startup`、`headless-runner` | 一次性任务执行 | ✅(仅 headless) |

> web-app 额外层（macro 不依赖，仅供参考）：`storage*`、`workspace`、`session-log-download`、`directory-picker`、`plugin-inventory`、`api-gateway`、`webserver`/`web-runtime`、`modules`/`connection`/`client-runtime`、`ui-*(浏览器 UI)`、`agent-presets`(default `standard`)。

---

## 2. macro agent 能力映射表

| macro 场景 | 对应官方插件 | 说明 / 收益 |
|---|---|---|
| 每日运行采集脚本（Python venv） | `tool-pwsh` + `subprocess` | **必须经 PowerShell 调用**；长任务可 `run_in_background` |
| 读写 vintage 数据 / 清洗库 / 输出报告 | `tool-fs` + `tool-fs-search` | 受 `sandbox-policy` 约束（建议 `danger-full-access` 跑采集） |
| 抓取 NBS/BLS 公告、修订说明 | `tool-web`（DeepSeek 搜索） | 辅助核实修订；主数据仍走 API（nbsc / bls 直连） |
| 把「vintage/月份正则/源分级」等经验固化 | `skill` + `tool-skill` | 将领域知识封装为官方 Skill（Markdown），保证 agent 一致调用 |
| 30+ 指标并行采集 | `subagent` + `tool-subagent` + `tool-workflow` | 按指标分桶派生子代理，缩短每日窗口 |
| 采集→校验→清洗→分析 编排 | `tool-workflow` + `goal`/`tool-goal` | 多步 pipeline，失败可定位步骤 |
| 校验/分析脚本自迭代优化 | `tool-ralph`(fresh-agent, 64 轮) | 对固定脚本做迭代精修（如 clean 规则） |
| 长任务上下文膨胀 | `compaction-basic` + `tool-result-pruner` + `session-checkpoint-policy` | 防 OOM / 中途崩溃丢进度（macro 采集输出大） |
| 跨会话复用状态 | `session-persistence-jsonl` + `attachment-local` | 运行记录/图片落盘 |

---

## 3. 关键发现与建议

### 3.1 Windows Shell 约束（已验证，最高优先级）
- base 层 `tool-bash` 在 `win32` 下 `disabled: true`，`tool-pwsh` 反之启用。
- **影响**：macro agent 在 Windows 上只能用 PowerShell 跑脚本。现有每日自动化已是 `python` 直调（PowerShell 宿主），保持一致即可；若未来把采集逻辑交给 agent 自由发挥，必须确保它走 `pwsh` 而非 `bash`。
- 沙箱侧：`pwsh-sandbox` 启用、`bash-sandbox` 禁用 —— 命令超时默认 60s（`pwsh-sandbox.timeoutMs`），大脚本需显式延长或关沙箱。

### 3.2 MCP 客户端默认不加载（重要）
- `packages/mcp/mcp-client`（`@deepseek-ai/dsh-mcp-client`）存在且功能完整（stdio 传输、断线重连、tool 桥接），但**仅被 `examples/mcp-memory/` 演示引用，未进入任何默认 bundle**。
- **对 macro 的意义**：当前 macro 数据全靠 Python 直连 API（nbsc/BLS/东财），不依赖 MCP。若要让 agent 直接消费 WorkBuddy 侧已连接的连接器（Tushare、东财妙想等）或自定义 MCP 采集器，需：
  1. 写一份 `cordis.patch.yml` 注入 `@deepseek-ai/dsh-mcp-client`（参考 `examples/mcp-memory/memorix.cordis.yml` 写法）；
  2. 或 `dsh plugin --profile headless add @deepseek-ai/dsh-mcp-client`（转发给 pnpm 安装后自动入层栈）。
- 注意：MCP 是「外部数据源」通道，与「数据质量飞轮」正交；引入前仍须遵守红线——**第三方源不得冒充官方一手**。

### 3.3 领域知识 Skill 化（推荐）
- 官方 `skill` 体系（Markdown `SKILL.md` + `tool-skill`）适合把 macro 的硬知识固化：vintage 防前视偏差、月份正则 `(1[0-2]|0?[1-9])` 陷阱、NBS easyquery 废弃→nbsc、BLS 修订捕获、源可信度分级。封装后 agent 调用更稳定，也便于「三飞轮」里的校验智慧复用。
- 落地位置：`.agents/skills/<name>/SKILL.md`（官方约定）或 `scripts/macro-analysis/` 内随仓库。

### 3.4 编排与健壮性
- 采集窗口长、指标多 → 优先用 `subagent`/`tool-workflow` 并行；`tool-result-pruner`(阈值 8192 字符) + `session-checkpoint-policy` 防中途丢失。
- `tool-ralph` 适合对「clean 规则 / verify 断言」做离线精修，但需固定脚本入口。

### 3.5 Extensions / 运行时自改（前沿，默认关闭）
- `tool-cordis`（agent 在内存中 `cordis_define/run/undefine` 动态插件）仅 `examples/web-cordis`、`examples/acp-agent` 演示，**默认不加载**。
- 暂不推荐 macro 启用（复杂度高、重启即失、与「数据层只增不修」的稳重基调不符）；列为「可选增强」。

---

## 4. 建议下一步（待用户拍板）

1. **保持现状**：macro 继续用 Python 直连 + `tool-pwsh` 调度，无需改架构。
2. **（可选）Skill 化**：把 3.3 的硬知识封装 1~2 个 Skill，提升 agent 一致性。
3. **（可选）MCP 接线**：若要用外部数据源，按 3.2 加 `@deepseek-ai/dsh-mcp-client` 自定义 patch（需评估是否破坏「官方一手」红线）。
4. **（可选）并行编排**：将 30+ 指标采集改为 `subagent`/`tool-workflow` 分桶，验证稳定性后再固化。

---

## 附：如何自己复现本次核查

```bash
cd my-deepseek-harness/deepseek-harness
# 启动（web 验证）
node --expose-internals apps/cli/lib/bin.js --profile web --port 3080
# 官方插件清单（权威来源）
less packages/bundle/base/cordis.patch.yml          # ~78 核心插件
less packages/bundle/headless/cordis.patch.yml       # +3
less packages/bundle/web-app/cordis.patch.yml        # +浏览器/Host 层
# 默认未加载的「前沿」插件（仅 examples）
ls examples/mcp-memory examples/web-cordis examples/acp-agent
```
