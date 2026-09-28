# dsh-muse 复刻总览（2026-09-28 修订版）第三方审计报告

**审计对象**：《dsh-muse 复刻总览（2026-09-28 修订版）》文档（commit b8a97c0ca）
**审计依据**：https://github.com/deepseek-ai/deepseek-harness 公开仓库 master 分支代码与文档（2026-09-28 读取）
**审计员**：独立第三方技术审计（subagent）
**审计边界**：仅核验文档中关于 dsh 上游能力的断言；Python 版 deepseek-harness 的断言（custom_cron.py 229 行、permissions.py 四级等）与 dsh-muse 自有插件的实现细节、本地生产状态（:8080/:3080 端口、24h 监控、HMC 配对）不在公开仓库可验证范围内，标记为 `[本地断言，未验证]`。

---

## 一、总体结论：有条件通过

文档的核心结论成立：**23 项的去向划分（8 dsh 原生 + 8 新写插件 + 6 未排期 + 1 不复刻 = 23）在算术与分类上自洽；8 项"dsh 原生"中有 7 项经仓库代码确认准确；5 项"dsh 缺乏"断言中有 4 项准确、1 项（schedule）被夸大。**

但存在 **2 处事实性错误**（`wrong`）与 **3 处实质性夸大/表述偏差**（`questionable`），其中 dsh-schedule 的语义描述错误直接影响了 §3.1 架构决策与 §四 P0 路线的正当性论述，浏览器路线的差距分析遗漏了上游已有的实验性 Playwright 提供者。在修正这些表述并补足 3 个验证动作后，文档可作为可信的复刻口径。

| 审计维度 | 结果 |
|---|---|
| 算术与完整性（8+8+6+1=23；A1–A7 无重叠；分类无错位） | ✅ 通过 |
| "dsh 原生"8 项描述准确性 | 6 verified，1 wrong（#4），1 questionable（#2） |
| "dsh 缺乏"5 项断言（#5/#8/#9/#10/#11/#12 中的缺乏侧） | 4 verified，1 questionable（#11 schedule，被夸大） |
| §四 优先级排序 | 基本同意，P0/P1 路线需调整（见 §四） |
| "智能层复刻已完成"的诚实性 | 有条件成立（实现完成、生产验证中；文档自身已披露未激活项，见 §五） |

---

## 二、逐项核验结果

### 2.1 "dsh 原生"8 项核验

