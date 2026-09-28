核实完毕。以下是《23 项能力对照表》全文（全部基于服务器上已安装的 `@deepseek-ai/dsh@0.1.7-rc.1` 实测：`package.json` description、README、lib 源码 grep；只读，未改动服务器任何状态）。

---

# 23 项能力对照表（复刻版 Python 栈 → dsh 0.1.7-rc.1）

核验日期：2026-09-28；核验对象：`/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/` 下全部包

| # | 复刻版能力 | dsh 对应包 | 是否有等价物 | 语义是否等价 | 缺口补在哪（核实依据） |
|---|---|---|---|---|---|
| 1 | agent loop + 工具调用 | `dsh-agent-loop`、`dsh-agent` | ✅ 有 | ✅ 等价 | —（`dsh-agent-loop` 的 package.json 写 "The concrete agent loop plugin for the DeepSeek Harness"；`dsh-agent` 写 "Agent interface, registry, initiator scope, and event vocabulary"） |
| 2 | 终端操作 `shell_exec` | `dsh-tool-bash` + `dsh-bash-local` + `dsh-bash-sandbox` | ✅ 有 | ✅ 等价（超集） | —（`dsh-tool-bash` 写 "Model-facing bash tool with optional generic background-job and sandbox-escalation support"；`dsh-bash-sandbox` 写 "confines every command via ctx.sandbox"——dsh 还多一层沙箱） |
| 3 | 文件读写 `file_read/write/append` | `dsh-tool-fs` + `dsh-tool-fs-search` + `dsh-tool-str-replace-editor` | ✅ 有 | ✅ 等价（超集） | —（`dsh-tool-fs` 写 "Model-facing filesystem tools (read, write, edit)"；`dsh-tool-fs-search` 带 glob/grep；`dsh-tool-str-replace-editor` 带字符串替换编辑） |
| 4 | 网页抓取 + 搜索 | `dsh-tool-web` + `dsh-web-fetch-http` + `dsh-web-search-deepseek` | ✅ 有 | ⚠️ 基本等价 | 搜索后端只有 DeepSeek 一家。核实：`dsh-tool-web` 写 "Model-facing web tools (web_search, web_fetch)"；但安装版里只有 `dsh-web-search-deepseek`，没有 exa/perplexity 包（dsh.md 评估说的 3 后端在此安装版不存在）。如需多搜索后端再补 provider 包 |
| 5 | 多步浏览器（11 个 `browser_*` 工具） | 无（仅 `dsh-client-ui-sidebar-browser`） | ❌ 无 | — | **挂载上游实验性 provider（非新写）**。核实：安装版 0.1.7-rc.1 的已装包里无模型工具包（`browser_snapshot`/`browser_click` 在全部包 lib 里 0 命中；`dsh-client-ui-sidebar-browser` 是右侧栏 UI 组件）——这是对 dsh.md 评估的修正；**但上游仓库 `packages/experimental/` 确有** `browser-use-playwright-mcp`、`browser-use-chrome-devtools-mcp`、`browser-use-stagehand-native` + `browser-use-runtime`（第二轮审计确认存在），默认未挂载。路线：评估挂载而非新写（与 §4 #5 一致）。 |
| 6 | 子智能体 `spawn_subagent` | `dsh-tool-subagent` + `dsh-subagent`（+ `dsh-subagent-*-in-process` 驱动） | ✅ 有 | ✅ 等价 | —（`dsh-tool-subagent` 写 "Model-facing subagent delegation tool over the ctx.subagents seam"；`dsh-subagent` 写 "Abstract subagent seam (ctx.subagents): named-provider registry"。注：dsh.md 说的 acp/claude-code/codex/dsh-sdk 四种后端包在安装版里不存在，只有 in-process 驱动——核心委派能力不受影响） |
| 7 | 语义记忆（向量 + `memory_search/read/write`） | 无 | ❌ 无 | — | **阶段 1 新写记忆包**。核实：`memory`/`embedding`/`vector`/`semantic` 在全部包的 package.json 里命中数为 0；`dsh-session-query-sqlite` 写的是 "Concrete ctx.sessionQuery backend with SQLite FTS5 search"——是词法全文检索，不是语义向量 |
| 8 | Artifacts v2（版本 + HTML 沙盒） | `dsh-tool-present` + `dsh-client-ui-deliverables`（仅 UI） | ⚠️ 部分 | ❌ 不等价 | 版本历史 + 沙盒渲染需新写。核实：`dsh-tool-present` README 写 "Use `present` to declare final files accessible through the Session filesystem…The tool records paths and optional descriptions without copying file contents"——只是交付声明；其 lib 无 version/revision 相关实现；`dsh-deliverables` 后端包不存在，只有 UI 包 |
| 9 | 审批卡 + 四级权限 | `dsh-user-approval` + `dsh-permission-presets` | ⚠️ 部分 | ❌ 不等价 | 四级分类 + 高危 shell 正则拦截需新写（可用 `tools/pre-execute` 缝，该事件存在，被 `dsh-bash-local` 等消费）。核实：`dsh-user-approval` 写 "one-shot permission decisions dispatched to composed answerers over the approval/request waterfall, fail-closed by default"——审批机制有；`dsh-permission-presets` 写 "sandbox-mode and approval-policy knobs"——是预设开关不是四级；lib 里无 READ/WRITE/BLOCKED 分级。注：安装版无 `interaction`/`guard` 包（dsh.md 用的另一套名字） |
| 10 | Hooks（webhook + inbox 轮询） | `dsh-webhook`（入站）+ `dsh-hook-protocol`/`dsh-hooks-claude-code`/`dsh-hooks-codex` | ⚠️ 部分 | ❌ 不等价 | inbox 轮询 + shell 动作需新写。核实：`dsh-webhook` README 写 "trusted external-event policies that create Workspace Sessions…Use it when a trusted rule must turn an external event into a new agent Session"——入站 webhook→建会话，接近复刻版 `POST /api/hooks/<name>` 触发 agent 的一半；`dsh-hook-protocol` 写 "Shared Claude Code / Codex hook wire protocol"——是外部 hook 配置桥接，不是 inbox 轮询 |
| 11 | 用户自建定时任务（中文时间解析） | `dsh-schedule` | ⚠️ 部分 | ❌ 不等价 | 真后台执行需用 `dsh-jobs` 另行承载。核实：`dsh-schedule` README 写 "Schedule lets you ask the model for durable reminders that return as ordinary follow-up messages in the same conversation…Delivery never uses email, SMS, push, or browser notifications"——是"提醒"，不是复刻版那种"到点跑 agent_loop 写日记"的后台任务 |
| 12 | Goals | `dsh-tool-goal` + `dsh-goal` + `dsh-goal-round-driver` + `dsh-command-goal` | ⚠️ 有但 | ❌ 不等价（任务书点名例） | 长期目标层需新写。核实：`dsh-tool-goal` 写 "Model-facing same-session goal tools"；`dsh-goal` 写 "Event-sourced same-session goal state and lifecycle service"——dsh 的 goal 是**同会话内**驱动多轮工作的；复刻版 goals 是跨会话长期目标 + 进展记录 + 简报接入，两者不是一回事 |
| 13 | Quiet-moment pass（对话后复盘） | 无（挂载点 `agent/turn-stopping` 存在） | ❌ 无 | — | **阶段 2 新写复盘包**。核实：`turn-stopping` 的消费者只有 `dsh-agent-loop`（发出方）、`dsh-hooks-claude-code`、`dsh-hooks-codex`、`dsh-scope`、`dsh-workspace-changes`（记文件 diff）——没有任何包实现"复盘该记住什么"的行为，挂载点是现成的，行为是空的 |
| 14 | 自我进化（upkeep/studying/idea_curation/dreaming/skill_review） | 无 | ❌ 无 | — | **阶段 3 新写**（用 `dsh-schedule` + `dsh-jobs` 承载）。核实：全部包 description 含 dream/upkeep/self-improvement/evol 为 0 |
| 15 | Heartbeat | 无 | ❌ 无 | — | **阶段 3 新写**（schedule 承载 + 每 3 项分块逻辑）。核实：同上，无对应包 |
| 16 | Feed 信息流 | 无 | ❌ 无 | — | **阶段 4 新写**。核实：全部包 description 含 feed（排除 feedback）为 0 |
| 17 | 多会话 Side chats | `dsh-session` + `dsh-session-query` | ✅ 有 | ✅ 等价（超集） | —（`dsh-session` 写 "Event-sourced session store for the DeepSeek Harness"；`dsh-session-query` 写 "Combined session query service contract with concrete reads, traces, and filters"——还带历史检索，复刻版没有） |
| 18 | 文件上传（50MB） | `dsh-attachment` + `dsh-attachment-local` | ✅ 有 | ✅ 等价 | —（`dsh-attachment` 写 "Durable immutable attachment storage seam"；`dsh-attachment-local` 写 "Private content-addressed DSH_HOME attachment storage"；50MB 系配置项非能力缺口） |
| 19 | MCP client | `dsh-mcp-client` + `dsh-mcp-resources` | ✅ 有 | ✅ 等价（超集） | —（`dsh-mcp-client` 写 "connects to MCP servers and registers their tools on ctx.tools"；还多 `dsh-mcp-resources` 资源读取，复刻版没有） |
| 20 | 推送 webhook（`notify.py`） | `dsh-webhook` | ⚠️ 有但 | ❌ 不等价（方向相反） | 出站推送需新写。核实：`dsh-webhook` 是**入站**（外部事件→创建会话，见 #10）；复刻版 `notify.py` 是**出站**（agent→向外部 URL POST）。方向完全相反，不能复用 |
| 21 | joblog（后台任务日志 + 告警 + 状态） | `dsh-jobs` + `dsh-jobs-local` + `dsh-tool-jobs` | ⚠️ 部分 | ❌ 不等价 | `jobs.log`/`job_alerts.md`/`job_runs.json` 三件套需新写。核实：`dsh-jobs` 写 "Background job registry (ctx.jobs)…shared ids, owner isolation, polling, cancellation, and completion listeners"——注册表机制有；但 `dsh-jobs`/`dsh-jobs-local` 的 lib 里无 log/alert/audit 相关实现 |
| 22 | 结构化用户画像 | 无（`dsh-persona` 是助手人设；`dsh-anonymous-user-id` 是匿名遥测 ID） | ❌ 无 | — | **阶段 4 新写**。核实：`dsh-persona` 写 "Composition-authored deployment persona section for the DeepSeek Harness"——是部署方写的**助手**人设 section，不是用户画像；`dsh-anonymous-user-id` 写 "Shared anonymous user identity for … telemetry and feedback correlation"——匿名 ID 与画像无关 |
| 23 | 晨间简报 | 无（可由 schedule + web_search + feed 组合实现） | ❌ 无 | — | **阶段 3/4 组合实现**，无现成包。核实：无对应包；能力可由已有件拼出，不属"缺口"，属"待组装" |

