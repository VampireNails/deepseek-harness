# dsh-muse 后续优化项确认（结合 HMC 交叉核验）

- **被评估文档**：`github.com/VampireNails/deepseek-harness` → `intel` 分支 → `intel/REPLICATION.md`
- **文档自称修订点**：`b8a97c0ca`；**分支实际 HEAD**：`96a628fd50`（多一个"第三方审计 P1 修正"提交，该提交内容已体现在文档正文）
- **本地核验**：`inputs/2026-09-28/dsh-intel`，已 checkout 到 `origin/intel` = `96a628fd50`
- **HMC 侧核验基线**：`HarnessMobileClient` @ `main 224be48`（第六轮已合并，工作区干净）
- **评估时间**：2026-09-28 晚间 ｜ 基线 dsh `0.1.7-rc.1`（`46a7f68b0`）
- **证据性质**：本报告"实测"均为本机真实执行；无法执行的明确标注

---

## 结论先行（三句话）

1. **上一轮我提的 9 条问题，8 条已落实且我逐条验证通过**（含最关键的"数字与实物对齐"和"测试脚本不再删真实数据"）——这是这一版真正的进步。
2. **但"环境可复现"这条不但没修好，脚本还有两处硬伤**：`hmc-test` profile 依赖一个**仓库里不存在的目录**，且对全新目录用 `npm ci`（无锁文件**必然失败**，已实测）。所以"从零重建三 profile 已验证"这一句**不成立**。
3. **对"后续做什么"，HMC 侧给出了一个决定性事实**：手机端的审批卡链路**早就做好了、还有真机用例**（文档以为这是"未知数"），但 HMC 启动器把新会话固定成 **Full access**、且手机上**没有**改权限的接口——所以**照现在的默认部署，审批卡永远不会弹**。这一条直接改变 #9 的做法与顺序。

---

## 一、上一轮 9 条问题的落实情况（逐条验证）

| 上轮问题 | 新版实物 | 我的核验结果 |
|---|---|---|
| 单元测试数字写成 17 | 文档改为 21 用例 | ✅ **本机实测 21/21 通过、0 失败**（在新 HEAD `96a628fd50` 上重跑） |
| HMC 端到端只有自述 | 新增 `intel/hmc-e2e.sh` + 证据日志 | ⚠️ **脚本与证据对不上**，见 §2.1 |
| "24h 稳定性"名不副实 | 改名"空转存活监控"，新增 `stability-load.sh` | ✅ 改名到位；负载版**确有判据**（RSS 增长上限 100MB、错误率上限 5%、每 10 分钟真实 prompt）。小瑕疵：`stability-24h.sh` 文件头自己仍写"24h 稳定性测试" |
| 集成脚本会删真实数据 | `export DSH_HOME="$(mktemp -d)"` + `trap` 清理 | ✅ `run-integration.sh:10-11`；两处 `rm -rf` 已改为删 `$DSH_HOME/…` 下的隔离目录；依赖（python3/zstd）已在文件头声明 |
| `migrate.sh` 中文切分与线上不一致 | 改为 `re.sub(r'([一-鿿])', r' \1 ', text)` | ✅ 与线上 `spaceCjk` 等价（拉丁词保持完整）；`created_at` 已从源库读取回填；"触发器"那句错注释已改 |
| 三份文档对"23 项"三种口径 | `capability-matrix.md` 声明指向 §2 唯一权威 | ✅ 口径已归一。残留：矩阵正文仍把 #5 写"**需新写**"，与 §4 的"**挂载上游 provider**"冲突 |
| `phase2-report.md` 留着旧设计 | 已更新为真后台设计 | ✅ |
| quiet 超时缩短 / 状态前移丢数据 | `reflectTimeoutMs` 默认 `90000` 且可配；状态改 pending→成功 | ✅ 源码确认（`index.js:16`、`:84-112`），`start()` 抛错会清 pending，可重试 |
| 环境不可复现 | 新增 `intel/bootstrap.sh` | ❌ **两处硬伤**，见 §2.2 |

---

## 二、本轮新发现的问题

### 2.1【P1】HMC 端到端的"证据"不是那个脚本跑出来的

文档 §1 表格写"证据 `intel/hmc-e2e-evidence-2026-09-28.log`"，§3.2 与提交信息又写"TLS→session→**memory_write 落盘**"。我把两者并排看：

