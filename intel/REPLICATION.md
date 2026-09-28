# dsh-muse 复刻总览（2026-09-28 修订版）

> 一份文档讲清四件事：
> ① dsh-muse 是否完成复刻（结论先行），
> ② 完整复刻清单（23 项 Python 能力 → dsh 的去向，**以 §2 表为唯一权威口径**），
> ③ DeepSeek Harness 改造汇总（Python 单体 → dsh 插件架构），
> ④ 未排期 6 项的升级提议详细评估（P0/P1/P2，2026-09-28 新增）。
>
> 修订：2026-09-28，HEAD `b8a97c0ca`。单元测试 21 用例 21 通过。第三方审计（有条件通过）后修正 P1 项。

---

## 一、结论：dsh-muse 是否完成复刻

**智能层复刻已完成。23 项 Python 能力未全部搬运——这是设计使然，不是缺口。**

dsh-muse 的任务书只定义了 5 个阶段，目标是把 Python 栈里 **dsh 没有的那部分"智能"** 搬过去：

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 环境验证 | ✅ 完成 |
| 1 | 记忆（memory_write/read/search） | ✅ 完成 |
| 2 | quiet-moment 复盘 | ✅ 完成（含真后台修复） |
| 3 | 自我进化 + Heartbeat + joblog | ✅ 完成 |
| 4 | Feed + 用户画像 + 晨间简报 | ✅ 完成 |

评估修复（第三方评估，两轮）：

| 问题 | 状态 |
|---|---|
| 单元测试（21 用例全过）+ 集成测试（3/3） | ✅ 完成 |
| quiet 真后台（复刻 Python daemon 语义） | ✅ 完成 |
| 迁移脚本（migrate.sh + rollback.sh） | ✅ 完成（含 spaceCjk 对齐修复） |
| 文档订正 + README npm ci | ✅ 完成 |
| run-integration.sh DSH_HOME 隔离 | ✅ 完成 |
| bootstrap 可复现脚本 | ✅ 完成（`intel/bootstrap.sh`：从零 clone 重建三 profile 已验证） |
| 24h 空转存活监控 | 🔄 运行中（2026-09-29 14:34 出结果；仅进程存活+RSS，非负载稳定性） |
| HMC 端到端 | ✅ API 级通过（TLS→401→session/create→session/prompt 全绿，证据 `intel/hmc-e2e-evidence-2026-09-28.log`）；手机真机已直连 100.73.148.102:43197，Tomas 确认"已连上" |

**23 项的去向（由 §2 表统计得出，唯一口径）：**

| 去向 | 数量 | 说明 |
|---|---|---|
| dsh 原生已有 | 8 | agent loop、shell、文件、子智能体、会话、上传、MCP、网页搜索——直接用 |
| 新写插件 | 8 | 记忆、quiet、进化、Heartbeat、Feed、joblog、画像、晨报——dsh-muse 补的 |
| 未排期 | 6 | 浏览器工具、Artifacts v2、审批四级、Hooks、Goals、用户 cron——任务书范围外，见 §四 评估 |
| 不复刻 | 1 | 出站推送（#20）——Tomas 决定保留通道不推进 |

**另有 7 项 Python 能力不在 23 项对比范围内**（dsh-muse 任务书从未纳入，Tomas 已决策不复刻）：

| # | 能力 | 不复刻依据 |
|---|---|---|
| A1 | Gmail 等外部服务连接器 | Tomas 2026-09-27 明确决定不在复刻范围 |
| A2 | 人物/群组档案 | 个人使用，Tomas 明确不要 |
| A3 | onboarding 教学流程 | 一次性流程，无需复刻 |
| A4 | 图片生成（ChatGPT 路线） | Cloudflare 硬拦截；Tomas 决定不复刻，media.py 已删 |
| A5 | TTS | 同上，占位代码已删 |
| A6 | 预订/快递追踪 | 低频能力，未排期 |
| A7 | Tailscale Serve 跨重启验收 | 运维事项，非能力复刻 |

