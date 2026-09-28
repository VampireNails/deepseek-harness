# dsh-muse 复刻总览（2026-09-29 修订版）

> 一份文档讲清五件事：
> ① dsh-muse 是否完成复刻（结论先行），
> ② 完整复刻清单（23 项 Python 能力 → dsh 的去向，**以 §2 表为唯一权威口径**），
> ③ DeepSeek Harness 改造汇总（Python 单体 → dsh 插件架构），
> ④ 未排期 5 项的升级提议详细评估（P0/P1/P2），
> ⑤ 23 项之外的复刻：Muse 行为规则（Soul）。
>
> 修订：2026-09-29，内容对应提交 `96ac06d66`。（本行之外的全部内容与该提交一致；若 HEAD 更新而本行未动，以 git log 为准）。
> 本次修订（2026-09-29 上午，第三轮审计 12 项确认优化全部落实）：#11 cron 提醒 prompt 自带复述指令（手机可见性硬伤修复，方案 1）；新增"生产默认 workspace-write+ask 运维告知"，#9 归因修正（① HMC 启动器路径 vs ② 生产路径分开）；bootstrap 等价性口径修正（hmc-test 仅冒烟）；数字校正（cron 42 用例、内存双点位说明、cron 激活改 🔄）；Soul 补"手机侧不可见 + step-1 三注入 token 成本"；HMC 连接地址口径澄清（走 IP，域名仅 PC Web）；#9 APPROVAL 分级优先级下调；§3.4 夹具已由 Tomas 在 HMC 2b4f7f6 补回（无需另开 PR）。
> 上次修订（2026-09-29 凌晨）：P0 用户自建 cron 插件完成并真机验收（#11 从"未排期"转为"新写插件"，23 项口径变为 8+9+5+1）；Muse Soul 行为规则复刻上线（新增 §五）；生产切换完成（Python 退役，dsh-prod 接管，不留遗留）；HMC 默认权限 PR 已合并（#9 跨仓库决策解决）；系统 cron 分档激活已批准（stage-1 待 2026-09-29 14:40 执行）。
> 上上次修订（2026-09-28 晚间）：第二轮审计（HMC 交叉核验）后修正：bootstrap 两处硬伤已修复并通过干净验收；HMC 端到端证据已对齐为脚本真实输出；#9 APPROVAL 层改为跨仓库决策项（V3 关闭）；#11 新增"手机可见性"选型判据；#8 改为"消费通道部分存在"。

---

## 一、结论：dsh-muse 是否完成复刻

**智能层复刻已完成，且已接管生产。23 项 Python 能力未全部搬运——这是设计使然，不是缺口。**

dsh-muse 的任务书只定义了 5 个阶段，目标是把 Python 栈里 **dsh 没有的那部分"智能"** 搬过去：

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 环境验证 | ✅ 完成 |
| 1 | 记忆（memory_write/read/search） | ✅ 完成 |
| 2 | quiet-moment 复盘 | ✅ 完成（含真后台修复） |
| 3 | 自我进化 + Heartbeat + joblog | ✅ 完成 |
| 4 | Feed + 用户画像 + 晨间简报 | ✅ 完成 |
| P0 | 用户自建 cron 插件 | ✅ 完成（dsh-intelligence-cron；HMC 真机验收） |
| Soul | Muse 行为规则复刻 | ✅ 完成并上线（dsh-intelligence-soul，见 §五） |
| 切换 | 生产切换（Python → dsh-muse） | ✅ 完成（dsh-prod.service；不留遗留） |
| HMC | 默认权限 PR（新会话 workspace-write） | ✅ 完成并合并（origin/main） |

评估修复（第三方评估，两轮）：

