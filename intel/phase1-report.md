# 阶段 1 报告：记忆插件 dsh-intelligence-memory

日期：2026-09-28 ｜ 分支：`intel` ｜ dsh 0.1.7-rc.1 ｜ profile：`web-intel`（开发）/`headless`（测试）

## 结论

**通过。** 记忆插件已按任务书 §4.1 完整实现并真测：
`agent/pre-step` 自动召回注入 + `memory_write` / `memory_read` / `memory_search` 三工具，
存储在 `~/.dsh/intel-memory/memory.db`（SQLite + FTS5），零真 key 自动化测试全过。

## 1. 检索方案选型（任务书 §4.2 实测后决定）

| 方案 | 实测 | 结论 |
|---|---|---|
| A. transformers.js + `Xenova/bge-small-zh-v1.5`（ONNX） | 模型可下载；加载+3 条中文 embedding，**峰值 RSS 277104 KB（≈271MB）**，5.9 秒 | ❌ 否决 |
| B. SQLite FTS5（node:sqlite 内建） | 中文按字切分后 6/6 召回正确；内存增量可忽略 | ✅ 采用 |
| C. 复用 Python embedding sidecar | — | ❌ 任务书明令禁止（最终 Python 必须可停） |

否决 A 的算术：dsh web 本体 144MB + 向量增量约 230MB ≈ 374MB > 迁移期可用内存约 355MB，
Python 栈并存时必爆。检索做成**可替换契约**（`lib/retriever.js`：`add/get/search/close`），
Python 退役、内存宽裕后可实现同接口的向量检索器直接替换，不动插件主体。

FTS5 中文细节：默认 unicode61 分词器把连续汉字当一个 token，
故入库前按字加空格（"我喜欢"→"我 喜 欢"），查询按字 OR + BM25 排序，
再加 JS 二次重叠过滤（至少共享 2 个不同汉字，英文词命中 1 个即算），
"今天天气怎么样"这类无关查询返回空，无误召回。

## 2. 插件实现

位置：`intel-plugins/dsh-intelligence-memory/`（树外插件，不碰 `packages/`）

| 文件 | 内容 |
|---|---|
| `index.js` | `name` / `inject=["agents","sessionProjections","tools"]` / `Config`（schemastery）/ `apply` |
| `lib/store.js` | `openStore(dataDir)`：建表 + FTS5 虚表 |
| `lib/retriever.js` | `FtsRetriever`：检索契约的 FTS5 实现 |
| `lib/tools.js` | 三个 `defineTool` 定义 |
| `package.json` | 依赖包内自带（阶段 0 教训）；`dsh.bundle.patch` 声明 |
| `cordis.patch.yml` | `insert: [{id, name}]` |

三处注册全部走 `ctx.effect`（可卸载回卷）：
1. `ctx.sessionProjections.register({key:"intelMemory",...})` —— 记录本 turn 是否已注入，用于节流
2. `ctx.on("agent/pre-step", handler, {prepend:true})` —— 仅 step 1；取**真人**最后一条 user 消息做 query（`source.kind==="user"` 过滤 system-reminder）；命中则 `createUserMessage` 注入，`source: {kind: "dsh-intelligence-memory", form: "snapshot"}`
3. `ctx.effect(function*(){ yield ctx.tools.register(...) })` —— 三工具

存储：`process.env.DSH_HOME || ~/.dsh` 下的 `intel-memory/`，**不进 workspace**（任务书硬约束）。

## 3. 踩坑记录（记入 LESSONS）

1. **schemastery 没有 `.optional()`**：`Config` 字段用 `.default()`（`z.string().default("")` 等），否则 `failed to import`。
2. **缺 `dsh.bundle` 声明进不了 dump-config**：`package.json` 必须有 `"dsh": {"bundle": {"patch": "./cordis.patch.yml"}}`，否则 `dsh plugin add` 只装成普通依赖，不进 `dsh.profile.bundles`。
3. **pre-step 的 query 取错**：`decision.messages` 里最后一条 user 消息可能是 system-reminder（别的插件注入的），必须按 `source.kind === "user"` 过滤，否则召回查的是提醒文本，永远 0 命中。
4. **FTS5 中文分词**：unicode61 不切分汉字，入库/查询都要先按字加空格（见 §1）。
5. **`pkill -f` 会误杀自己的 SSH 会话**：pattern 出现在 ssh 命令行里。杀进程一律先 `pgrep` 确认，或用 PID。

## 4. 验收（部署铁律）

| 步骤 | 结果 |
|---|---|
| 语法检查 | `node --check` 4 个文件全过；插件可独立 `import` |
| 服务 active | `dsh --profile web-intel` 监听 127.0.0.1:3080 |
| 健康检查 | 启动日志干净，无 `failed to import` / `plugin tree failed`；`/` 返回 401（鉴权正常） |
| 真实功能验收 | 见 §5，全部基于 **dsh mock LLM server**（`packages/test-support/llm-mock-server`，`node src/bin.ts` 直接跑，零依赖），**零真 key** |

内存：带插件的 dsh web RSS **150596 KB（≈147MB）**，相对基线 144MB 增量约 3MB，可忽略。

## 5. 自动化测试（mock LLM server，不烧真 key）

- **TEST 1（写入）**：mock `--sequence tool_call_success,success --tool-name memory_write` → 真工具执行 → `~/.dsh/intel-memory/memory.db` 落盘 `{"id":1,"text":"我喜欢喝美式，不加糖","kind":"preference"}` ✅
- **TEST 2（召回）**：新会话问"我喜欢喝什么？" → 会话 JSONL 出现 `source.kind="dsh-intelligence-memory"` 的注入消息 ✅
- **TEST 3（进模型上下文，字节级）**：HTTP 代理抓包，发往模型的请求正文含 `"以下是与当前问题相关的长期记忆（系统自动召回，仅供参考）：\n1. 我喜欢喝美式，不加糖"` 及 source 标记 ✅

## 6. 23 项能力对照表

见 `intel/capability-matrix.md`（任务书 §4.3/§8 交付物）。核心结论：

- **8 项可直接用** dsh 原生包：#1 agent loop、#2 shell（多沙箱）、#3 文件、#6 子智能体、#17 会话、#18 上传、#19 MCP、#4 搜索（单后端，基本等价）
- **7 项部分有但语义不等价**，需补：#8 产物（无版本/沙盒）、#9 审批（无四级）、#10 Hooks（无 inbox 轮询）、#11 定时（提醒≠后台执行）、#12 Goals（同会话≠跨会话）、#20 推送（入站≠出站）、#21 joblog（三件套）
- **8 项完全没有**，需新写：#5 浏览器工具、#7 语义记忆（本阶段已做）、#13 复盘、#14 进化、#15 心跳、#16 Feed、#22 画像、#23 简报
- **dsh.md 评估修正 3 处**：安装版无 `browser-use` 包；搜索后端只有 deepseek 一家；subagent 后端只有 in-process

5 项 dsh 完全没有的（7/13/14/16/22）与任务书预期一致；3 项加固（joblog 三件套、步骤用尽不判死、Heartbeat 分块）dsh 均无等价物。

## 7. 下一步

阶段 2：quiet-moment 复盘插件（`agent/turn-stopping`，后台 ≤5 步，5 分钟冷却去重）。
阶段 1 无遗留问题。
