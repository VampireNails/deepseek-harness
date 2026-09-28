# 阶段 2 报告：quiet-moment 复盘插件

日期：2026-09-28
插件：`intel-plugins/dsh-intelligence-quiet`（`dsh-intelligence-quiet@0.1.0`）

## 做什么

`agent/turn-stopping` 挂载点监听。每轮对话结束时：
1. 冷却检查（默认 5 分钟，`~/.dsh/intel-quiet/state.json`）
2. 从 `ctx.sessionQuery.readSession` 取本 turn 真实用户消息拼 transcript（跳过插件注入的 system-reminder）
3. 内容去重（sha256 与上次复盘比对）
4. 起一次性复盘子智能体（`spawn` provider，`toolFilter: allow=["memory_write"]`，`maxDepth: 1`，90 秒超时），复用阶段 1 的记忆接口写长期记忆

## 关键实现决策

- **不用后台 jobs**：复盘在 turn-stopping 里同步 await（`await run.result`），用户可见回复已先送达，不阻塞体验；headless 下可测，web 下常驻进程无影响。
- **防递归**：prompt 带 `[quiet-moment-reflection]` marker，transcript 含 marker 直接跳过；子智能体 `maxDepth: 1` 不再派生。
- **步数上限**：dsh 无 maxSteps 语义，用 `toolFilter` 只给 `memory_write` 实现等价约束（子智能体只能：读 prompt → 调 1-2 次工具 → 结束）。
- **`await start()` 坑**：`ctx.subagents.start()` 只表示发布成功，必须 `await run.result` 才是子智能体跑完；否则 headless 退出时子智能体被取消（实测复现过）。
- **`listEvents` 只有元数据**：拿不到消息内容，改用 `readSession` 快照。
- **mock 序列要给标题请求留位**：dsh 每会话有一次 session-title LLM 调用，会消耗 mock 序列中的一个行为；测试序列用 `success,tool_call_success,tool_call_success,success`。

## 验收（2026-09-28，全部 mock LLM，零真 key）

1. 用户说"我昨天买了一辆红色自行车" → 主回合结束 → turn-stopping 触发 → 复盘子智能体（独立 session，`origin: subagent`）→ 真实调用 `memory_write` → SQLite 落盘 `{"text":"用户买了一辆红色自行车","kind":"fact"}`。
2. 冷却：5 分钟内第二轮对话零复盘日志、无新记忆写入。
3. `web-intel` 干净启动：`:3080`，`/` 返回 401，无 `failed to import` / `plugin tree failed`。
4. 语法检查：`node --check` 全过；`import()` 实测 OK。

## 清理

- 测试记忆已删（memories=0）、`intel-quiet/state.json` 已删、mock 测试会话目录已删。
- `web-intel` 临时进程已停，3080 已释放。

## 未做

- 后台 jobs 化（当前同步 await 已满足需求，不引入复杂度）。
- 向量检索（阶段 1 已否决，延续 FTS5）。