| 问题 | 状态 |
|---|---|
| 单元测试（21 用例全过）+ 集成测试（3/3） | ✅ 完成 |
| quiet 真后台（复刻 Python daemon 语义） | ✅ 完成 |
| 迁移脚本（migrate.sh + rollback.sh） | ✅ 完成（含 spaceCjk 对齐修复） |
| 文档订正 + README npm ci | ✅ 完成 |
| run-integration.sh DSH_HOME 隔离 | ✅ 完成 |
| bootstrap 可复现脚本 | ✅ 完成（第二轮审计发现两处硬伤已修：`intel/hmc-service-wrapper` 入库（此前引用路径不存在）+ `npm ci`→`npm install`（无锁文件必然失败）；`DSH_HOME=$(mktemp -d)` 干净验收三 profile 全过。第三轮审计 §3.3 口径修正：hmc-test 是 API 级冒烟 profile（能装、能起、API 通），未并入 HMC `service/cordis.patch.yml` 的 4 项（其中 `pwsh-sandbox` 为 Windows 专用，故意不并），**不等价于真机验收环境**；依赖 `/root/intel/hmc-client` 外部路径，wrapper README 已披露） |
| 24h 空转存活监控 | 🔄 运行中（2026-09-29 14:34 出结果；仅进程存活+RSS，非负载稳定性） |
| HMC 端到端 | ✅ API 级通过（TLS→401→session/create→session/prompt→memory_write SQLite 落盘，全绿；证据 `intel/hmc-e2e-evidence-2026-09-28.log` 为 `hmc-e2e.sh` 真实 stdout，第二轮审计对齐）；手机真机已直连 100.73.148.102:43197，Tomas 确认"已连上" |
| 系统 cron 分档激活 | 🔄 已批准、待执行；stage-1（5 个低频任务）2026-09-29 14:40 执行（24h 监控结束后）；stage-2（每小时 upkeep/heartbeat）观察后决定 |

**23 项的去向（由 §2 表统计得出，唯一口径）：**

| 去向 | 数量 | 说明 |
|---|---|---|
| dsh 原生已有 | 8 | agent loop、shell、文件、子智能体、会话、上传、MCP、网页搜索——直接用 |
| 新写插件 | 9 | 记忆、quiet、进化、Heartbeat、Feed、joblog、画像、晨报、**用户 cron**——dsh-muse 补的 |
| 未排期 | 5 | 浏览器工具、Artifacts v2、审批四级、Hooks、Goals——任务书范围外，见 §四 评估 |
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
| 9 | 审批卡 + 四级权限 | 未排期 | dsh 有审批瀑布，无四级；任务书未要求。**分两条路径**（第三轮审计 §3.2）：② 生产路径（`dsh-prod.service --profile web`，env 未设 `DSH_PERMISSION_MODE`）走上游 base profile 默认即 workspace-write + ask（`apps/cli/reference/README.zh.md:123`），新会话审批卡会弹——切生产那一刻即生效，**与 HMC PR 无关**；① HMC 启动器路径：PR（4b4da70）已合并，把写死的 danger-full-access 改为默认 workspace-write，新会话同样可弹卡。dsh 侧 BLOCKED 硬拦截维持 P1，APPROVAL 分级优先级下调（默认 ask 已能弹卡） | §四 P1 |
| 10 | Hooks | 未排期 | dsh 有入站 webhook，无 inbox 轮询；任务书未要求 | §四 P2 |
| 11 | 用户自建 cron | 新写插件 | `dsh-intelligence-cron`（`cron_create`/`cron_list`/`cron_delete`，中文时间解析；dsh-schedule 触发层 + 系统 cron 后台层，手机可见性为第一判据） | P0 真机验收 |
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
| 23 | 晨间简报 | 新写插件 | cron 08:00（web_search + 画像 + 记忆 → Feed）；**stage-1 分档激活已批准**，2026-09-29 14:40 执行（含 07:30 学习、09:30 想法、23:30 复盘、周日 03:00 技能审查共 5 个低频任务） | 阶段 4 + 分档激活 |

**统计**：8 dsh 原生 + 9 新写插件 + 5 未排期 + 1 不复刻 = 23 ✅

**新写插件汇总**（8 个，全部在 `intel-plugins/`，树外独立 npm 包）：

| 插件 | 工具/能力 | 数据位置 |
|---|---|---|
| dsh-intelligence-memory | `memory_write`/`memory_read`/`memory_search`，step 1 自动召回 | `~/.dsh/intel-memory/memory.db` |
| dsh-intelligence-quiet | `turn-stopping` 监听，真后台复盘（fire-and-forget） | `~/.dsh/intel-quiet/` |
| dsh-intelligence-evolution | `heartbeat_check` + 5 进化任务 prompt | `~/.dsh/intel-evolution/` |
| dsh-intelligence-joblog | `joblog_*`，三件套 | `~/.dsh/intel-joblog/` |
| dsh-intelligence-feed | `feed_post`/`feed_read` | `~/.dsh/intel-feed/feed.json` |
| dsh-intelligence-profile | `user_profile_read`/`user_profile_update`，step 1 注入 | `~/.dsh/intel-profile/` |
| dsh-intelligence-cron | `cron_create`/`cron_list`/`cron_delete`，中文时间解析 | `~/.dsh/intel-cron/` |
| dsh-intelligence-soul | 每回合 step 1 注入 Soul；`soul_read`/`soul_update` | `~/.dsh/intel-soul/soul.md` |