| # | 文档断言 | 审计结论 | 仓库依据 |
|---|---|---|---|
| 1 | dsh-agent-loop（DeepSeek Harness 专用） | **verified**（措辞小瑕疵） | `packages/core/README.md`：`agent-loop/` — "The default agent driver: creates agents and runs the turn and step lifecycle"（`ctx.agentLoop`）。"DeepSeek Harness 专用"表述别扭（它就是 harness 自带的默认 loop，并非针对 DeepSeek 模型），但事实无误。 |
| 2 | dsh-tool-bash（多一层沙箱，超集） | **questionable** | 沙箱能力真实存在：`packages/sandbox/README.md` — 进程约束 seam，Linux bwrap→Landlock、macOS Seatbelt、Windows restricted token，三档模式 `read-only` / `workspace-write` / `danger-full-access`；`packages/shell/README.md` 有 `bash-sandbox` 执行器变体。但**沙箱是按 composition 可选挂载的**（"Exactly one executor implementation is mounted per composition…pick the sandboxing variant when commands need file-level confinement"），`tool-bash` 本身默认可跑在无沙箱的 `bash-local` 上。"多一层沙箱"仅在 dsh-muse 的 profile 挂载了 `bash-sandbox` 时成立，文档未说明其 profile 的挂载选择。"超集"（相对 Python shell_exec）无法仅凭 dsh 侧验证。 |
| 3 | dsh-tool-fs（多搜索/替换，超集） | **verified** | `packages/fs/README.md`：`tool-fs`（read/read_image/write/edit）+ `tool-fs-search`（glob/grep，ripgrep 后端）+ `tool-str-replace-editor`（view/create/str_replace/insert）。"多搜索/替换"准确。"超集"相对 Python 文件读写方向正确（Python 侧未验证）。 |
| 4 | dsh-tool-web（单 DeepSeek 后端，基本等价） | **wrong** | `packages/web/README.md`："Deployments can choose **Exa, Perplexity, or DeepSeek** for search" — 三个搜索后端：`web-search-exa/`、`web-search-perplexity/`、`web-search-deepseek/`（+ `web-fetch-http/` 匿名抓取）。"单 DeepSeek 后端"是对上游的事实性误述；若 dsh-muse 部署只配了 DeepSeek，应写"部署选用 DeepSeek 后端"，而非归因于 dsh 原生能力。 |
| 6 | dsh-tool-subagent（in-process 驱动） | **verified** | `packages/subagent/README.md`：`subagent-in-process-driver`、`subagent-spawn-in-process`（fresh in-process child）、`subagent-fork-in-process`（history-seeded），另有 ACP/Codex/Claude Code/dsh-sdk 等 out-of-process 后端。 |
| 17 | dsh-session（多历史检索，超集） | **verified** | `packages/session-query/README.md`：统一会话历史检索服务 + `session-query-sqlite`（SQLite FTS5 全文索引）+ `tool-session-query`（5 个模型工具：search/trace/read）。"多历史检索"准确。 |
| 18 | dsh-attachment | **verified**（有细微限定） | `packages/attachment/README.md`："durable **image** attachments…only raster image formats are supported"，存储于 DSH_HOME，重启保留。原生能力属实，但**仅支持图片**，若 Python 版"文件上传"是通用文件上传，则此处存在能力口径差异，文档未披露。 |
| 19 | dsh-mcp-client（多 resources，超集） | **verified** | `packages/mcp/README.md`："connect external MCP servers, call their tools, **and read their resources**"；`mcp-client/` + `mcp-resources/`（跨 server 的资源发现/读取工具）。 |

### 2.2 "dsh 缺乏"断言核验（对应未排期 6 项中的 dsh 侧描述）

| # | 文档断言 | 审计结论 | 仓库依据 |
|---|---|---|---|
| 11 | dsh-schedule 是"提醒"语义，不是后台执行——完全不等价 | **questionable（夸大）** | `packages/schedule/README.md` 与 `packages/schedule/schedule/README.md`：dsh-schedule 确为 reminder 形态（"reminders as follow-up messages in their original Session"），但它**是定时触发 + 唤醒 agent 执行后续 turn 的机制**：支持 `cron`（五字段 Vixie cron + IANA 时区）、daily/weekly/every；"The Host restores a cold Session when delivery is due"；到期经 `followup()` 入 inbox 并启动"一个普通的后续轮次"。第三方教程实测：到期提醒会产生"a whole new turn's worth of output…the model replying after seeing a user message shaped like [reminder]"。因此"不是后台执行——完全不等价"是**二值化夸大**。真实差距是三点：(a) 任务绑定到原 Session（reminder 形态），非脱离会话的 headless 后台任务；(b) 交付要求 Host Web Session controller，"cannot be mounted alone in a headless or SDK-only composition"；(c) 无中文时间解析。晨间简报（08:00 cron → 唤醒 → web_search+画像+记忆→Feed）在 dsh-schedule 语义内**完全可实现**，文档用"语义不等价"作为弃用理由不成立，应改述为"架构选型：选用系统 cron + headless 以获得脱离 Host 进程的独立执行"。 |
| 9 | dsh 有审批瀑布，无四级 | **verified** | `packages/interaction/README.md`：`user-approval` — "Asks composed answerers for one-shot allow/reject decisions and fails closed"；`user-questions` — "scoped answerer waterfall"（即"审批瀑布"）。`packages/interaction/permission-presets/README.md`：权限模型是**两旋钮组合**——sandbox 模式（read-only/workspace-write/danger-full-access）× approval 策略（ask/never/auto），无 READ/WRITE/APPROVAL/BLOCKED 四级格、无 BLOCKED 正则硬拦截层。文档描述准确。 |
| 12 | dsh goal 是同会话驱动，语义不同 | **verified** | `packages/goal/README.md`："lets one agent session pursue a durable completion objective…**Each session has only one current goal**, and that goal records completion state rather than scheduling work"。与 Python 版多目标 goals.json（跨会话目标库）语义确实不同。 |
| 10 | dsh 有入站 webhook，无 inbox 轮询 | **verified**（有术语注记） | `packages/webhook/README.md`：入站 webhook 存在（"receives authenticated provider events and runs trusted programmatic rules…create an ordinary root Session"）。`packages/hooks/README.md`：dsh 的 hooks 是 Claude Code/Codex 的 shell-hook 桥（hooks.json 生命周期钩子），**与 Python hooks.py 的 webhook+inbox 文件轮询是同名异物**，文档未混淆两者，正确。dsh 确无"每 20 秒 inbox 文件轮询"机制（注：dsh agent 核心另有内存态 `Agent.inbox` 待办消息队列，`agent/inbox/*` 事件，与文件轮询无关）。 |
| 8 | dsh 只有交付声明，无版本/沙盒 | **verified** | `packages/deliverables/README.md`：`tool-present`（声明交付文件）+ `workspace-changes`（基于 git 快照的每 turn 变更记录）。无版本历史、无 HTML sandbox iframe、无 /raw /download /versions 端点。 |
| 5 | dsh 安装版无多步浏览器等价物 | **verified（范围限定下）/ 路线有遗漏** | 现状部分属实：`packages/experimental/README.md` 明确 shipped 安装的可选 bundle 只有 Agent Teams、voice input、Auto review、Schedule 四个，`browser-use-playwright-mcp`（Playwright browser tools over MCP）、`browser-use-chrome-devtools-mcp`、`browser-use-stagehand-native` 属"libraries or explicit compositions"，**默认安装未挂载**。但 §四 P1 的"路线：Playwright 插件"**遗漏了上游已存在的 `browser-use-playwright-mcp` + `browser-use-runtime`（Session-owned browser resources）**，差距评估"6 项中差距最大"和"从零写插件"的隐含前提不成立——现实路线应是"评估挂载上游实验性 provider"，而非新写 Playwright 插件。 |