---

## 总结

### 5 项 dsh 完全没有（与 dsh.md 预期一致，逐项核实成立）
| # | 能力 | 核实 |
|---|---|---|
| 7 | 语义记忆 | 全部包 package.json 中 memory/embedding/vector/semantic 命中 0；唯一沾边的是 SQLite FTS5 词法检索 |
| 13 | Quiet-moment pass | `agent/turn-stopping` 挂载点存在且已有 4 个消费者，但无任何包实现复盘行为 |
| 14 | 自我进化循环 | 全部包 description 无 dream/upkeep/self-improvement 命中 |
| 16 | Feed | 全部包 description 含 feed（除 feedback）为 0 |
| 22 | 结构化用户画像 | `dsh-persona` 是助手人设、`dsh-anonymous-user-id` 是遥测匿名 ID，均非用户画像 |

### 3 项加固 dsh 没有等价物（全部需新写）
| # | 加固 | 核实 |
|---|---|---|
| 21 | joblog 三件套 | `dsh-jobs` 只有注册表（ids/owner/polling/cancellation/listeners），lib 无 log/alert/audit |
| 7（加固项） | 步骤用尽不判死（`partial=True` 最终综合） | `dsh-agent-loop` 的 lib 里无 maxSteps/step-limit/partial 相关实现（仅 "exhaustiveness"/"exhausts" 各 1 处，指迭代器耗尽语义） |
| 8（加固项） | Heartbeat 长清单分块 | Heartbeat 能力本身就不存在（见 #15），分块逻辑自然也没有 |