---

## 二、完整复刻清单（23 项 → dsh 去向）

"去向"列：**dsh 原生** = 直接用；**新写插件** = dsh-muse 补的；**不复刻** = Tomas 决策；**未排期** = 任务书范围外（评估见 §四）。

| # | Python 能力 | 去向 | dsh-muse 实现 | 验证 |
|---|---|---|---|---|
| 1 | agent loop + 工具调用 | dsh 原生 | `dsh-agent-loop`（dsh 原生默认 agent loop） | 阶段 0 |
| 2 | 终端 `shell_exec` | dsh 原生 | `dsh-tool-bash`（profile 挂载 `bash-sandbox` 变体时多一层沙箱；待 V1 确认实际挂载） | 阶段 0 |
| 3 | 文件读写 | dsh 原生 | `dsh-tool-fs`（多搜索/替换，超集） | 阶段 0 |
| 4 | 网页抓取 + 搜索 | dsh 原生 | `dsh-tool-web`（Exa/Perplexity/DeepSeek 三后端可选；dsh-muse 部署选用 DeepSeek） | 阶段 0 |
| 5 | 多步浏览器（11 工具） | 未排期 | dsh 安装版无等价物；任务书未要求 | §四 P1 |
| 6 | 子智能体 | dsh 原生 | `dsh-tool-subagent`（in-process 驱动） | 阶段 2 真测 |
| 7 | 语义记忆 | 新写插件 | `dsh-intelligence-memory`（SQLite FTS5 + BM25；分词器待 V2 披露；向量方案因 1GB 内存否决） | 阶段 1 |
| 8 | Artifacts v2 | 未排期 | dsh 只有交付声明，无版本/沙盒；任务书未要求 | §四 P2 |
| 9 | 审批卡 + 四级权限 | 未排期 | dsh 有审批瀑布，无四级；任务书未要求 | §四 P1 |
| 10 | Hooks | 未排期 | dsh 有入站 webhook，无 inbox 轮询；任务书未要求 | §四 P2 |
| 11 | 用户自建 cron | 未排期 | dsh-schedule 是会话绑定的 reminder 交付，非脱离会话的 headless 执行；任务书未要求 | §四 P0 |
| 12 | Goals 长期目标 | 未排期 | dsh goal 是同会话驱动，语义不同；任务书未要求 | §四 P2 |
| 13 | Quiet-moment | 新写插件 | `dsh-intelligence-quiet`（turn-stopping + 真后台） | 阶段 2 + 真后台修复 |
| 14 | 自我进化（5 任务） | 新写插件 | `dsh-intelligence-evolution`（cron + headless profile） | 阶段 3 |
| 15 | Heartbeat | 新写插件 | 同上（`heartbeat_check` + 3 项分块） | 阶段 3 |
| 16 | Feed | 新写插件 | `dsh-intelligence-feed`（`feed_post`/`feed_read`） | 阶段 4 |
| 17 | 多会话 | dsh 原生 | `dsh-session`（多历史检索，超集） | 阶段 0 |
| 18 | 文件上传 | dsh 原生 | `dsh-attachment`（仅支持图片格式；Python 通用文件上传口径有收窄，待披露） | 阶段 0 |
| 19 | MCP client | dsh 原生 | `dsh-mcp-client`（多 resources，超集） | 阶段 0 |
| 20 | 推送 webhook | 不复刻 | 出站推送；Tomas 决定保留通道不推进 | 2026-09-27 |
| 21 | joblog 三件套 | 新写插件 | `dsh-intelligence-joblog`（jobs.log/job_runs.json/job_alerts.md） | 阶段 3 |
| 22 | 结构化用户画像 | 新写插件 | `dsh-intelligence-profile`（仅 Tomas 本人，step 1 注入） | 阶段 4 |
| 23 | 晨间简报 | 新写插件 | cron 08:00（web_search + 画像 + 记忆 → Feed），未激活 | 阶段 4 |

