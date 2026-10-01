# dsh-intelligence-hooks — Event automation Hooks

English | [中文](README.zh.md)

Reimplements Python `agent/hooks.py`: hooks (name → trigger + prompt template + description)
persist in `DSH_HOME/intel-hooks/hooks.json`; `file` triggers poll the inbox directory and fire once per new file.

## Tools

| Tool | Parameters | Description |
|---|---|---|
| `hook_register` | `name*`, `trigger_type*` (file/webhook), `prompt*`, `dir`, `desc`, `auto` | Register/update a hook; file trigger directories are relative to the data directory, defaulting to `inbox` |
| `hook_list` | None | Name, automatic dispatch switch, backlog, latest batch start/exit result; task bodies are omitted |
| `hook_remove` | `name*` | Delete a custom hook; built-ins such as inbox cannot be deleted, but register can override them |
| `hook_fire` | `name*`, `source` (manual/webhook, default manual), `path`, `payload` | Trigger once manually and return the rendered task text (for tests/external events) |

`MAX_HOOKS=50`; names allow only letters, digits, `_`, and `-` (1–64 characters).

## Trigger semantics

- **Built-in inbox hook** (matching the Python default): `trigger={type:file,dir:inbox}`,
  with prompt template `"inbox got a new file: {path}. Read it, figure out what to do and act; log the result to the daily note."`
- **Polling**: every 60 seconds by default (`pollIntervalMs`, configurable), scan each file trigger directory.
  Deduplication keys `name:filename → mtime` persist in `hook_state.json`; a new file fires once,
  and changed content (changed mtime) fires again. Dotfiles and subdirectories are skipped. Record deduplication after successful enqueue;
  repeated ticks within one process do not duplicate events, and restart does not resend processed files.
- **Concurrency**: calls to fire the same hook run serially (a promise-chain lock, matching Python's threading.Lock).
- **Template rendering**: substitute `{name}` / `{path}` / `{payload}`; missing keys retain the placeholder and append `[context]`
  (matching Python `_render`).

## Execution differences from Python

Python `fire` executes the action (`agent` loop / `shell` command) and writes a daily note.
The plugin records tasks in `pending/<hook>.jsonl`, calling `taskBin` with a per-hook cooldown, per-hook serialization, and a global concurrency limit. The default wrapper runs headless tasks through `dsh --profile evolve` and records Joblog.

- `fires.jsonl` records triggers and cannot prove start or completion. The `spawn` event confirms wrapper start; only `close` with exit code 0 and no terminating signal records `succeeded`. Verify business outcomes against Joblog and actual artifacts.
- Failure before start (including asynchronous ENOENT) retains pending for cooldown-based retry. Nonzero exit after start or uncertain storage saves the original batch and pauses that hook; tasks with possible side effects cannot replay automatically.
- `dispatch-runs/<hook>.json` stores the latest batch, unique job ID, and start/exit outcome. After reload, `starting`/`started` still occupy concurrency slots. If parent-process exit leaves the outcome unknown, verify it manually; disappearance of a PID does not release a task.
- Current `auto=false`, hook deletion, global disable, and `stop()` prevent future dispatch. Disabling retains pending for re-enabling; deletion retains historical data. Override the built-in inbox with `auto=false` to disable it.
- Defaults: pending per hook is limited to 1 MiB, batches to 32 records and 48 KiB of prompt, and in-memory trigger history to 100 entries. Exceeding a limit explicitly rejects enqueue and retains the previous backlog; legacy oversized or corrupt files block dispatch. Polling enqueue failure does not advance file deduplication, allowing a later attempt. Audit and history files remain on disk; PC operations must handle backups, capacity monitoring, and archiving.

The plugin does not provide Python's `POST /api/hooks/<name>` webhook HTTP endpoint;
register webhook triggers and invoke them externally with `hook_fire(source=webhook)`.

## Data directory

The independent `intel-hooks/` directory under `DSH_HOME` (or `~/.dsh` when unset):

```
intel-hooks/
  hooks.json      # 自定义 hook 配置（内置不落盘）
  hook_state.json # 轮询去重账本 name:filename → mtime
  fires.jsonl     # 触发记录（每行一个任务）
  pending/        # 尚未确认启动的任务
  dispatch-runs/  # 最近批次及状态；失败/不明结果阻止自动重放
  dispatch-history/ # 每个 job 的原始结果与人工恢复决定
  dispatch.log    # started/start_failed/succeeded/failed/uncertain
  inbox/          # 默认轮询目录
```

Production/test isolation: tests construct `HookStore(tmpdir)` directly without using `DSH_HOME`.

## Unloading

`ctx.effect` registers tools and polling. Unloading stops discovery and future dispatch; already-started independent headless tasks continue, and completion callbacks update outcomes without starting another batch. Their persistent concurrency slots survive plugin reload. Only one dispatcher may use a data directory; multiple Hosts must not share it concurrently. Backups and manual recovery must preserve pending, batches, and Joblog to prevent duplicate execution.

## Profile wiring

`cordis.patch.yml` mounts only this plugin (no upstream overlay dependency). Add a profile bundle:

```json
{ "dsh": { "profile": { "bundles": ["@deepseek-ai/dsh-base", "dsh-intelligence-hooks"] } } }
```

Optional plugin configuration: `dataDir`, `pollIntervalMs`, `autoDispatch` (default true), `dispatchCooldownMs` (default 900000), `taskBin` (default `/root/intel/bin/intel-task.sh`), and `maxConcurrentDispatches` (default 3).

Configure resource limits as positive integers using `maxPendingBytes`, `maxBatchBytes`, `maxBatchRecords`, and `maxDeliveredTasks`. Lowering a limit does not delete old records; verify and handle oversized backlogs first.

## Manual recovery on PC

Stop the Host, back up Hooks data, and verify Joblog, the task and its descendant processes, and actual artifacts by job ID. `release` removes the barrier and skips the original batch after manual verification; it does not mean success. `retry` requeues the original batch and may duplicate existing side effects. Recovery does not launch tasks. Interrupted writes retain the barrier and can resume with the same job/action; changed pending during recovery is rejected for manual verification.

```sh
node intel-plugins/dsh-intelligence-hooks/scripts/recover-dispatch.js \
  --data-dir /path/to/intel-hooks --hook inbox --job "$JOB_ID" \
  --action release --confirm-host-and-task-stopped
```

Set `JOB_ID` to the verified job from `hook_list`. To explicitly retry, change `release` to `retry` and add `--acknowledge-duplicate-risk`. If the Host uses a custom pending limit, add the matching `--max-pending-bytes`. Original results and recovery decisions persist separately in `dispatch-history/<job>.json` and `<job>.recovery.json`. Re-enabled Hosts still obey automatic dispatch switches, cooldowns, and concurrency limits. Do not delete journals, treat PID disappearance as business completion, or manually clear pending.

## No dsh core changes

The plugin uses public APIs only: `ctx.effect` / `ctx.tools.register` / `ctx.provide`.
It does not modify core code under `packages/`.
