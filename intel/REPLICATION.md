# dsh-muse 复刻总览（2026-09-28 修订版）

> 一份文档讲清三件事：
> ① dsh-muse 是否完成复刻（结论先行），
> ② 完整复刻清单（23 项 Python 能力 → dsh 的去向，**以 §2 表为唯一权威口径**），
> ③ DeepSeek Harness 改造汇总（Python 单体 → dsh 插件架构）。
>
> 修订：2026-09-28，HEAD `c7a60ce58`。单元测试 21 用例 21 通过。

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
| 24h 稳定性测试 | 🔄 运行中（2026-09-29 14:34 出结果） |
| HMC 端到端 | ✅ API 级通过（脚本入库，见 `intel/hmc-e2e.sh`）；手机上屏待 Tomas 真机 |

**23 项的去向（由 §2 表统计得出，唯一口径）：**

| 去向 | 数量 | 说明 |
|---|---|---|
| dsh 原生已有 | 8 | agent loop、shell、文件、子智能体、会话、上传、MCP、网页搜索——直接用 |
| 新写插件 | 8 | 记忆、quiet、进化、Heartbeat、Feed、joblog、画像、晨报——dsh-muse 补的 |
| 未排期 | 6 | 浏览器工具、Artifacts v2、审批四级、Hooks、Goals、用户 cron——任务书范围外 |
| 不复刻 | 1 | 出站推送（#20）——Tomas 决定保留通道不推进 |

**另有 7 项 Python 能力不在 23 项对比范围内**（dsh-muse 任务书从未纳入，Tomas 已决策不复刻）：Gmail 等外部连接器、人物/群组档案、onboarding 教学、图片生成、TTS、预订/快递追踪、Tailscale Serve 跨重启验收。

---

## 二、完整复刻清单（23 项 → dsh 去向）

"去向"列：**dsh 原生** = 直接用；**新写插件** = dsh-muse 补的；**不复刻** = Tomas 决策；**未排期** = 任务书范围外。

| # | Python 能力 | 去向 | dsh-muse 实现 | 验证 |
|---|---|---|---|---|
| 1 | agent loop + 工具调用 | dsh 原生 | `dsh-agent-loop`（DeepSeek Harness 专用） | 阶段 0 |
| 2 | 终端 `shell_exec` | dsh 原生 | `dsh-tool-bash`（多一层沙箱，超集） | 阶段 0 |
| 3 | 文件读写 | dsh 原生 | `dsh-tool-fs`（多搜索/替换，超集） | 阶段 0 |
| 4 | 网页抓取 + 搜索 | dsh 原生 | `dsh-tool-web`（单 DeepSeek 后端，基本等价） | 阶段 0 |
| 5 | 多步浏览器（11 工具） | 未排期 | dsh 安装版无等价物；任务书未要求 | — |
| 6 | 子智能体 | dsh 原生 | `dsh-tool-subagent`（in-process 驱动） | 阶段 2 真测 |
| 7 | 语义记忆 | 新写插件 | `dsh-intelligence-memory`（SQLite FTS5；向量方案因 1GB 内存否决） | 阶段 1 |
| 8 | Artifacts v2 | 未排期 | dsh 只有交付声明，无版本/沙盒；任务书未要求 | — |
| 9 | 审批卡 + 四级权限 | 未排期 | dsh 有审批瀑布，无四级；任务书未要求 | — |
| 10 | Hooks | 未排期 | dsh 有入站 webhook，无 inbox 轮询；任务书未要求 | — |
| 11 | 用户自建 cron | 未排期 | dsh-schedule 是提醒非后台执行；任务书未要求 | — |
| 12 | Goals 长期目标 | 未排期 | dsh goal 是同会话驱动，语义不同；任务书未要求 | — |
| 13 | Quiet-moment | 新写插件 | `dsh-intelligence-quiet`（turn-stopping + 真后台） | 阶段 2 + 真后台修复 |
| 14 | 自我进化（5 任务） | 新写插件 | `dsh-intelligence-evolution`（cron + headless profile） | 阶段 3 |
| 15 | Heartbeat | 新写插件 | 同上（`heartbeat_check` + 3 项分块） | 阶段 3 |
| 16 | Feed | 新写插件 | `dsh-intelligence-feed`（`feed_post`/`feed_read`） | 阶段 4 |
| 17 | 多会话 | dsh 原生 | `dsh-session`（多历史检索，超集） | 阶段 0 |
| 18 | 文件上传 | dsh 原生 | `dsh-attachment` | 阶段 0 |
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
| 定时任务 | APScheduler 进程内线程 | 系统 cron + `dsh --profile evolve` headless（dsh-schedule 语义不等价，弃用） |
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
- 24h 稳定性（运行中）+ HMC 端到端 API 级验证（脚本入库）

### 3.3 关键决策记录

| 决策 | 选项 | 依据 |
|---|---|---|
| quiet 后台机制 | 真后台（fire-and-forget），不用 ctx.jobs | 复刻 Python daemon 线程语义；Tomas 拍板 |
| 真后台适用边界 | **仅常驻 profile（web-intel）**；headless 不得依赖 | headless 退出会取消后台子智能体；evolve 不含 quiet |
| 定时任务承载 | 系统 cron + headless，不用 dsh-schedule | dsh-schedule 是"提醒"语义，不等价 |
| 手机端可见性 | A：隐形基础设施，不做 followup 弹窗 | 复刻 Python Muse 方式；Tomas 拍板 |
| 记忆检索 | FTS5/BM25，不用向量 | 1GB 内存；ONNX 同进程 374MB 太贵 |
| HMC 客户端 | 不修改，只读 + PR | Tomas 指示 |
| 生产切换 | 未切；Python 仍是生产，dsh 在 web-intel 隔离验证 | Tomas 未批准切换 |

### 3.4 生产状态（2026-09-28）

- **生产**：Python `deepseek-harness.service`（:8080），Tailscale Serve 指向它
- **dsh**：`web-intel` 隔离 profile（:3080，24h 稳定性测试中）；`evolve` 跑定时任务（cron 未激活）；`hmc-test` 跑 HMC 服务验证（:43197）
- **Git**：`intel` 分支已 push，`master` 跟踪上游
- **待 Tomas**：HMC 手机真机配对；生产切换批准；cron 激活批准