**统计**：8 dsh 原生 + 8 新写插件 + 6 未排期 + 1 不复刻 = 23 ✅

**新写插件汇总**（6 个，全部在 `intel-plugins/`，树外独立 npm 包）：

| 插件 | 工具/能力 | 数据位置 |
|---|---|---|
| dsh-intelligence-memory | `memory_write`/`memory_read`/`memory_search`，step 1 自动召回 | `~/.dsh/intel-memory/memory.db` |
| dsh-intelligence-quiet | `turn-stopping` 监听，真后台复盘（fire-and-forget） | `~/.dsh/intel-quiet/` |
| dsh-intelligence-evolution | `heartbeat_check` + 5 进化任务 prompt | `~/.dsh/intel-evolution/` |
| dsh-intelligence-joblog | `joblog_*`，三件套 | `~/.dsh/intel-joblog/` |
| dsh-intelligence-feed | `feed_post`/`feed_read` | `~/.dsh/intel-feed/feed.json` |
| dsh-intelligence-profile | `user_profile_read`/`user_profile_update`，step 1 注入 | `~/.dsh/intel-profile/` |

---

## 三、DeepSeek Harness 改造汇总

### 3.1 架构范式转移：Python 单体 → dsh 插件

| 维度 | Python 栈（/opt/deepseek-harness） | dsh-muse（intel 分支） |
|---|---|---|
| Agent 骨架 | 自研 `agent/loop.py`（约千行） | dsh 原生 `dsh-agent-loop`（零代码） |
| 工具系统 | `agent/tools.py` 手工注册 | dsh 原生工具包（bash/fs/web/subagent…） |
| 插件机制 | 无（改代码即改核心） | Cordis 插件，`ctx.effect` 注册，卸载可回卷 |
| 定时任务 | APScheduler 进程内线程 | 系统 cron + `dsh --profile evolve` headless（架构选型：脱离 Host 进程独立执行、故障隔离；dsh-schedule 为会话绑定的 reminder 交付，见第三方审计） |
| 会话存储 | 自研 `sessions.py` | dsh 原生 event-sourced session |
| 智能层 | 与骨架耦合在同一进程 | 6 个树外独立插件，可单独装卸 |
| 语言栈 | Python 3.10 | Node.js（dsh 原生）；最终零 Python 依赖 |

**核心洞察**（2026-09-28 决策）：复刻版 Python 栈把 dsh 的活重做了一遍——两个平行大脑。HMC 只认 dsh 事件方言是决定性约束，智能层必须长在 dsh 上，而不是另起一套。

### 3.2 分阶段改造记录

**阶段 0：环境验证**（2026-09-28）
- dsh 0.1.7-rc.1（46a7f68b0）在 1GB 机器上内存无忧：web 空载 144MB，单回合峰值 139MB
- 踩坑：`pnpm link` 不装树外插件传递依赖——插件包须自带 `node_modules`
- 踩坑：`@deepseek-ai/*` 必须 pin `0.1.7-rc.1`，npm 默认 `0.0.1-rc.1` 导致 `unknown tool`

**阶段 1：记忆**（2026-09-28）
- SQLite FTS5 + BM25，CJK 空格化（`spaceCjk`：只在汉字两侧加空格，拉丁词保持完整）
- 向量方案（ONNX bge-small-zh-v1.5，同进程 374MB）因内存预算否决
- 检索契约可替换（`add/get/search/close`），以后内存宽裕可换向量

**阶段 2：quiet-moment**（2026-09-28）
- 初版在 `turn-stopping` 中同步等待，最长阻塞回合 90 秒
- 评估后改为真后台：handler 不 await，fire-and-forget，复刻 Python daemon 线程语义
- 5 分钟冷却 + SHA-256 去重 + `pendingHash` 防并发 + 子智能体仅 `memory_write` + maxDepth 1
- 超时可配（`reflectTimeoutMs` 默认 90s）；`start()` 失败清 pending，不静默丢数据