| | 证据日志（.log） | 入库脚本（hmc-e2e.sh） |
|---|---|---|
| 首行 | `=== HMC API 端到端验证 ===` | `隔离 DSH_HOME=/tmp/tmp.XXXX`（脚本第 12 行 echo） |
| 输出风格 | 手写体：`【测试2】无 token 应返回 401` / `✓ 返回 401` | 程序体：`未带 token → 401 OK` |
| 步骤 | 4 步（TLS / 401 / session-create / session-prompt） | **5 步**，第 3 步是"断言 SQLite 落盘（memory_write）" |
| 结尾 | `=== 验证完成 ===` | `=== HMC 端到端 API 级验证通过 ===` |

**两条结论**：① 这份日志与脚本的输出格式完全不匹配，**不是该脚本的原始输出**，无法据此复跑复核；② 文档反复强调的 **`memory_write` 落盘这一步，没有任何证据**（日志里没有这一节）。

**修法**：用入库脚本重跑一次、原样贴 stdout；或者把文档口径老实降到"四步 API 级"、去掉 memory_write 落盘的表述。二选一，别混着写。

### 2.2【P1】`bootstrap.sh` 无法从零重建，两处硬伤

**硬伤 A：引用了不存在的目录。**
`hmc-test` profile 的依赖写 `file:$REPO_ROOT/intel/hmc-service-wrapper`，但 `intel/` 下 16 个条目我全列过了，**没有 `hmc-service-wrapper`**（全仓 grep 该名字只命中 bootstrap.sh 自己）。

后果：**HMC 端到端所用的那个 profile，恰恰是不能被重建的那一个**。"从零 clone 重建三 profile 已验证"对第三项不成立——而第三项正是与 HMC 对接的入口。

**硬伤 B：对全新目录用 `npm ci`，必然失败。**
脚本对每个 profile 是 `mkdir -p` 新建目录 → 只写 `package.json` → 紧接着 `npm ci`。而 `npm ci` 要求目录里已有 `package-lock.json`。我做了最小实测：

```
$ mkdir -p /tmp/xxx && cd /tmp/xxx && echo '{"name":"probe","private":true,"version":"1.0.0"}' > package.json
$ npm ci
npm error code EUSAGE
npm error The `npm ci` command can only install with an existing package-lock.json …
```

**推论（重要）**：既然干净目录必然报错，那句"已验证"很可能是**在一个本来就存在的 `~/.dsh` 里跑的**——那两个 profile 目录里早就有旧锁文件，所以 `npm ci` 才没报错。这和"第六轮验收跑在自建 git 工作区里"属于**同一类问题**：在带状态的环境里验收，真正"从零"的路径没被覆盖。仓库里也没有任何 profile 级 `package-lock.json`（只有 7 个插件各有自己的锁文件）。

**修法**：① 入库 profile 的 `package-lock.json`，或把 `npm ci` 改成 `npm install`；② 补上 `hmc-service-wrapper`（或把它指向 `HarnessMobileClient/service`）；③ 验收方式改为"`DSH_HOME=$(mktemp -d)` 下跑 bootstrap"，这才叫从零。

### 2.3【P2】文档头部版本号过期

文档写"HEAD `b8a97c0ca`"，但 `intel` 分支实际 HEAD 是 `96a628fd50`；而文档正文里"第三方审计纠正 #4/#2/#11/#5"这些内容**正是 96a628fd50 这次提交带进来的**。也就是说：**文档描述的是 96a628fd50 的状态，却标着 b8a97c0ca 的号**。

### 2.4【P2】"第三方审计"这份结论没有实物

§4 末尾称"独立审计员对照上游公开仓库逐项核验，结论有条件通过"，但仓库里**没有任何审计报告或记录**（只列了 V1–V6 待验证项）。这份"有条件通过"的含金量外部无法复核。要么把这轮审计落成文件入库，要么在文档里标明审计主体与时间。

---

## 三、结合 HMC 的交叉核验：对 6 项优化项的确认与修正

这一节是本次任务的重点。我直接读了 HMC 客户端与服务端源码取证，结论如下（每条都带文件定位，可复核）。

### 3.1 关键事实：HMC 的审批卡链路**已经做好了**，而且有真机用例

文档 §4 #9 写着"APPROVAL 层依赖 dsh 审批瀑布在 **HMC 手机端**的呈现方式——**未知数，需先验证，不可假设**"。这句话现在可以关掉，呈现层是**已实现**的：

