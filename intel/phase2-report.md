# 阶段 2 报告：quiet-moment 复盘插件

日期：2026-09-28（2026-09-28 修订：真后台改造）
插件：`intel-plugins/dsh-intelligence-quiet`（`dsh-intelligence-quiet@0.1.0`）

## 做什么

`agent/turn-stopping` 挂载点监听。每轮对话结束时：
1. 冷却检查（默认 5 分钟，`~/.dsh/intel-quiet/state.json`）
2. 从 `ctx.sessionQuery.readSession` 取本 turn 真实用户消息拼 transcript（跳过插件注入的 system-reminder）
3. 内容去重（sha256 与上次复盘比对；另有 `pendingHash` 防并发重复触发）
4. 起一次性复盘子智能体（`spawn` provider，`toolFilter: allow=["memory_write"]`，`maxDepth: 1`，`reflectTimeoutMs` 默认 90 秒可配），复用阶段 1 的记忆接口写长期记忆

## 关键实现决策

- **真后台（fire-and-forget）**：复刻 Python 版 daemon 线程语义。`turn-stopping` handler 不 `await run.result`，只等 `start()` 发布成功；复盘在后台跑，`run.result.then()` 做清理。回合收尾不再被复盘阻塞（3 秒 vs 旧 90 秒）。
- **状态机**：`pendingHash` → `start()` 成功 → 落 `lastAt`/`lastHash` 并清 `pendingHash`；`start()` 抛错时清 `pendingHash`，冷却期过后可重试，不静默丢数据。
- **防递归**：prompt 带 `[quiet-moment-reflection]` marker，transcript 含 marker 直接跳过；子智能体 `maxDepth: 1` 不再派生。
- **步数上限**：dsh 无 maxSteps 语义，用 `toolFilter` 只给 `memory_write` 实现等价约束（子智能体只能：读 prompt → 调 1-2 次工具 → 结束）。
- **`await start()` 坑**：`ctx.subagents.start()` 只表示发布成功；headless 一次性运行退出时会取消未完成的后台子智能体（实测复现过）。
- **适用边界（重要）**：真后台仅在**常驻 profile**（`web-intel`，宿主长期存活）成立；headless 一次性运行（`evolve`）不得依赖 fire-and-forget 的后台任务落地。`evolve` 的 bundle 清单不含 quiet（见阶段 3 报告），故该坑不落在实际部署面上。
- **`listEvents` 只有元数据**：拿不到消息内容，改用 `readSession` 快照。
- **mock 序列要给标题请求留位**：dsh 每会话有一次 session-title LLM 调用，会消耗 mock 序列中的一个行为；测试序列用 `success,tool_call_success,tool_call_success,success`。
- **`ctx.effect` 包裹**：`ctx.effect(() => ctx.on(...))`，插件卸载时 listener 自动回收。

## 验收（2026-09-28，全部 mock LLM，零真 key）

1. 用户说"我昨天买了一辆红色自行车" → 主回合结束 → turn-stopping 触发 → 复盘子智能体（独立 session，`origin: subagent`）→ 真实调用 `memory_write` → SQLite 落盘 `{"text":"用户买了一辆红色自行车","kind":"fact"}`。
2. 冷却：5 分钟内第二轮对话零复盘日志、无新记忆写入。
3. `web-intel` 干净启动：`:3080`，`/` 返回 401，无 `failed to import` / `plugin tree failed`。
4. 语法检查：`node --check` 全过；`import()` 实测 OK。
5. 真后台：主回合约 3 秒结束（旧同步实现最长 90 秒阻塞）；`start()` 失败场景下 `pendingHash` 被清理，不丢数据（单元测试覆盖）。

## 清理

- 测试记忆已删（memories=0）、`intel-quiet/state.json` 已删、mock 测试会话目录已删。
- `web-intel` 临时进程已停，3080 已释放。

## 未做

- 向量检索（阶段 1 已否决，延续 FTS5）。