### 2.3 算术与完整性

- **8+8+6+1=23 自洽** ✅：逐行清点 §2 表——dsh 原生（#1,#2,#3,#4,#6,#17,#18,#19）=8；新写插件（#7,#13,#14,#15,#16,#21,#22,#23）=8；未排期（#5,#8,#9,#10,#11,#12）=6；不复刻（#20）=1。合计 23，与表行数一致。
- **7 项范围外（A1–A7）与 23 项无重叠** ✅：Gmail 等外部连接器、人物/群组档案、onboarding、图片生成、TTS、预订/快递追踪、Tailscale Serve，均未出现在 23 项表中。注：A2"人物/群组档案"与 #22"结构化用户画像"概念相邻，但文档已用"仅用户本人"明确切分，不构成重复计数。
- **去向分类无错位** ✅：8 个"新写插件"项（记忆、quiet、进化、Heartbeat、Feed、joblog、画像、晨报）在上游 packages/ 中均无对应原生组（无 memory/knowledge/feed/quiet/evolution/profile 组；`context/` 仅为 workspace instructions/time context/references，无用户画像；`session-query` 是会话内历史检索非跨会话记忆），划为"新写插件"正确；8 个"dsh 原生"项均有对应原生包。未发现应属新写插件却被划入 dsh 原生的项。

---

## 三、发现的问题（按严重程度排序）

### P0
**（无 P0 级事实错误。以下 P1 为需修正后文档方可定稿的项。）**