| 环节 | 位置 | 内容 |
|---|---|---|
| 弹卡 | `MainActivity.java:707-722` | 收到 `approval/request` → 弹「工具请求权限」对话框，两个按钮分别提交 `allowed-once` / `rejected` |
| 填详情 | `MainActivity.java:730-736` | 显示工具名 / 调用 ID / 详情 / 理由；详情没同步全时提示"请在 PC 核对"，正按钮置灰 |
| 时间线 | `Timeline.java:163-164` | 渲染「请求权限」/「权限结果」 |
| 后台提醒 | `MonitorEvents.java:78-81` + `BackgroundMonitorService.java:177` | 审批类提醒标题「有待处理审批」 |
| 服务端校验 | `service/security.mjs:37-72`（`ApprovalFence`） | 校验 eventId / sessionId / clientId / 单次结果；结果白名单**只接受** `allowed-once` 或 `rejected`；重复提交拒掉（`STALE_APPROVAL`） |
| 真机测试 | `ClientDeviceTest.java:63-69`、`MonitorNotificationDeviceTest.java:23`、`PhoneMonitorDeviceTest.java:129`、`MonitorLockscreenDeviceTest` | 归属与去重、通知标题、**点通知→弹出「工具请求权限」**、锁屏 SECRET |

**所以 V3（"验证 HMC 手机端 approval 呈现"）可以直接关掉**，不用再排期验证。

### 3.2 但有一个卡死条件：默认部署下审批卡**永远不弹**

同一份 HMC 代码里还有两条互相拉扯的事实：

1. `scripts/host-env.mjs:9` 把新会话权限写死为 `DSH_PERMISSION_MODE='danger-full-access'`，而且**有单测把这个行为锁住**（`tests/host-env.test.mjs:7` 断言注入后就是 `danger-full-access`）。Full access 语义 = 无需逐次审批。
2. 手机端的 RPC 白名单（`service/security.mjs:6-7`）**没有任何"改权限模式"的接口**；手机只能**显示**当前预设（`ClientDeviceTest.java:248-258` 断言界面显示"工作区可写"/"Full access"）。

**合起来就是**：四级权限模型哪怕做好了，在 HMC 的默认部署路径上，审批卡**一次都不会出现**——因为会话从一开始就是 Full access，而手机上又没有开关能把它降下来。

**由此得出两条行动结论**：

- **BLOCKED 层优先是对的，而且理由比文档写的更硬**：BLOCKED 走 `tools/pre-execute` 缝在插件里硬拒，**不依赖 dsh 的预设与审批瀑布**，因此完全不受上面这条限制。我确认了这个缝存在：`packages/core/agent-loop/src/tool-calls.ts:211,216`，且上游测试 `tests/interception.spec.ts:705` 明确把它称作"**原生插件权限模式**"。文档说的路线成立。
- **APPROVAL 层要加一个前置条件**：它不是"dsh-muse 单方面能做完的事"，而要**跨仓库决策**——是否放开 HMC 启动器里那个写死的 `danger-full-access`（那是 HMC 仓库的 `host-env.mjs`，不是手机客户端；但 Tomas 的"不修改 HMC 客户端"边界需要他确认这一条算不算在内）。建议在 §4 #9 与路线图里把它显式标成"**跨仓库决策项**"，而不是一条 dsh-muse 的开发任务。

### 3.3 #11 用户自建 cron：HMC 只看得见"同一会话里的提醒"

我核了上游 `packages/schedule/schedule/README.md`，原文两点很关键：

> Session-local durable reminders … **delivery requires a live root agent**: closed sessions keep reminders overdue until resumed. **Delivery never uses email, SMS, push, or browser notifications.**

再叠加 HMC 侧事实：提醒会作为**会话里的后续消息**出现 → 手机上走时间线可见；后台提醒**只能监控一条会话、上限 2 小时**（HMC ADR 002）。

**所以**：

- 走 **dsh-schedule** 的"用户 cron"，**恰好是手机上唯一看得见的路径**——这给文档的 (a)/(b) 两层拆分加了一条**比"省代码"更重要的理由：交付可见性**。建议把这句写进 §4 #11。
- 但必须诚实标注边界：只在**该会话活着、且处于 2 小时监控窗口内**才可能推到手机；出了窗口就退化成"回到 App 才看到"。而 `evolve` headless 跑出来的结果（Feed / joblog）**HMC 完全没有入口**——手机上看不到任何东西。
- 结论：**#11 维持 P0**，但验收判据要从"任务能建"改成"**到点能在手机上看见**"。