---

## 三、DeepSeek Harness 改造汇总

### 3.1 架构范式转移：Python 单体 → dsh 插件

| 维度 | Python 栈（/opt/deepseek-harness，已退役） | dsh-muse（intel 分支，生产） |
|---|---|---|
| Agent 骨架 | 自研 `agent/loop.py`（约千行） | dsh 原生 `dsh-agent-loop`（零代码） |
| 工具系统 | `agent/tools.py` 手工注册 | dsh 原生工具包（bash/fs/web/subagent…） |
| 插件机制 | 无（改代码即改核心） | Cordis 插件，`ctx.effect` 注册，卸载可回卷 |
| 定时任务 | APScheduler 进程内线程 | 系统 cron + `dsh --profile evolve` headless（架构选型：脱离 Host 进程独立执行、故障隔离；dsh-schedule 为会话绑定的 reminder 交付，见第三方审计）。分档激活：stage-1 低频 5 任务先行，stage-2 每小时任务观察后定 |
| 会话存储 | 自研 `sessions.py` | dsh 原生 event-sourced session |
| 智能层 | 与骨架耦合在同一进程 | 8 个树外独立插件，可单独装卸 |
| 行为规则 | Muse 内建（不可见） | `dsh-intelligence-soul` 显式复刻，每回合注入（见 §五） |
| 语言栈 | Python 3.10 | Node.js（dsh 原生）；最终零 Python 依赖 |

**核心洞察**（2026-09-28 决策）：复刻版 Python 栈把 dsh 的活重做了一遍——两个平行大脑。HMC 只认 dsh 事件方言是决定性约束，智能层必须长在 dsh 上，而不是另起一套。

### 3.2 分阶段改造记录

**阶段 0：环境验证**（2026-09-28）
- dsh 0.1.7-rc.1（46a7f68b0）在 1GB 机器上内存无忧：web 启动后 RSS 峰值约 144MB（阶段 0 测量），单回合峰值 139MB；24h 空转监控（2026-09-28/29）复核：启动峰值 145936KB 后缓降，24h 后约 11MB——两者是同一进程生命周期的不同点位（启动峰值 vs 长期空转稳态），非矛盾（第三轮审计 §3.5 口径说明）
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
- cron 分档激活：stage-1（5 个低频任务）2026-09-29 14:40 执行；stage-2（每小时 upkeep/heartbeat）观察后决定

**阶段 4：Feed + 画像 + 晨报**（2026-09-28）
- Feed：新在前、最多 100 条、ID f1/f2…（与 Python 格式兼容，迁移可直接合并）
- 画像：仅 Tomas 本人，step 1 自动注入（复刻 Python dreaming 每晚更新机制）
- 晨报：08:00 cron，web_search + 画像 + 记忆 → 中文简报 → Feed（stage-1 激活）

**P0：用户自建 cron 插件**（2026-09-28，Tomas 批准开工）
- `dsh-intelligence-cron`：`cron_create(name, prompt, schedule)` / `cron_list` / `cron_delete(ref)`；中文时间解析
- 单测 42/42（含提醒复述指令回归测试；其中 2 个真解码器用例需 dsh 运行时，服务器上全过、无运行时的环境自动跳过）、隔离集成测试 6/6、旧插件回归 21/21
- 真机验收：经 HMC API 创建 5 分钟一次性任务，到点生成 follow-up turn（"⏰ p0test 提醒到点"），`cron.json` 状态流转正确，测试任务已删除
- 手机可见性判据（2026-09-29 第三轮审计 §3.1 修正）：dsh-schedule 到点投递的是 `source.kind='schedule'` 消息，HMC 客户端只渲染 `kind=="user"`（`Timeline.java:150-151`）——**提醒正文在手机时间线上不可见，看得见的是智能体对提醒的回复**（`assistant/message` 无条件渲染）。补法（已实施，方案 1）：cron 插件的提醒 prompt 自带"先把提醒原文完整复述给用户，再执行任务"指令，让回答自带原文，规避渲染过滤。隐形基础设施，不做 followup 弹窗（复刻 Python Muse 方式，Tomas 拍板）