### P1
1. **#4 dsh-tool-web"单 DeepSeek 后端"事实错误**（§2.1）。上游 `packages/web/` 有 Exa / Perplexity / DeepSeek 三个搜索后端（`packages/web/README.md`）。修正：将"单 DeepSeek 后端，基本等价"改为"dsh-tool-web（Exa/Perplexity/DeepSeek 三后端可选；dsh-muse 部署选用 DeepSeek）"。若 dsh-muse 实际只验证了 DeepSeek 后端，应如实标注验证范围。
2. **#11 dsh-schedule"完全不等价"夸大，动摇 §3.1 弃用决策的论述**（§2.2）。dsh-schedule 支持 cron 表达式、可恢复 cold session、到期触发模型后续 turn；与 Python custom_cron 的真实差距是"会话绑定的 reminder 交付 vs 脱离会话的 headless 后台执行"，而非"提醒 vs 执行"的二分法。修正 §3.1 表格与 §四 P0 的措辞，并重新论证"系统 cron + headless"路线的选择依据（Host 进程独立性、headless 可执行任意任务、故障隔离），而非建立在错误的"语义不等价"上。注意该修正**不推翻** P0 优先级本身（见 §四）。
3. **§四 P1 #5 浏览器路线遗漏上游已有能力**（§2.2）。上游 `packages/experimental/` 已有 `browser-use-playwright-mcp`、`browser-use-chrome-devtools-mcp`、`browser-use-stagehand-native` 与 `browser-use-runtime`（Session-owned browser resources，会话级浏览器资源生命周期——恰好支撑"按需启动"的内存策略）。"Playwright 插件"路线应改为"评估挂载上游实验性 browser-use provider（Playwright MCP）"，成本/风险评估需下调（从"高。OOM 则停"调整为"中：实验性包无稳定性承诺 + 仍需内存压测"）。

### P2
4. **#2"多一层沙箱"表述需加限定**（§2.1）。沙箱是按 composition 选装的执行器变体（`bash-sandbox`），非 `tool-bash` 默认行为。文档应注明 dsh-muse 的 web-intel/evolve profile 实际挂载的是哪个执行器，否则"超集"无从谈起。
5. **#18 dsh-attachment 仅支持图片**（§2.1）。上游 attachment 明确"only raster image formats are supported"。若 Python 版文件上传支持通用文件，此处存在能力口径收窄，文档应披露。
6. **#7 记忆检索 FTS5 的中文分词器未交代**（技术路线风险，见 §五）。上游 dsh 自身 FTS5（`session-query-sqlite`）使用 unicode61 分词器；社区实测（dsh-knowledge-sqlite 等插件的 EXPERIMENTS 数据）显示 unicode61 对中文子串查询召回接近 0%，生态内严肃记忆插件均自带 CJK bigram/trigram 或 Intl.Segmenter 分词器。文档只写了"SQLite FTS5"而未说明分词方案——这是中文检索质量的决定性变量，需补披露（见 §五验证动作）。
7. **#1"DeepSeek Harness 专用"措辞**。`agent-loop` 是 harness 默认 loop，并非"DeepSeek 模型专用"。小瑕疵，建议改为"dsh 原生默认 agent loop"。

---

## 四、对 §四 优先级的独立意见

| 项 | 文档定级 | 独立意见 |
|---|---|---|
| P0 #11 用户自建 cron | P0（最先做） | **同意 P0，调整路线**。用户价值判断成立（手机一句话建定时任务；晨报已写好待激活）。但路线不应是唯一的"系统 cron + headless"：dsh-schedule 原生支持 cron/daily/weekly、任务管理工具（schedule_create/list/update/delete）、cold session 恢复，对"提醒用户/在用户会话里执行"类任务是更原生的承载。建议拆两层：(a) 用户 cron 的**触发与交付**优先评估复用 dsh-schedule（省掉约 200 行中文时间解析之外的全部调度器代码）；(b) 仍需 headless 后台执行的任务（如进化、joblog 整理）保留系统 cron + headless。中文时间解析（~200 行 JS）两种路线都需要。 |
| P1 #9 审批 BLOCKED 层先行 | P1（分两层） | **同意**。dsh 已有 sandbox 三档（文件效应硬约束）+ approval 瀑布；Python 式 BLOCKED（高危 shell 正则硬拒）是 dsh 没有的增量层，纯插件、零 UI 依赖，先做合理。APPROVAL 层依赖 HMC 手机端呈现上游 approval waterfall——该呈现是否存在**未验证**（见 §五）。 |
| P1 #5 多步浏览器 | P1（先过内存压测） | **同意 P1，调整路线与成本评估**。内存论证基本符合实际（headless Chromium 常驻约 150–300MB；1GB 机器上与 dsh host、web-intel、evolve 常驻进程共存时，按需启动是正确策略；上游 `browser-use-runtime` 的 Session-owned 资源模型正好支撑此策略）。但不应"新写 Playwright 插件"，而应评估挂载上游实验性 `browser-use-playwright-mcp`。风险从"高"下调为"中"（实验性包无稳定性承诺仍是真风险）。 |
| P2 #12 Goals | P2 | **同意 P2，附加告诫**。dsh 原生 goal 已提供同会话 durable objective（跨重启/resume/fork）；若再写一个多目标插件，会出现"两套目标系统"的真相源冲突。建议实现前先回答：多目标是否可落在 memory/profile/Feed 的组合上，而非另起 goal 服务。 |
| P2 #10 Hooks | P2（暂缓） | **同意暂缓**。触发源论证成立：#20 出站推送与 A1 外部连接器均已明确不做，inbox 文件轮询确无上游事件可消费；且 dsh 入站 webhook（`packages/webhook/`，verified external events）已覆盖"外部系统回调"场景，真有触发源时应优先走 webhook 而非轮询。 |
| P2 #8 Artifacts v2 | P2（暂缓） | **同意暂缓**。"谁消费"的问题真实：Feed Web UI 未复刻、用户主用手机 HMC，版本化 artifact 无呈现面。 |

