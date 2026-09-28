# 阶段 3 报告：自我进化循环 + Heartbeat + joblog

日期：2026-09-28
分支：intel
状态：已完成并通过验收

## 目标

复刻 Python 版的后台智能：
- 自我进化循环：memory upkeep（每小时）、studying（每天 07:30）、idea curation（每天 09:30）、dreaming（每天 23:30）、skill review（每周日 03:00）
- Heartbeat：每小时巡检 HEARTBEAT.md 清单（空清单跳过，每批≤3项）
- joblog 三件套：jobs.log、job_runs.json、job_alerts.md

## 架构决策

**不采用 dsh 原生 schedule 系统**。dsh 的 schedule 是"回到原会话的提醒"，不是后台 agent 执行，与 Python APScheduler 语义不等价。

**采用 cron + headless agent**：
- 系统 cron 触发 `/root/intel/bin/intel-task.sh <job_id> "<prompt>"`
- 包装器经 joblog 记录后，用 `dsh --profile evolve` 跑任务 prompt
- agent 用 memory_* 等工具完成任务，结果落盘
- Unix 原生，不常驻内存，dsh 重启不影响

这符合"dsh 是唯一骨架"（任务执行仍是 dsh agent，只是触发器用 cron）。

## 交付物

### 1. dsh-intelligence-joblog 插件
路径：`intel-plugins/dsh-intelligence-joblog/`
- `src/joblog.js`：JobLog 类（不依赖 dsh，可被 cron 脚本直接 require）
  - `guarded(jobId, fn)`：记日志、更新状态、失败告警
  - `jobs.log`（保留最近 2000 行）、`job_runs.json`、`job_alerts.md`
  - 存储：`~/.dsh/intel-joblog/`
- 工具：`joblog_status`、`joblog_alerts`
- 所有注册走 `ctx.effect`（带 key），支持卸载回卷

### 2. dsh-intelligence-evolution 插件
路径：`intel-plugins/dsh-intelligence-evolution/`
- `src/tasks.js`：5 个进化任务 + heartbeat 的 prompt 模板（复刻 evolve.py 语义）
- 工具：`heartbeat_check`
  - 读 `~/.dsh/intel-evolution/HEARTBEAT.md`
  - 空清单（或文件不存在）→ 返回"清单为空，跳过"，不烧 token
  - 非空 → 按每批≤3项分块返回
  - 忽略空行、`#` 开头、`_` 开头行，strip markdown 列表符
- 所有注册走 `ctx.effect`（带 key）

### 3. evolve profile
- bundles：`@deepseek-ai/dsh-base` + `@deepseek-ai/dsh-headless` + `dsh-intelligence-memory` + `dsh-intelligence-joblog` + `dsh-intelligence-evolution`
- 不带 quiet 插件（后台任务不需要复盘，避免噪声）

### 4. cron 包装器
- `/root/intel/bin/intel-task.sh`：`<job_id> "<prompt>"`，经 joblog 包装跑 evolve profile
- `/root/intel/bin/joblog-cli.js`：给包装器用的 joblog CLI
- 生产 key 从 `/etc/deepseek-harness/.env` 读取（仅 root 可读）
- `/root/intel/cron/intel-cron.txt`：6 个任务的 cron 配置（**未激活**，见下）

## 验收（全部基于 mock LLM，零真 key）

| 测试 | 结果 |
|------|------|
| heartbeat 空清单 | `heartbeat_check` 返回"清单为空，跳过"，isError=False |
| heartbeat 4 项清单 | 返回 2 批（3+1），分块正确 |
| dreaming 写记忆 | `memory_write` 调用成功，SQLite 落盘 `复盘：用户今天测试了进化任务` |
| joblog_status 工具 | 返回"暂无任务运行记录"，isError=False |
| intel-task.sh 成功路径 | exit 0，job_runs.json 记 ok，jobs.log 有起止 |
| intel-task.sh 失败路径 | exit 1，job_runs.json 记 fail，job_alerts.md 有告警 |
| evolve profile 加载 | 无错误，3 个 intel 插件全部就绪 |

## 踩坑

**`@deepseek-ai/dsh-tools` 版本必须 pin 到 0.1.7-rc.1**。
`npm install @deepseek-ai/dsh-tools` 默认装了 `0.0.1-rc.1`（旧版），用它 `defineTool` 定义的工具在 dsh 0.1.7-rc.1 运行时报 `unknown tool`。症状极具迷惑性：插件加载无错误、dump-config 能看到插件，但工具调不通。memory 插件（阶段 1）用的是对版所以没事。
教训：所有 `@deepseek-ai/*` 依赖必须 pin 到 dsh 基线版本 `0.1.7-rc.1`。

## 未激活 cron 的原因

Python harness 仍在生产运行这些任务（APScheduler）。现在激活 dsh cron 会导致双跑、双倍 API token 消耗。切生产时执行 `crontab /root/intel/cron/intel-cron.txt` 即可。

## 合规

- 未改 dsh 核心、未改 HMC
- 未进生产 web profile（只用 evolve + web-intel 隔离 profile）
- 所有注册走 ctx.effect，支持卸载回卷
- 密钥不进日志/代码/Git（从 /etc/deepseek-harness/.env 读取）