**Soul：Muse 行为规则复刻**（2026-09-28，Tomas 要求"复刻你 muse 的规则"）
- `dsh-intelligence-soul`：每回合 step 1 自动注入；`soul_read` / `soul_update(section, content)` 运行时更新；持久化 `~/.dsh/intel-soul/soul.md`（直接改文件下回合生效，无需重启）
- 单测 5/5；已装入生产 web profile；HMC 真机会话验证注入生效（询问"做事方式第一条"，快照命中"先自己动手"）
- 规则要点见 §五

**生产切换**（2026-09-28，Tomas 批准，要求"不要有遗留"）
- `dsh-prod.service`：单个 dsh 进程同时监听 Web `127.0.0.1:8080` + HMC TLS `100.73.148.102:43197`；profile=web；`EnvironmentFile=/etc/deepseek-harness/.env`（干净，明确无 `DEEPSEEK_BASE_URL`）；Tailscale Serve 保持指向 `:8080`
- 迁移：Python 记忆 31/31、Feed、`user_profile.md`、goals/todos/ideas、两天日记归档；清理一条混入的假测试记忆（"用户喜欢喝茶"）
- HMC API 真 DeepSeek 端到端验证通过（provider=deepseek-official）；Web `:8080` 返回 401（token 鉴权正常）
- 不留遗留：`deepseek-harness.service` / `mobile-mirror.service` disable+inactive；旧手动 hmc-test 生产进程已停（`:3095` 释放）；孤儿 mock `:18003/:18091/:18092` 已清；`headless` 实验 profile 已删；`/opt/deepseek-harness`（467MB）先归档后删除
- 备份与回滚：全量备份 `/root/intel/cutover-backup-20260928/`（137MB）；一键完整回滚 `/root/intel/dsh-fork/intel/rollback-full.sh --yes`（commit `96ac06d66`，已 push）

**HMC 默认权限 PR**（2026-09-28，Tomas 批准，已合并）
- `scripts/host-env.mjs`：`hostEnv.DSH_PERMISSION_MODE ??= 'workspace-write'`——新会话默认 workspace-write + ask（触发审批卡），`danger-full-access` 仅显式 opt-in；旧会话持久化权限不受影响
- commit `4b4da70`，已确认为 `origin/main` 祖先；服务器 `/root/intel/hmc-client` 已切回 main 并 pull
- PR 改的是 HMC 启动器；生产由 `dsh-prod.service` 直接启动 dsh，无需为此重启生产，手机链路未中断
- 意义：#9 APPROVAL 层的跨仓库卡死条件解除（默认部署下审批卡可弹）
- 附带发现（第三轮审计 §3.4）：该 PR 改 `tests/host-env.test.mjs` 时删掉了 `UNRELATED_SECRET=do-not-load` 夹具（原用例守的是"只从项目 .env 读 DEEPSEEK_API_KEY，不加载其它密钥"）。**Tomas 已在 HMC `2b4f7f6`（2026-09-28 21:19）亲手补回**，单测 2/2 通过；剩余两处缩进为纯风格问题，未单独立 PR（避免噪音），在此记录

**生产默认 workspace-write + ask 的运维含义**（2026-09-29 第三轮审计 §3.2，新增告知）
- 生产（`--profile web`，env 未设 `DSH_PERMISSION_MODE`）的新会话默认就是 workspace-write + ask：手机顶栏显示「权限：工作区可写，敏感操作需审批」（A 级真机可见），敏感操作会弹审批卡等人点。
- **这是切生产那一刻即发生的线上行为变化，与 HMC 的 PR 无关**（生产由 `dsh-prod.service` 直接启动 dsh，不经过 HMC 启动器）——此前文档把功劳记在 PR 名下，现修正归因：① HMC 启动器路径靠 PR 解除写死，② 生产路径靠上游 base profile 默认。
- `ask` 是 fail-closed：无人值守时（没开后台提醒，或超过 HMC 2 小时监控窗口），触发审批的回合会**卡住等人**。要无人值守：显式 opt-in `DSH_PERMISSION_MODE=danger-full-access`，或把后台提醒开着。