**阶段 3：进化 + Heartbeat + joblog**（2026-09-28）
- 弃用 dsh-schedule（语义是"提醒"非"后台执行"），改用系统 cron + headless profile
- 5 个进化任务：memory upkeep、studying、idea curation、dreaming、skill review
- joblog 三件套：`jobs.log` + `job_runs.json` + `job_alerts.md`
- cron 未激活（Python 生产仍在跑，避免双跑烧 token）

**阶段 4：Feed + 画像 + 晨报**（2026-09-28）
- Feed：新在前、最多 100 条、ID f1/f2…（与 Python 格式兼容，迁移可直接合并）
- 画像：仅 Tomas 本人，step 1 自动注入（复刻 Python dreaming 每晚更新机制）
- 晨报：08:00 cron，web_search + 画像 + 记忆 → 中文简报 → Feed（未激活）

**评估修复**（2026-09-28，第三方两轮评估）
- 单元测试 21 用例全过；集成测试 3/3（`run-integration.sh`，DSH_HOME 隔离）
- quiet 真后台（3 秒 vs 90 秒）；超时可配；pending→成功状态机
- `migrate.sh`（备份→31 条记忆→feed 合并→画像→存档）+ `rollback.sh`；spaceCjk 对齐；created_at 回填
- 文档订正 + README `npm ci`
- 24h 空转存活监控（运行中；负载版 stability-load.sh 已就绪，待下轮启用）+ HMC 端到端 API 级验证（脚本入库）

### 3.3 关键决策记录

| 决策 | 选项 | 依据 |
|---|---|---|
| quiet 后台机制 | 真后台（fire-and-forget），不用 ctx.jobs | 复刻 Python daemon 线程语义；Tomas 拍板 |
| 真后台适用边界 | **仅常驻 profile（web-intel）**；headless 不得依赖 | headless 退出会取消后台子智能体；evolve 不含 quiet |
| 定时任务承载 | 系统 cron + headless 为主 | 需脱离会话独立执行、故障隔离；dsh-schedule 为会话绑定的 reminder 交付，不适用后台任务（第三方审计已核验） |
| 手机端可见性 | A：隐形基础设施，不做 followup 弹窗 | 复刻 Python Muse 方式；Tomas 拍板 |
| 记忆检索 | FTS5/BM25，不用向量 | 1GB 内存；ONNX 同进程 374MB 太贵 |
| HMC 客户端 | 不修改，只读 + PR | Tomas 指示 |
| 生产切换 | 未切；Python 仍是生产，dsh 在 web-intel 隔离验证 | Tomas 未批准切换 |

### 3.4 生产状态（2026-09-28）

- **生产**：Python `deepseek-harness.service`（:8080），Tailscale Serve 指向它
- **dsh**：`web-intel` 隔离 profile（:3080，24h 空转存活监控中）；`evolve` 跑定时任务（cron 未激活）；`hmc-test` 跑 HMC 服务验证（:43197）
- **HMC 手机**：已直连 100.73.148.102:43197 配对成功（Tomas 2026-09-28 确认"已连上"）
- **Git**：`intel` 分支已 push，`master` 跟踪上游
- **每周养成检查**：已改名"dsh-muse 每周养成检查"（每周一 09:15，覆盖 dsh-muse + Python 双系统）
- **待 Tomas**：生产切换批准；cron 激活批准；§四 P0（用户自建 cron）开工批准

---

## 四、未排期 6 项升级提议评估（2026-09-28）

评估方法：逐项核对 Python 源码实现 → dsh 原生差距 → dsh-muse 实现路线 → 对 Tomas 的价值 → 成本/风险 → 优先级。

### P0：#11 用户自建 cron（最先做）