**优先级总体**：P0（cron）> P1（审批 BLOCKED、浏览器）> P2（三项暂缓）的排序合理。唯一实质调整是 P0 与 P1-浏览器的**实现路线**，而非顺序。

---

## 五、技术路线风险与建议补充的验证动作

### 技术路线风险评估（审计任务第 6 项）
1. **系统 cron + headless 替代 APScheduler**：可靠性上 cron 优于进程内 APScheduler（Host 崩溃不丢任务），但运维语义不同——每次触发冷启动一个 `dsh --profile evolve` headless host（Node 启动秒级 + 模型调用），无 APScheduler 的 misfire/coalesce 语义；静默失败需外部可观测性（恰好 joblog 插件可覆盖，但 joblog 本身也可能由同一 cron 驱动——注意自举依赖）。风险：中低，路线可行，需补失败告警。
2. **quiet 的 fire-and-forget 真后台**：dsh 有 `ctx.jobs`（`packages/jobs/`：后台任务注册表，completion delivered in-session），但其完成通知会污染用户会话——不用它有合理性。真后台（detached 子进程/线程）在 Cordis 插件架构下的已知坑：插件卸载/Host 重启时无生命周期管理（孤儿进程）、不可观测/不可取消、无跨重启恢复。文档提到"真后台修复"说明已踩过坑 `[本地断言，未验证]`。风险：中。建议：用被监督的子进程 + 启动时回收孤儿，或评估 `ctx.jobs` + 抑制通知的可行性。
3. **FTS5 替代向量的中文检索损失**：1GB 内存否决 ONNX（374MB）的决策合理；但**损失大小取决于分词器**，文档未披露。用 stock unicode61 时中文子串/短语召回会很差（社区实测 hard 查询接近 0 召回）；用 CJK bigram/trigram + BM25 时关键词召回可接受，但语义/改写查询仍明显弱于向量。风险：中。缓解：Intl.Segmenter / bigram 分词（零新增内存）、查询改写（L1 expansion，社区 dsh-knowledge-sqlite 实测可将 hard 查询召回从 7% 提到 21–64%）、或将向量检索降级为可选的云端 embedding（但用户要求本地/内存约束，需用户决策）。