**事故：mock 泄漏**（2026-09-28 晚）
- 生产 HMC 的 dsh 后端被发现连着 mock LLM（`DEEPSEEK_BASE_URL=http://127.0.0.1:18099/v1`）约 2.5 小时，手机收到的全是 mock "ok" 回复
- 根因：测试 shell 的环境变量泄漏进了手动启动的生产 dsh 进程
- 修复：kill 旧进程，用生产 .env（无 BASE_URL）+ HMC_CONFIG 干净重启；HMC API 端到端验证真 DeepSeek（"收到"，provider=deepseek-official）
- 教训（已写入 Soul 规则）：mock/测试 profile/测试数据与生产硬隔离；测试 shell 与生产启动分离；生产切换用 systemd + 干净 EnvironmentFile 从结构上杜绝泄漏

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
| HMC 客户端 | 不修改，只读 + PR | Tomas 指示（PR 已合并为例外通道，用完即止） |
| 生产切换 | dsh-prod.service 单进程双监听，不留遗留 | Tomas 批准"不要有遗留"；11 项清理清单全执行 |
| Soul 规则地位 | dsh 智能层每回合注入，运行时可更新 | Tomas 要求复刻 Muse 规则；持续烧 token 的事须先审批 |
| 系统 cron 激活 | 分档：stage-1 低频 5 任务先行，stage-2 观察后定 | 按 Soul 规则判定：等 24h 空转监控结束 + Tomas 批准（2026-09-29 已批准 stage-1） |
| 测试与生产隔离 | 硬隔离：独立 shell/env/profile/systemd | mock 泄漏事故教训；已入 Soul |
| #9 APPROVAL 层 | 跨仓库决策已解决（分路径），dsh 侧分级未排期 | ① HMC 启动器曾写死 danger-full-access 致默认永不弹（第二轮审计）；2026-09-28 PR（4b4da70）合并后该路径解除。② 生产路径走上游 base profile 默认 workspace-write+ask，与 PR 无关（第三轮审计 §3.2）。dsh 侧：BLOCKED 硬拦截维持 P1（走 `tools/pre-execute` 缝，不受权限预设影响）；APPROVAL 分级优先级下调——默认 ask 已能弹卡，分级（READ/WRITE 免税）是优化非必需 |
| #11 选型第一判据 | 手机可见性 | dsh-schedule 提醒落同一会话时间线，是手机上唯一看得见的路径（第二轮审计；HMC ADR 002：后台提醒仅单会话/上限 2 小时） |

### 3.4 生产状态（2026-09-29）

- **生产**：`dsh-prod.service`（active）——单个 dsh 进程同时监听 Web `127.0.0.1:8080` + HMC TLS `100.73.148.102:43197`；profile=web；干净 env（无 `DEEPSEEK_BASE_URL`）；Tailscale Serve 指向 `:8080`；8 个智能插件全挂载
- **退役**：Python `deepseek-harness.service` 已 disable+inactive；`/opt/deepseek-harness` 已归档删除；`mobile-mirror.service` 已 disable+inactive；旧手动 hmc-test 生产进程已停（`:3095` 释放）；孤儿 mock 端口已清
- **备份与回滚**：全量备份 `/root/intel/cutover-backup-20260928/`（约 137MB）；一键完整回滚 `/root/intel/dsh-fork/intel/rollback-full.sh --yes`
- **HMC 手机**：已直连 `100.73.148.102:43197` 配对成功，真 DeepSeek 回复（provider=deepseek-official）。**连接地址口径**（第三轮审计 §3.7）：HMC 服务走 IP（`100.73.148.102:43197`）；`hmc-mirror.tail52c730.ts.net` 域名仅用于 PC Web（Tailscale Serve → 127.0.0.1:8080）。HMC TLS 证书 SAN（`CN=hmc-test`，含 IP 100.75.211.11/100.73.148.102/127.0.0.1）不含该域名——改域名访问 HMC 会因主机名校验失败。保持走 IP，或重签证书补 SAN。
- **24h 空转存活监控**：web-intel `:3080` + mock LLM `:18099`，2026-09-29 14:34 出结果（结束后停 mock、归档日志、释放 `:18099`）
- **系统 cron**：stage-1 分档激活已批准，2026-09-29 14:40 执行（5 个低频任务）；stage-2（每小时 upkeep/heartbeat）观察几天 token 消耗与稳定性后再决定
- **Git**：`intel` 分支已 push（HEAD `96ac06d66`），`master` 跟踪上游
- **每周养成检查**：已改名"dsh-muse 每周养成检查"（每周一 09:15，覆盖 dsh-muse 生产健康 + 进化产出 + 升级提议）
- **待 Tomas**：stage-2 cron 激活；§四 P1/P2 开工（#9 dsh 侧 BLOCKED/APPROVAL、#5 浏览器内存压测）

---

## 四、未排期 5 项升级提议评估（2026-09-29 修订）

评估方法：逐项核对 Python 源码实现 → dsh 原生差距 → dsh-muse 实现路线 → 对 Tomas 的价值 → 成本/风险 → 优先级。