- **Python 实现**：`agent/custom_cron.py`（229 行）。`cron_create/list/delete` 三个工具；中文时间解析（"每天9点"→cron 表达式）；APScheduler 进程内调度；上限 20 个任务。
- **dsh 原生差距**：dsh-schedule 支持 cron/daily/weekly、任务管理工具、cold session 恢复，但为**会话绑定的 reminder 交付**，非脱离会话的 headless 后台执行（第三方审计已核验上游 `packages/schedule/` 代码）。
- **dsh-muse 路线**（第三方审计建议拆两层）：
  - (a) 用户 cron 的**触发与交付**：优先评估复用 dsh-schedule 原生（省掉调度器代码，只需中文时间解析约 200 行 JS）
  - (b) 需脱离会话的后台任务（如进化、joblog）：保留系统 cron + `dsh --profile evolve` headless
- **价值**：高。"电脑级智能体"的用户可见核心能力——Tomas 在手机上一句话就能建定时任务。晨间简报（#23）已写好但 cron 未激活，用户自建 cron 是让 Tomas 自己能下指令的那一环。
- **成本/风险**：中低。中文时间解析两种路线都需要；dsh-schedule 复用进一步降低实现成本。

### P1：#9 审批卡 + 四级权限（分两层）

- **Python 实现**：`agent/permissions.py` + `agent/approvals.py`。四级：READ（自动放行）/ WRITE（默认放行+记日志）/ APPROVAL（弹卡等确认，主循环现场存 `pending_runs`，30 分钟过期）/ BLOCKED（高危 shell 正则直接拒：`rm -rf /`、fork 炸弹、写裸设备等）。
- **dsh 原生差距**：dsh 有审批瀑布，但无分级、无 BLOCKED 硬拦截。
- **dsh-muse 路线**（拆两层，风险不同）：
  - **BLOCKED 层（先做）**：纯插件，shell 工具调用前正则检查，命中直接拒。零 UI 依赖，可独立交付。
  - **APPROVAL 层（后做）**：依赖 dsh 审批瀑布在 **HMC 手机端** 的呈现方式——未知数，需先验证，不可假设。
- **价值**：高。cron 和浏览器跑起来后自动操作破坏力上升，审批是安全带。Tomas 单用户自用，WRITE 默认放行已够宽松，短期不阻塞。
- **成本/风险**：中。BLOCKED 正则可直接移植；APPROVAL 需先验证 HMC 行为。

### P1：#5 多步浏览器（先过内存压测）

- **Python 实现**：11 个工具（`browser_open/snapshot/screenshot/close/select/wait/downloads/click/fill`…）；persistent context（`workspace/browser_profile`）；下载自动落盘。
- **dsh 原生差距**：`dsh-tool-web` 只有单次抓取/搜索。但上游 `packages/experimental/` 已有 `browser-use-playwright-mcp`、`browser-use-chrome-devtools-mcp`、`browser-use-stagehand-native` + `browser-use-runtime`（会话级浏览器资源），默认安装未挂载——**路线应为评估挂载上游实验性 provider，而非新写 Playwright 插件**（第三方审计发现）。
- **dsh-muse 路线**：在隔离 profile 挂载上游 `browser-use-playwright-mcp` 做冒烟 + 内存压测。**硬约束**：Chromium 常驻约 150–300MB，1GB 机器上只能按需启动、用完即关；上游 `browser-use-runtime` 的 Session-owned 资源模型正好支撑此策略。Cloudflare 指纹拦截是已知风险（2026-09-27 ChatGPT 路线已验证）。
- **价值**：高，电脑级智能体的标志能力。但 Tomas 主用手机遥控，浏览器更多是 agent 自主 research 用，非每天高频。
- **成本/风险**：中（原估"高"下调）。实验性包无稳定性承诺仍是真风险；仍需内存压测，OOM 则停下报告（硬约束）。
- **前置条件**：内存压测通过才能开工。

