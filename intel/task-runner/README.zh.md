# Linux 任务运行器

[English](README.md) | 中文

ChatGPT cron 路由显式为每个一次性进程使用 SSE。否则，已安装 SDK 的默认 WebSocket 缓存会在回复后继续保留进程五分钟。此单次调用 patch 不改变 web profile。路由的 `transport` 接受 `sse`、`websocket`、`websocket-cached` 或 `auto`；fallback 请求使用其自身路由的传输方式。

此运行器从演进功能当前的 `TASKS` 模板中解析内置 cron 任务 ID。内置任务会忽略可选的旧提示词参数；自定义任务必须提供明确的提示词。`routes.json` 提供任务路由、默认提供方与模型，以及超时时间。运行器仅启动 `evolve` DSH profile，并显式传入模型 patch。它依赖已安装的 profile 依赖和正常的提供方凭据，不会安装提供方或图像工具。

`route-runs.jsonl` 记录请求的提供方与模型、模板 ID、进程退出码和 fallback 原因，不记录提示词或凭据。请求的身份不能证明实际使用的模型：须将成功请求与 tokenlog 关联核对。重试非零退出的任务可能重复产生副作用，因此默认路由禁用重试。仅对经过审查的只读任务启用 `retrySafe`。超时会终止进程组；退出失败仍可能表示已经产生部分副作用，需要检查。

部署前先备份 `crontab -l`，再将其输出传给 `node intel/task-runner/cli.mjs --migrate-crontab` 并比较预览。该命令只输出替换结果，从不写入 crontab。它仅迁移已注册、提示词为单个引号字面量的内置任务；自定义任务、复合命令、重定向和依赖特定环境的包装器保持原样，留待人工检查。检查后部署 `run.sh` 并赋予执行权限，保留 cron 的时间安排与时区，再检查首次运行。不要盲目重跑失败的任务。仅添加此目录不会改变现有 profile 配置和生产 cron。

在 Linux 上运行 `node --test intel/task-runner/tests/*.test.mjs`。隔离的 CLI 测试使用假的 `dsh` 可执行文件，从不调用生产模型。

保留生产路由分工：upkeep 和 heartbeat 使用 Flash；晨间简报及其余六个演进任务使用 ChatGPT。`bash intel/verify-linux.sh` 安装每个插件的锁定依赖，并执行 Linux 插件与运行器回归；应在一次性检出目录中运行，因为它会替换插件依赖目录。