### 3.4 #8 Artifacts v2：文档"谁消费"的论断被部分推翻

文档 §4 #8 的依据是"Tomas 主用手机 HMC……连 Feed Web UI 都未复刻，所以没人消费"。但 HMC 里其实**已经有工作区文件的消费通道**：

- 服务端 `service/artifacts.mjs` 提供 `artifact/list`（分页 + revision 防错序）与 `artifact/read`（校验 inode、拒符号链接、限 4MB）；
- 手机菜单有「工作区文件」入口（`MainActivity.java:154`），可逐层浏览、可经 SAF 下载落盘（`:657-688`、`:360-365`）。

**所以手机不是"零消费场景"，而是"只消费文件、不消费版本与沙盒渲染"**。建议把 #8 从"无消费场景，暂缓"改为"**消费通道部分存在，先做一次零成本试验**"：让 `present` 声明的文件落在 HMC 的 workspace 根内，验证手机「工作区文件」能不能直接看到。能，则 #8 的价值判断要重估；不能，再维持暂缓。

### 3.5 #5 多步浏览器：路线成立，但产物到手机上只能"下载"

上游实验性 provider 我确认存在：`packages/experimental/browser-use-playwright-mcp`、`browser-use-runtime`、`browser-use-chrome-devtools-mcp`、`browser-use-stagehand-native` 全在。文档"挂载上游而非新写"的路线成立，硬约束仍是 1GB 内存 + OOM 即停。

补一条 HMC 侧的实情：**手机没有浏览器/截图的原生预览**——`artifact/read` 是文本（二进制走 base64，单文件上限 4MB），手机侧只能经 SAF 存盘再看。所以浏览器的产物要上手机，路径只能是"落进 workspace → 工作区文件 → 下载"。**这与 #8 共用同一条受限的展示通道**，建议在路线图里把二者合并成一个"**手机端产物通道**"议题考虑，而不是各自为战。

### 3.6 #10 Hooks / #12 Goals：维持 P2，与 HMC 无耦合

- #10 Hooks：触发源缺失的论断成立（Tomas 已决定不复刻 Gmail 等连接器），无外部事件接入就是空转，维持暂缓。
- #12 Goals：注入模式与 profile 画像注入同构（step 1 注入），HMC 不需要任何改动，维持 P2。

---

## 四、确认版后续优化清单（可直接照此执行）

| 序 | 项 | 文档原判 | **我的确认 / 修正** | 依据 |
|---|---|---|---|---|
| 1 | #11 用户自建 cron | P0 | ✅ **维持 P0**。路线确认（dsh-schedule + 中文时间解析）；**新增选型第一判据：手机可见性**；**新增验收判据：到点手机能看见**；诚实标注"超出 2 小时监控窗口即降级" | schedule README + HMC ADR 002 |
| 2 | #9 BLOCKED 层 | P1（先做） | ✅ **维持，且理由更硬**：走 `tools/pre-execute` 缝，不依赖 dsh 预设 ⇒ 不受 HMC 默认 Full access 影响 | `tool-calls.ts:211,216`、`interception.spec.ts:705` |
| 3 | #9 APPROVAL 层 | P1（后做，先验 V3） | ⚠️ **V3 关闭**（HMC 呈现层已实现 + 有真机用例）；**新增阻塞：HMC 写死 Full access 且手机无法改** ⇒ 改标为"**跨仓库决策项**"，不是 dsh-muse 的开发任务 | `host-env.mjs:9`、`host-env.test.mjs:7`、`security.mjs:6-7` |
| 4 | #5 多步浏览器 | P1（内存压测后） | ✅ 维持。上游 provider 确认存在；补充"手机只能下载看，无原生预览" | `packages/experimental/*` |
| 5 | #8 Artifacts v2 | P2（"无消费场景"） | ⚠️ **修正为"消费通道部分存在"**：先做零成本试验（present→workspace→手机「工作区文件」能否看到）再定价 | `service/artifacts.mjs`、`MainActivity.java:154` |
| 6 | #12 Goals | P2 | ✅ 维持 | — |
| 7 | #10 Hooks | P2（等触发源） | ✅ 维持 | — |
| 8 | **新增** hmc-e2e 证据对齐 | — | 🆕 用脚本重跑贴原始 stdout，或降口径；去掉无证据的 memory_write 表述 | §2.1 |
| 9 | **新增** bootstrap 可复现 | 文档自称"已完成" | 🆕 **降级为未完成**：补 `hmc-service-wrapper` + 锁文件/改 `npm install`；验收改为干净 `DSH_HOME` 下跑 | §2.2 |
| 10 | **新增** 文档版本号 | — | 🆕 头部 HEAD 改为 `96a628fd50`；第三方审计落成文件 | §2.3、§2.4 |