### ✅ P0：#11 用户自建 cron——已完成（2026-09-28）

- **交付**：`dsh-intelligence-cron` 插件，`cron_create`/`cron_list`/`cron_delete` 三个工具，中文时间解析
- **验证**：单测 42/42（含提醒复述指令回归测试；其中 2 个真解码器用例需 dsh 运行时，服务器上全过、无运行时的环境自动跳过）、隔离集成测试 6/6、旧插件回归 21/21；HMC 真机验收（5 分钟一次性任务到点生成 follow-up turn，`cron.json` 状态流转正确，测试任务已删）
- **设计**（第三方审计建议拆两层，第二轮审计加选型判据，第三轮审计 §3.1 修正口径）：**选型第一判据是手机可见性**——但机制上**看得见的是回答，不是提醒正文**：dsh-schedule 到点投递 `source.kind='schedule'`，HMC 客户端只渲染 `kind=="user"`，提醒正文在手机时间线上不可见；手机上能看到的是智能体对提醒的回复。补法（已实施，方案 1）：提醒 prompt 自带"先把提醒原文完整复述给用户"指令，让回答自带原文，规避渲染过滤（上游 schedule README：delivery never uses email/SMS/push；HMC ADR 002：后台提醒仅监控单条会话、上限 2 小时）；需脱离会话的后台任务保留系统 cron + `dsh --profile evolve` headless
- **验收判据**："到点能在手机上看见智能体产生了回答（含复述的提醒原文）"。诚实边界：仅会话活着且处于 2 小时监控窗口内才可能推到手机；出窗口退化为"回 App 才看到"。真机投递 schedule 消息的 A 级验证待补（第三轮审计 §六：建一条 cron 等触发后手机截图）
- **原评估中的路线已兑现**：中文时间解析 + dsh-schedule 触发层复用，手机可见性判据指导了实现

### P1：#9 审批卡 + 四级权限（跨仓库决策已解决，dsh 侧未排期）

- **Python 实现**：`agent/permissions.py` + `agent/approvals.py`。四级：READ（自动放行）/ WRITE（默认放行+记日志）/ APPROVAL（弹卡等确认，主循环现场存 `pending_runs`，30 分钟过期）/ BLOCKED（高危 shell 正则直接拒：`rm -rf /`、fork 炸弹、写裸设备等）。
- **dsh 原生差距**：dsh 有审批瀑布，但无分级、无 BLOCKED 硬拦截。
- **跨仓库决策（已解决，2026-09-28）**：HMC 启动器曾把新会话权限写死 `DSH_PERMISSION_MODE='danger-full-access'`（单测锁定该行为），手机 RPC 白名单无改权限模式接口 → 默认部署下审批卡永远不弹。Tomas 批准 PR：`scripts/host-env.mjs` 改为 `??= 'workspace-write'`，新会话默认触发审批卡，`danger-full-access` 仅显式 opt-in；PR（`4b4da70`）已合并进 `origin/main`。旧会话持久化权限不受影响。
- **dsh-muse 路线（未排期）**：
  - **BLOCKED 层（先做）**：纯插件，shell 工具调用前正则检查，命中直接拒。零 UI 依赖，可独立交付。**理由比原先更硬**：走 `tools/pre-execute` 缝（`packages/core/agent-loop/src/tool-calls.ts:211,216`，上游测试 `tests/interception.spec.ts:705` 称其为"原生插件权限模式"），不依赖 dsh 权限预设与审批瀑布→**不受 HMC 权限模式影响**（第二轮审计确认）。
  - **APPROVAL 层（优先级下调，第三轮审计）**：HMC 呈现层早就实现且有真机用例（V3 已关闭）：`MainActivity.java:707-736` 收到 `approval/request` 弹「工具请求权限」卡（allowed-once/rejected），`Timeline.java:163-164` 渲染，`MonitorEvents.java:78-81` 后台提醒「有待处理审批」，服务端 `service/security.mjs:37-72`（ApprovalFence）校验；真机测试 `ClientDeviceTest.java:63-69`、`MonitorNotificationDeviceTest.java:23`、`PhoneMonitorDeviceTest.java:129`。PR 合并后默认部署下审批卡可弹，**且生产路径默认预设已是 ask——不实现 dsh 侧分级也能弹卡**。分级价值仍在（READ/WRITE 免税、BLOCKED 硬拒），但属优化非必需，故下调。
