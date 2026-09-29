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
dsh 插件侧能做的是**生成任务描述文本 + 记录**：

- 触发时渲染好的 prompt 文本记入内存任务队列（`hooks.deliveredTasks()`）和 `fires.jsonl`
- 实际执行（开 agent 回路处理）由消费方决定，例如 evolve/heartbeat 任务定期读取
  `fires.jsonl` 后用 `dsh --profile` 跑任务

Python 版的 `POST /api/hooks/<name>` webhook HTTP 入口本插件不提供；
webhook 触发器可注册，由外部通过 `hook_fire(source=webhook)` 触发。

## 数据目录

`DSH_HOME`（未设则 `~/.dsh`）下的独立子目录 `intel-hooks/`：

```
intel-hooks/
  hooks.json      # 自定义 hook 配置（内置不落盘）
  hook_state.json # 轮询去重账本 name:filename → mtime
  fires.jsonl     # 触发记录（每行一个任务）
  inbox/          # 默认轮询目录
```

与生产/测试隔离：测试用 `HookStore(tmpdir)` 直接构造，不碰 `DSH_HOME`。

## 卸载

`ctx.effect` 注册：工具 + 轮询 timer。卸载时 effect 回卷停掉 timer；
已生成的任务记录（`fires.jsonl`）保留，彻底清理请先 `hook_remove` 再删数据目录。

## Profile 接线

`cordis.patch.yml` 只自挂载本插件（无上游 overlay 依赖）。profile bundles 加一行：

```json
{ "dsh": { "profile": { "bundles": ["@deepseek-ai/dsh-base", "dsh-intelligence-hooks"] } } }
```

可选配置（plugin 行 `config`）：`{ "dataDir": "/path/to/dir", "pollIntervalMs": 30000 }`。

## 不碰 dsh 核心

本插件只用 `ctx.effect` / `ctx.tools.register` / `ctx.provide` 等公开 API，
未修改 `packages/` 下任何核心代码。