### 建议补充的验证动作
1. **V1**：确认 dsh-muse 各 profile（web-intel、evolve）挂载的 shell/fs 执行器是否为 sandbox 变体（`bash-sandbox`/`fs-sandbox`），否则修正 #2"多一层沙箱"断言。（对应问题 P1-4）
2. **V2**：披露 `dsh-intelligence-memory` 的 FTS5 分词器实现（unicode61 / 自定义 CJK bigram-trigram / Intl.Segmenter），并跑一组中文记忆召回抽查（10–20 条真实记忆，关键词 + 改写查询各半）。（对应问题 P2-6）
3. **V3**：验证 HMC 手机端对 dsh approval waterfall（`user-approval` answerer）的呈现——这是 §四 P1 #9 APPROVAL 层的前置条件，当前为未验证假设。（§四）
4. **V4**：在隔离 profile 挂载上游 `@deepseek-ai/dsh-experimental-browser-use-playwright-mcp` 做一次冒烟 + 内存压测（按需启动/会话结束释放），用实测数据替代"Playwright 插件"的估算。（对应问题 P1-3）
5. **V5**：对 dsh-schedule 做一次等价性实测（cron 到期 → cold session 恢复 → 后续 turn 执行），用实测结论重写 §3.1"弃用"论述；若实测通过，P0 用户 cron 可复用其触发层。（对应问题 P1-2）
6. **V6**：文档中所有 `[本地断言]`（21 单元测试、24h 监控、HMC 配对、真后台修复、quiet turn-stopping 语义）建议由持有 dsh-muse 仓库访问权的一方做第二轮代码审计，本次审计因无仓库访问权未能覆盖。

### 诚实边界评估（审计任务第 5 项）
"智能层复刻已完成"的口径**有条件成立**：文档自身的 §2 表将 #23 晨报标注为"未激活"、§3.4 披露"24h 空转存活监控中""cron 未激活""HMC 只到 API 级验证"，没有把"计划中"写成"已完成"。但"完成"一词易被误读为"生产就绪"；建议改为"**智能层实现完成，生产验证中**（监控/HMC 端到端/cron 激活尚未关闭）"，并在 §一结论表增加"生产验证"行。另："单元测试 21 用例 21 通过"为本地断言，本次未验证。

---

## 六、Sources

**公开仓库依据**（2026-09-28 读取，均为 `verified live` 的仓库页面/原始文件）：

1. 仓库与包分组总览：https://github.com/deepseek-ai/deepseek-harness ；https://github.com/deepseek-ai/deepseek-harness/tree/master/packages ；`packages/README.md`（分组定义：goal/schedule/hooks/deliverables/webhook/interaction/session-query/mcp/browser-use 等）
2. shell/sandbox：https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/shell/README.md ；https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/sandbox/README.md
3. fs：https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/fs/README.md
4. web（三后端）：https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/web/README.md
5. mcp：https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/mcp/README.md
6. session-query：https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/session-query/README.md
7. subagent：https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/subagent/README.md
8. attachment（仅图片）：https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/attachment/README.md
9. core（agent-loop）：https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/core/README.md
10. schedule：https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/schedule/README.md ；https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/schedule/schedule/README.md ；官方子系统文档镜像 https://github.com/deepseek-ai/deepseek-harness/blob/HEAD/docs/subsystems/schedule.md ；行为教程 https://github.com/richardds5/learn-deepseek-harness/blob/HEAD/docs/en/s28-jobs-and-schedule.md
11. goal：https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/goal/README.md
12. hooks/webhook：https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/hooks/README.md ；https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/webhook/README.md
13. deliverables：https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/deliverables/README.md
14. interaction/approval/permission-presets：https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/interaction/README.md ；https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/interaction/permission-presets/README.md
15. browser-use/experimental：https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/browser-use/README.md ；https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/experimental/README.md
16. jobs（quiet 路线背景）：https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/jobs/README.md

**生态佐证**（非官方，FTS5 中文分词问题，`index` 来源，已在正文中标注）：
- https://github.com/lkq667/metamath-harness/blob/HEAD/plugins/dsh-knowledge-sqlite/README.md （unicode61 中文子串召回≈0 的实测）
- https://github.com/the-thinker0/dsh-memory-search-plus ；https://github.com/johnxu22786/docindex/blob/HEAD/README.md ；https://github.com/chenzheshushi-commits/dsh-evolve （CJK bigram/Intl.Segmenter 方案）

**工作笔记**：`notes/note-01-packages.md`、`notes/note-02-native-tools.md`、`notes/note-03-gaps.md`、`notes/note-04-fts5-chinese.md`

**未验证项**：Python 版 deepseek-harness 的所有断言（custom_cron.py、permissions.py、agent/goals.py、hooks.py、artifacts.py 的行数与行为）；dsh-muse 自有 8 个插件的实现；§3.4 本地生产状态；21 单元测试。需持有对应仓库访问权者补审（V6）。
