# dsh-intelligence-hooks —— 事件自动化 Hooks

复刻 Python 版 `agent/hooks.py`：hook 可注册（name → trigger + prompt 模板 + desc），
持久化到 `DSH_HOME/intel-hooks/hooks.json`；`file` 触发器轮询 inbox 目录，新文件触发一次。

## 工具

| 工具 | 参数 | 说明 |
|---|---|---|
| `hook_register` | `name*`, `trigger_type*`（file/webhook）, `prompt*`, `dir`, `desc` | 注册/更新 hook；file 触发器的轮询子目录相对数据目录，默认 `inbox` |
| `hook_list` | 无 | 列出内置 + 自定义 hook：名称、触发类型、描述 |
| `hook_remove` | `name*` | 删除自定义 hook；内置（如 inbox）不能删，可用 register 覆盖 |
| `hook_fire` | `name*`, `source`（manual/webhook，默认 manual）, `path`, `payload` | 手动触发一次，返回渲染后的任务文本（测试/外部事件用） |

`MAX_HOOKS=50`，名称只允许字母/数字/`_`/`-`（1-64 字符）。

## 触发语义

- **内置 inbox hook**（复刻 Python 默认配置）：`trigger={type:file,dir:inbox}`，
  prompt 模板 `"inbox got a new file: {path}. Read it, figure out what to do and act; log the result to the daily note."`
- **轮询**：默认每 60 秒（`pollIntervalMs`，配置项）扫描所有 file 触发器的目录；
  去重键 `name:filename → mtime` 存 `hook_state.json`——新文件只触发一次，
  文件内容变化（mtime 变）会再次触发；点开头文件与子目录跳过；先记账再触发，
  同进程重复 tick 不会重复触发，重启不重发已处理文件。
- **并发**：同名 hook 的 fire 串行执行（promise 链加锁，复刻 Python 的 threading.Lock）。
- **模板渲染**：`{name}` / `{path}` / `{payload}` 占位替换；缺键时退化为原文 + `[context]` 追加
  （复刻 Python `_render`）。

## 与 Python 版的执行语义差异

Python 版 `fire` 会实际执行 action（`agent` 回路 / `shell` 命令）并写 daily note。
dsh 插件将任务记录写入 `pending/<hook>.jsonl`，按单 hook 冷却、单 hook 串行和全局并发上限调用 `taskBin`。默认包装器通过 `dsh --profile evolve` 执行 headless 任务，并记录 Joblog。

- `fires.jsonl` 表示触发记录，不能证明启动或完成。`spawn` 事件确认包装器启动；`close` 的退出码为 0 且无终止信号才记录 `succeeded`，业务结果仍须核对 Joblog 和实际产物。
- 未启动失败（包括异步 ENOENT）保留 pending，并按冷却重试。启动后非零退出或存储结果不明保存原批次并暂停该 hook；不能自动重放可能已产生副作用的任务。
- `dispatch-runs/<hook>.json` 保存最近批次、唯一 job ID、启动及退出结果。重载后 `starting`/`started` 仍占并发位；主进程退出导致结果不明时须人工核对，不能仅凭 PID 消失释放任务。
- 当前 `auto=false`、已删除 hook、全局关闭或 `stop()` 都阻止后续调度。禁用保留已有 pending，重新启用后可继续；删除不清除历史数据。内置 inbox 可覆盖为 `auto=false`。

Python 版的 `POST /api/hooks/<name>` webhook HTTP 入口本插件不提供；
webhook 触发器可注册，由外部通过 `hook_fire(source=webhook)` 触发。

## 数据目录

`DSH_HOME`（未设则 `~/.dsh`）下的独立子目录 `intel-hooks/`：

```
intel-hooks/
  hooks.json      # 自定义 hook 配置（内置不落盘）
  hook_state.json # 轮询去重账本 name:filename → mtime
  fires.jsonl     # 触发记录（每行一个任务）
  pending/        # 尚未确认启动的任务
  dispatch-runs/  # 最近批次及状态；失败/不明结果阻止自动重放
  dispatch.log    # started/start_failed/succeeded/failed/uncertain
  inbox/          # 默认轮询目录
```

与生产/测试隔离：测试用 `HookStore(tmpdir)` 直接构造，不碰 `DSH_HOME`。

## 卸载

`ctx.effect` 注册工具和轮询。卸载停止发现与后续调度；已启动的独立 headless 任务继续执行，完成回调只更新结果，不能启动下一批。其持久化并发位在插件重载后仍生效。一个数据目录由一个调度实例使用；不能并行启动多个 Host 共享该目录。备份和人工恢复须保留 pending、批次及 Joblog，避免重复执行。

## Profile 接线

`cordis.patch.yml` 只自挂载本插件（无上游 overlay 依赖）。profile bundles 加一行：

```json
{ "dsh": { "profile": { "bundles": ["@deepseek-ai/dsh-base", "dsh-intelligence-hooks"] } } }
```

可选配置（plugin 行 `config`）：`dataDir`、`pollIntervalMs`、`autoDispatch`（默认 true）、`dispatchCooldownMs`（默认 900000）、`taskBin`（默认 `/root/intel/bin/intel-task.sh`）、`maxConcurrentDispatches`（默认 3）。

## 不碰 dsh 核心

本插件只用 `ctx.effect` / `ctx.tools.register` / `ctx.provide` 等公开 API，
未修改 `packages/` 下任何核心代码。