- **价值**：高。cron 和浏览器跑起来后自动操作破坏力上升，审批是安全带。Tomas 单用户自用，WRITE 默认放行已够宽松，短期不阻塞。
- **成本/风险**：中。BLOCKED 正则可直接移植；APPROVAL 等 Tomas 一句话开工。

### P1：#5 多步浏览器（先过内存压测）

- **Python 实现**：11 个工具（`browser_open/snapshot/screenshot/close/select/wait/downloads/click/fill`…）；persistent context（`workspace/browser_profile`）；下载自动落盘。
- **dsh 原生差距**：`dsh-tool-web` 只有单次抓取/搜索。但上游 `packages/experimental/` 已有 `browser-use-playwright-mcp`、`browser-use-chrome-devtools-mcp`、`browser-use-stagehand-native` + `browser-use-runtime`（会话级浏览器资源），默认安装未挂载——**路线应为评估挂载上游实验性 provider，而非新写 Playwright 插件**（第三方审计发现）。
- **dsh-muse 路线**：在隔离 profile 挂载上游 `browser-use-playwright-mcp` 做冒烟 + 内存压测。**硬约束**：Chromium 常驻约 150–300MB，1GB 机器上只能按需启动、用完即关；上游 `browser-use-runtime` 的 Session-owned 资源模型正好支撑此策略。Cloudflare 指纹拦截是已知风险（2026-09-27 ChatGPT 路线已验证）。
- **价值**：高，电脑级智能体的标志能力。但 Tomas 主用手机遥控，浏览器更多是 agent 自主 research 用，非每天高频。
- **成本/风险**：中（原估"高"下调）。实验性包无稳定性承诺仍是真风险；仍需内存压测，OOM 则停下报告（硬约束）。
- **前置条件**：内存压测通过才能开工。
- **手机侧实情**（第二轮审计）：手机无浏览器/截图原生预览——`artifact/read` 是文本（二进制走 base64，单文件上限 4MB），只能经 SAF 存盘再看。浏览器产物上手机的唯一路径是"落进 workspace → 工作区文件 → 下载"，**与 #8 共用"手机端产物通道"议题**，路线图里合并考虑，不各自为战。

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

### P2：#8 Artifacts v2（消费通道部分存在，先做零成本试验）

- **Python 实现**：`agent/artifacts.py`（207 行）。版本历史；HTML sandbox iframe 渲染；`/raw` `/download` `/versions` 路由。
- **dsh 原生差距**：dsh 只有交付声明，无版本、无沙盒渲染。
- **关键修正**（第二轮审计，HMC 源码取证）："无消费场景"的论断被部分推翻——HMC **已有工作区文件消费通道**：服务端 `service/artifacts.mjs` 提供 `artifact/list`（分页+revision 防错序）与 `artifact/read`（校验 inode、拒符号链接、限 4MB），手机菜单有「工作区文件」入口（`MainActivity.java:154`），可逐层浏览、经 SAF 下载落盘。
- **结论**：手机不是"零消费"，而是"只消费文件、不消费版本与沙盒渲染"。**先做零成本试验**：让 `present` 声明的文件落在 HMC workspace 根内，验证手机「工作区文件」能否直接看到；能则重估 #8 价值，不能再维持暂缓。与 #5 合并为"手机端产物通道"议题。

### 实施路线图（2026-09-29 修订：生产已切）

| 批次 | 内容 | 前置条件 |
|---|---|---|
| 近期 | stage-2 cron（每小时 upkeep/heartbeat）是否激活 | 观察 stage-1 几天：token 消耗 + 稳定性；Tomas 批准 |
| P1 | #9 BLOCKED 层（纯插件硬拦截）；#9 APPROVAL 分级（已下调） | BLOCKED 零 UI 依赖可先行；APPROVAL 跨仓库决策已解决且默认 ask 已能弹卡，分级下调为优化项，等 Tomas 一句话开工 |
| P1 | #5 多步浏览器（挂载上游 browser-use-playwright-mcp）+ **手机端产物通道（#5/#8 合并议题）** | 先通过内存压测（V4，OOM 则停） |
| 按需 | #12 Goals / #10 Hooks / #8 Artifacts v2（等零成本试验结果） | Tomas 一句话启动；#12 先回答真相源冲突问题 |