### 与生产切换的关系（确认）

文档的排序——"先收 24h 空转监控结果 → Tomas 决策切生产 → 再生产后推进 P1/P2"——**逻辑成立，我确认**。但按本次核验，**切生产前这一批应该包含第 8、9 两项修复**（证据对齐 + bootstrap 可复现），因为它们是"复刻是否真的完成"的验收前提，而不是功能开发。换句话说：

> **切生产前**：#11（含手机可见性判据）+ #9 BLOCKED 层 + **修 bootstrap / 补证据**（本次新增）
> **切生产后①**：#9 APPROVAL 层 —— **但先要一个跨仓库决策**（是否放开 HMC 默认 Full access）
> **切生产后②**：#5 浏览器（内存压测通过才开工）+ **手机端产物通道**（#5/#8 合并议题）
> **按需**：#12 Goals / #10 Hooks / #8 Artifacts v2

---

## 五、给 Muse 的下一步（按优先级，可直接执行）

1. **修 `bootstrap.sh` 的两处硬伤并重跑干净验收**（这是"可复现"的底线）：补 `intel/hmc-service-wrapper` 或改指向 `HarnessMobileClient/service`；用锁文件或 `npm install`；验收用 `DSH_HOME=$(mktemp -d)`。
2. **对齐 HMC 端到端证据**：用入库的 `hmc-e2e.sh` 重跑，贴原始 stdout（含时间戳与"隔离 DSH_HOME"首行）；要么就删掉 memory_write 落盘的表述。
3. **更新 §4 #9**：V3 关闭（附我给出的 HMC 文件定位作为证据），把 APPROVAL 层改标为跨仓库决策项，并写明"默认 Full access ⇒ 审批卡不弹"这条卡死条件。
4. **更新 §4 #11**：把"手机可见性"写成选型第一判据，验收判据改为"到点手机能看见"，并标注 2 小时监控窗口的边界。
5. **更新 §4 #8**：改为"消费通道部分存在"，先做 present→workspace 的零成本试验。
6. **文档收尾**：头部 HEAD 改 `96a628fd50`；`capability-matrix.md` 里 #5 的"需新写"与 §4 的"挂载上游"统一；`stability-24h.sh` 文件头跟着改名；第三方审计落成文件。

---

## 附：术语对照（人话版）

| 术语 | 一句话解释 |
|---|---|
| 审批瀑布（approval waterfall） | 智能体要执行敏感操作时，把"要不要放行"这件事抛给人来定的机制 |
| `approval/request` | 瀑布里那个"请求批准"的事件；手机收到它就该弹卡 |
| `allowed-once` / `rejected` | 用户点"批准这一次" / "拒绝"时回给主机的两个值 |
| 四级权限（READ/WRITE/APPROVAL/BLOCKED） | 把操作按危险度分四档：自动放行 / 放行但记日志 / 弹卡等确认 / 直接禁止 |
| `tools/pre-execute` | 工具真正执行**之前**的一个拦截点，插件可以在这里直接说"不行" |
| Full access（`danger-full-access`） | 权限预设的一种，等于"什么都别问我"；本项目 HMC 启动时就写死了它 |
| dsh-schedule | dsh 自带的"提醒"功能：到点往**同一个会话**里塞一条消息。会话关着就一直欠着，且不会用邮件/推送 |
| profile（配置档） | 一套"装哪些插件、开哪个端口"的预设组合，如 `web-in​tel`（网页常驻）、`evolve`（定时任务）、`hmc-test`（HMC 对接） |
| bootstrap | 从零把上面那套 profile 重新装出来的脚本 |
| `npm ci` vs `npm install` | 前者按锁文件"照单精确安装"、必须有锁文件；后者会解析依赖并生成锁文件 |
| 锁文件（package-lock.json） | 记录每个依赖的确切版本，保证不同机器装出一样的东西 |
| headless | 没有界面、跑一次就退出的运行方式，用来做定时任务 |
| SAF | 安卓系统的"文件选择/保存"标准界面 |
| V3（待验证项） | 文档自己列的待验证清单里的第 3 条，内容正是"手机端审批卡长什么样" |