### P2：#12 Goals 长期目标（简单，不紧急）

- **Python 实现**：`agent/goals.py`。`goals.json` + 自动渲染 `goals.md`；`goal_list/create/log/close` 四个工具；studying 任务读 goals.md。
- **dsh 原生差距**：dsh 的 goal 是同会话驱动，语义完全不同；无跨会话长期目标概念。
- **dsh-muse 路线**：插件实现（约 150 行），step 1 注入 active goals（复刻 profile 画像注入模式）。
- **价值**：中。Muse 侧 tracking 系统已在管目标，dsh-muse 内部 goals 是 agent 自视角，短期不缺也能跑。
- **成本**：低。随时可做，不紧急。

### P2：#10 Hooks（无触发源，暂缓）

- **Python 实现**：`agent/hooks.py`。webhook（`POST /api/hooks/<name>`）+ 文件轮询（`workspace/inbox/` 每 20 秒）；动作：跑一轮 agent 或 shell。
- **dsh 原生差距**：入站 webhook dsh 原生有；缺 inbox 文件轮询。
- **关键问题**：**触发源在哪里？** Tomas 已决定不复刻 Gmail 等外部连接器——inbox 里谁放文件？webhook 给谁调？无外部系统接入就是空转轮询。
- **结论**：实现成本低（几十行），但无触发场景即死代码。等 Tomas 明确"我要 X 事件触发 Y"再做。

### P2：#8 Artifacts v2（无消费场景，暂缓）

- **Python 实现**：`agent/artifacts.py`（207 行）。版本历史；HTML sandbox iframe 渲染；`/raw` `/download` `/versions` 路由。
- **dsh 原生差距**：dsh 只有交付声明，无版本、无沙盒渲染。
- **关键问题**：**谁消费？** Tomas 主用手机 HMC；Python 版依赖 Web UI，而 dsh-muse 连 Feed Web UI 都未复刻（阶段 4 已知缺口）。存储层简单，贵的是渲染 UI。
- **结论**：等 Feed Web UI 排期时一起考虑，或 Tomas 明确要手机看文档再做。

### 实施路线图

| 批次 | 内容 | 前置条件 |
|---|---|---|
| 切生产前 | #11 用户自建 cron（优先评估复用 dsh-schedule 触发层）+ #9 BLOCKED 层 | 均为纯插件，隔离 profile 可验证；需 Tomas 批准开工 |
| 切生产后① | #9 APPROVAL 层 | 先验证 dsh 审批瀑布在 HMC 手机端的呈现（V3） |
| 切生产后② | #5 多步浏览器（挂载上游 browser-use-playwright-mcp） | 先通过内存压测（V4，OOM 则停） |
| 按需 | #12 Goals / #10 Hooks / #8 Artifacts v2 | Tomas 一句话启动；#12 先回答真相源冲突问题 |

**与生产切换的关系**：2026-09-29 14:34 收 24h 空转监控结果 → Tomas 决策是否切生产 → 切生产后再按上表推进。切生产前不做 P1/P2，避免在隔离环境验证无意义的功能。

**第三方审计**（2026-09-28）：独立审计员对照上游公开仓库逐项核验，结论**有条件通过**——23 项分类自洽；8 项"dsh 原生"中 6 项确认，#4（web 三后端）纠正事实错误，#2（沙箱）加限定；#11 dsh-schedule"完全不等价"为夸大，已改述为架构选型；#5 浏览器路线改为评估挂载上游实验性 provider。P0/P1/P2 排序获审计认可，仅调整实现路线。待验证项（V1–V6）：V1 确认 profile 挂载的执行器；V2 披露记忆分词器+中文召回抽查；V3 验证 HMC 手机端 approval 呈现；V4 上游 Playwright MCP 冒烟+内存压测；V5 实测 dsh-schedule 等价性；V6 持有 dsh-muse 仓库访问权者做第二轮代码审计。