**第三方审计**（2026-09-28）：独立审计员对照上游公开仓库逐项核验，结论**有条件通过**——23 项分类自洽；8 项"dsh 原生"中 6 项确认，#4（web 三后端）纠正事实错误，#2（沙箱）加限定；#11 dsh-schedule"完全不等价"为夸大，已改述为架构选型；#5 浏览器路线改为评估挂载上游实验性 provider。P0/P1/P2 排序获审计认可，仅调整实现路线。待验证项（V1–V6）：V1 确认 profile 挂载的执行器；V2 披露记忆分词器+中文召回抽查；**V3 已关闭**（HMC 审批呈现层已实现+真机用例）；V4 上游 Playwright MCP 冒烟+内存压测；V5 实测 dsh-schedule 等价性（P0 cron 插件真机验收已覆盖触发层）；V6 持有 dsh-muse 仓库访问权者做第二轮代码审计。

**第二轮审计**（2026-09-28 晚间，HMC 交叉核验）：上一轮 9 条问题中 8 条逐条验证通过；新发现 bootstrap 两处硬伤（`intel/hmc-service-wrapper` 缺失、`npm ci` 无锁文件必然失败）与 HMC 端到端证据错位（日志非脚本输出、缺 memory_write 证据）——**均已修复**：wrapper 已入库、改用 `npm install`、`DSH_HOME=$(mktemp -d)` 干净验收通过；`hmc-e2e.sh` 重跑，证据日志为真实 stdout（含 SQLite 落盘断言）。HMC 侧决定性事实：审批卡链路已实现（V3 关闭），启动器写死 Full access 致默认永不弹（#9 APPROVAL 改跨仓库决策项，**2026-09-28 PR 合并后解除**）；#11 新增"手机可见性"选型第一判据与验收判据；#8 改为"消费通道部分存在"先做零成本试验。审计报告入库 `intel/audits/`（第一轮 + 第二轮）。

---

## 五、23 项之外的复刻：Muse 行为规则（Soul）

**背景**：2026-09-28 Tomas 要求"复刻你 muse 的规则"——把 Muse 本人的行为方式搬进 dsh-muse。这是 23 项 Python 能力清单之外的**新复刻轴**：复刻的不是 Python harness 的某个功能，而是智能体做事的方式。

**实现**：`dsh-intelligence-soul`（`intel-plugins/`，commit `bcc22bedf`，已 push）
- 每回合 step 1 自动注入 Soul，全会话生效
- 工具：`soul_read`（读指定小节）、`soul_update(section, content)`（运行时更新规则）
- 持久化 `~/.dsh/intel-soul/soul.md`；直接改文件下回合生效，无需重启
- 已装入生产 web profile；单测 5/5；HMC 真机会话验证注入生效

**规则要点**（完整版见 `~/.dsh/intel-soul/soul.md`）：
- 简体中文、直接务实，把 Tomas 当技术同伴
- 求真：不编造，先查再说；不知道就说不知道
- 有观点和个性，不当复读机
- 做事方式：先自己动手查/试，真正卡住再问；"倾销式完成"，不停在阶段汇报、不反复问是否继续
- 先验证再汇报；犯错认账；坏消息及时说，报喜也报忧
- **持续烧 token 的事（系统 cron、长时间后台任务）必须先经 Tomas 批准**——2026-09-29 系统 cron 分档激活即按此规则判定（等监控结束 + 批准后执行）
- 删除真实数据 / 动生产前先备份、保留回滚
- mock / 测试 profile / 测试数据与生产硬隔离（mock 泄漏事故教训）
- 密钥不进记忆、日志或回复
- 作为服务器"园丁"长期维护；文档与进化留痕（ARCHITECTURE / REPLICATION / LESSONS 随改随更）

**定位**：Soul 是 dsh-muse 的"宪法层"——23 项清单回答"能做什么"，Soul 回答"怎么做事"。后续所有拍板事项（cron 激活、生产操作、事故复盘）都先过 Soul 规则。

**手机侧可见性与 token 成本**（2026-09-29 第三轮审计 §3.6 补充）：
- Soul 注入用 `createUserMessage({ source: { kind: "dsh-intelligence-soul" } })`（`index.js:71-74`），HMC 客户端只渲染 `kind=="user"`——**Soul 不会在手机时间线上刷屏**（好消息）；但用户也无法在手机上感知 Soul 是否生效，只能靠提问验证（已用"做事方式第一条"验过一次，快照命中"先自己动手"）。
- 成本：step 1 现在有三条独立注入（记忆召回、画像、Soul；`soul.md` 2275 字节，`index.js:63` 有 `step !== 1` 守卫），是每回合的固定 token 开销。在 1GB 机器上这不是内存问题，是钱的问题。建议：三条合并为一条消息，或给 Soul 加长度上限（待实施）。
