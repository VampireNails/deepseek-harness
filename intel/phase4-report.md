# 阶段 4 报告：Feed + 用户画像 + 晨间简报

日期：2026-09-28
分支：intel
状态：已完成并通过验收

## 目标

复刻 Python 版的三个能力：
- Feed：个人简报流（`feed_post` 工具，晨间简报自动发帖）
- 用户画像：仅 Tomas 本人的结构化画像（不建人物/群组档案）
- 晨间简报：每天 08:00 生成简报并发布到 Feed

## 交付物

### 1. dsh-intelligence-feed 插件
路径：`intel-plugins/dsh-intelligence-feed/`
- `src/feed.js`：FeedStore 类（复刻 Python feed.py）
  - `feed.json` 是唯一真相源（数组，新在前），上限 100 条，id 为 f1、f2……
  - 存储：`~/.dsh/intel-feed/feed.json`
- 工具：`feed_post`（标题/正文/来源）、`feed_read`（n 条，新在前）
- 所有注册走 `ctx.effect`（带 key），支持卸载回卷

### 2. dsh-intelligence-profile 插件
路径：`intel-plugins/dsh-intelligence-profile/`
- 存储：`~/.dsh/intel-profile/user_profile.md`
- 种子：从 Python 版 `/opt/deepseek-harness/memory/user_profile.md` 复制（Tomas 画像）
- 每回合第 1 步经 `agent/pre-step` 自动注入画像（走 `ctx.effect`，可卸载回卷）
- 工具：`user_profile_read`、`user_profile_update`（按小节更新）
- 明确约束：仅记录 Tomas 本人，不建人物/群组档案

### 3. 晨间简报
- 任务 prompt（复刻 Python `_morning_briefing`）：
  1. `web_search` 查今日重要科技新闻（3-5 条）
  2. 结合用户画像 + `memory_search` 的用户关注点
  3. 中文简短简报 + 每个进行中目标一句推进建议
  4. `feed_post` 发布（标题：晨间简报 YYYY-MM-DD）
- cron：每天 08:00（已加入 `/root/intel/cron/intel-cron.txt`，**未激活**，与阶段 3 同理）

### 4. Profile 装入
- `evolve` profile：+ feed + profile（共 7 个 bundle）
- `web-intel` profile：+ feed + profile（与 memory/quiet 一致，共 7 个 bundle）

## 验收（全部基于 mock LLM，零真 key）

| 测试 | 结果 |
|------|------|
| feed_post | 发布 f1，`feed.json` 落盘（id/ts/title/body/source） |
| 画像注入 | step 1 的 user/message 含画像内容（source.kind=dsh-intelligence-profile） |
| 晨间简报全链路 | agent 生成简报 → `feed_post` → feed.json 有"晨间简报 2026-09-28" |
| web-intel 启动 | `:3080` 返回 401（需鉴权），无错误 |

## 说明

- Feed 的 Web UI（Python 版有 `/feed` 页面）未复刻。dsh web 的 UI 扩展机制与 Python Flask 不同，
  工具层已完备，UI 可在切生产前按 dsh web 规范另行开发。记为已知缺口，不阻塞。
- 晨间简报依赖 `tool-web` 的 web_search（evolve profile 自带，DeepSeek 后端）。

## 合规

- 未改 dsh 核心、未改 HMC
- 未进生产 web profile（只用 evolve + web-intel）
- 所有注册走 ctx.effect，支持卸载回卷
- 依赖全部 pin 到 0.1.7-rc.1（阶段 3 教训）
- 密钥不进日志/代码/Git
