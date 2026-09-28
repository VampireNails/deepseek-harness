# dsh-intelligence-cron —— 用户自建定时任务（P0，REPLICATION #11）

复刻 Python 版 `agent/custom_cron.py`：中文时间解析 + `cron_create` / `cron_list` / `cron_delete`。
调度层由 `@deepseek-ai/dsh-schedule` 承担（见下文 profile 接线要求）。

## 工具

| 工具 | 参数 | 说明 |
|---|---|---|
| `cron_create` | `name*`, `prompt*`, `schedule*`（中文） | 解析中文时间 → 映射为 dsh schedule 后创建；返回中文回执（含 `cN` id 与下次触发时间） |
| `cron_list` | 无 | 中文列表：id、名称、重复规则、下次触发、scheduled/overdue/未武装/已完成 |
| `cron_delete` | `ref*`（id 如 `c1` 或名称关键词） | 删除 wrapper 记录 + 底层 schedule，停止重武装 |

## 中文时间 → dsh 映射

| 中文说法 | 映射 | 说明 |
|---|---|---|
| `每天9点` / `每天9:30` / `每天9点半` / `每日X` | one-shot `at`（下一个 9:00）+ 触发后重武装下一轮 | dsh 无"每天"语义，用 at 链实现 |
| `每周一9点` / `星期三18:30` | one-shot `at`（下一个周一 9:00）+ 触发后重武装下周 | 同上，步长 7 天 |
| `每2小时` / `每30分钟` / `每小时` / `每半小时` | `every_seconds` | dsh 原生固定频率；换算后 < 300 秒报中文错（最小 5 分钟） |
| `30分钟后` / `2小时后`（Python 版没有，新增） | `after_seconds` 一次性 | 触发后标记完成 |
| `明天8点`（Python 版没有，新增） | one-shot `at` | 触发后标记完成 |

`MAX_JOBS=20`，id 为 `c1、c2…`（与 Python 一致）。

## 架构要点

- **session-local**：提醒只在创建它的会话 live 时到点跟进（`[SCHEDULE REMINDER]` follow-up）。
  会话死亡期间错过的触发点**只防丢不补发**：对账时按"下一个未来触发点"补建。
- **重武装**：`每天/每周` 存为 one-shot `at`；插件监听 `session/event` 的 dispatch，
  命中自己的 schedule id 即追加下一轮（slot id `cron-<cN>-<YYYYMMDD>` 固定，天然幂等）。
- **对账**：`agent/created` 时检查本会话归属 job：存活则不管；过期/丢失则补建未来点；
  无归属的孤儿 `cron-*` schedule 回收。job 元数据在 `DSH_HOME/intel-cron/cron.json`。
- **不碰 dsh 核心**：未 import `@deepseek-ai/dsh-schedule` 的任何深路径（其 `transaction`
  未从 index 导出），直接按 `src/tools.ts` 的格式构造 `schedule/change` 会话事件；
  串行化/持久化屏障照抄其模式（per-agent tail + `ctx.sessions.flush`）。
- **卸载**：`ctx.effect` 注册的工具与监听可回卷；已写入会话日志的 schedule 事件仍在，
  未触发的单轮提醒会再触发一次（由 dsh-schedule 持有），不再重武装。
  彻底清理请先 `cron_delete` 再卸载。

## Profile 接线

本插件自带 Schedule overlay：`cordis.patch.yml` 会把 dsh 自带的
`@deepseek-ai/dsh-schedule` cordis 插件挂载为 host 服务（ScheduleRuntime 计时器 +
`schedule_create` 等原生工具 + session 投影）。profile 只需把本插件列进 bundles：

```json
{
  "dsh": {
    "profile": {
      "bundles": [
        "@deepseek-ai/dsh-base",
        "@deepseek-ai/dsh-web-app",
        "dsh-intelligence-cron"
      ]
    }
  }
}
```

注意：`@deepseek-ai/dsh-schedule` **不是** profile bundle（它的 package.json 没有
`dsh.bundle` 声明），写进 `bundles` 会被 dsh 跳过并告警；只能以 plugin 行挂载，
本插件已代劳。`@deepseek-ai/dsh-schedule` 由 dsh 全局安装自带（`0.1.7-rc.1`），
无需写进 dependencies；本插件用 `file:`/`link:` 装入即可。

## 测试

```bash
node --test tests/parse.test.js tests/scheduler.test.js  # 41 个单测
bash tests/integration.sh   # 隔离 profile + mock LLM + HMC RPC，真等 5 分钟并断言触发
```