### 额外发现：3 项 dsh.md 评估有误 / 安装版不存在（重要修正）
1. **浏览器工具**：dsh.md 称有 `browser-use` 包——安装版 0.1.7-rc.1 里**没有**，11 个 `browser_*` 工具无等价物，需新写（#5）。
2. **web 搜索后端**：dsh.md 称有 deepseek/exa/perplexity 3 个后端——安装版只有 `dsh-web-search-deepseek`（#4）。
3. **subagent 后端**：dsh.md 称有 acp/claude-code/codex/dsh-sdk 4 种后端——安装版只有 in-process 驱动（#6，核心委派能力不受影响）。

### 语义不等价但名字像的（任务书 §4.3 点名的坑，逐项坐实）
- #12 Goals：dsh 是**同会话内**多轮工作驱动，复刻版是**跨会话**长期目标——"Model-facing same-session goal tools" 为证。
- #20 推送：dsh-webhook 是**入站**（外部→建会话），复刻版 notify 是**出站**（agent→POST 外部）——方向相反。
- #11 定时：dsh-schedule 是**提醒**（回到同一会话的消息），复刻版 cron 是**后台执行**（跑 agent_loop 写日记）——"Delivery never uses email, SMS, push" 为证。
- #9 权限：dsh 只有审批瀑布 + 预设开关，无复刻版 READ/WRITE/APPROVAL/BLOCKED 四级。
- #8 产物：dsh 只有交付声明（记路径不拷贝），无版本历史、无 HTML 沙盒。
- #10 Hooks：dsh 的 hooks 是 Claude Code/Codex 配置桥接 + 入站建会话，无 inbox 轮询。

### 等价（可直接用，共 8 项）
#1 agent loop、#2 shell（dsh 多沙箱）、#3 文件（dsh 多搜索/替换）、#6 子智能体、#17 会话（dsh 多历史检索）、#18 上传、#19 MCP（dsh 多 resources）。#4 基本等价（单搜索后端）。

**最终口径**（2026-09-28 修订：以 `intel/REPLICATION.md` §2 的 23 行明细表为唯一权威口径，本矩阵不再另立分解）：
23 项 = 8 dsh 原生直接用（#1/2/3/4/6/17/18/19）+ 8 新写插件（#7/13/14/15/16/21/22/23）
+ 6 未排期（#5/8/9/10/11/12，任务书范围外）+ 1 不复刻（#20，Tomas 决策）。
本矩阵的逐项核实结论（等价/部分/无）仍然有效，作为 §2 表"去向"列的核实依据。