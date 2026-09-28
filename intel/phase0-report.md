# 阶段 0 环境自检报告（2026-09-28）

> 结论：dsh 在 hmc-mirror（1GB）上跑得起来，内存余量充足，停止条件未触发，可以进阶段 1。

## 1. 环境（全部实测）

| 项 | 结果 | 意味着什么 |
|---|---|---|
| Node | v22.22.2 | 满足 dsh 要求的 `^22.19.0 \|\| >=24` |
| dsh | `@deepseek-ai/dsh@0.1.7-rc.1`，npm 全局安装 | 与任务书基线一致，免构建 |
| 磁盘 | 11G 可用 | 远超 5G 下限 |
| `dsh --profile web --dump-config` | 正常输出插件树 | dsh 骨架在这台机器上能组装 |
| `dsh --profile web-intel` 起服 | `http://127.0.0.1:3080` 正常 | Web 骨架可对外服务 |
| `dsh --profile headless` 真实回合 | 调通 DeepSeek API，3.45 秒返回正确结果 | agent 循环 + 模型链路是通的 |

## 2. 内存基线（`/usr/bin/time -v` 精确值）

| 状态 | 峰值 RSS | 意味着什么 |
|---|---|---|
| web 空载 + hello 插件 | 144MB | —— |
| headless 单回合（含一次真实模型调用） | 139MB | —— |
| 整机可用内存（Python 栈运行中） | 约 355MB | dsh 常驻后仍剩约 200MB，**没有 OOM 风险** |

**停止条件判定**：dsh 跑得起来、无 OOM。**不停止，继续。**

## 3. 插件机制验证

- 最小 hello 插件：`dsh plugin add <本地路径>` 能装，`--dump-config` 能进树，启动 0 条 `failed to import`。
- **踩坑**：pnpm `link:` 安装不装插件的传递依赖，启动报 `failed to import`（日志不给原因，用 node 直接导入才看到是 `Cannot find package @deepseek-ai/dsh-llm`）。
  **结论**：树外插件必须在包内自带 `node_modules`（包里 `npm install`），`node_modules/` 不进 git。
- `@deepseek-ai/*` 包在 npm 公开可装，版本与基线对齐。
- 参考实现 `dsh-time-context` 已读：`name` + `inject=["agents","sessionProjections"]` + `agent/pre-step`（waterfall，一种"可拦截改写的钩子"，监听方必须调 `next()` 交棒）注入，与任务书 §2.2 一致。

## 4. 停掉 Python 栈的影响面

| 依赖方 | 现状 | 迁移时动作 |
|---|---|---|
| Tailscale Serve | tailnet URL → `127.0.0.1:8080`（Python） | 改指到 dsh 的 `:3080` |
| systemd | `deepseek-harness` enabled | 退役或保留对照 |
| Muse 周检 | 查 Python 服务健康 | 改查 dsh |
| 用户使用 | Python Web UI | 切到 dsh Web UI |

影响面收敛、可操作，不构成阻挡。

## 5. 版本管理

- fork：`VampireNails/deepseek-harness`（上游 `deepseek-ai/deepseek-harness`），clone 到 `/root/intel/dsh-fork`
- `master` 保持干净跟踪上游；智能层工作在 `intel` 分支，基于基线提交 `46a7f68b0`（= 0.1.7-rc.1）
- `dsh-baseline.json` 钉住版本；插件在 `intel-plugins/`，独立 npm 包形态
- push 待用户在 GitHub 仓库加 deploy key（write 权限），公钥已生成待添加

## 6. 待办（阶段 1 前置）

- 用户加 deploy key → push `intel` 分支
- 阶段 1 按任务书做记忆插件（mock LLM 测试，不许真调）

## 术语

| 术语 | 一句人话 |
|---|---|
| profile | dsh 的"一套组装方案"，存在 `~/.dsh/profiles/<名字>/` |
| bundle | 一组插件的打包层，profile 按顺序叠 bundle |
| `--dump-config` | 打印实际合成出的插件树，以它为准判断插件挂没挂上 |
| waterfall | 可被改写/拦截的事件，监听方必须调 `next()` 往下传 |
| pnpm `link:` | 把本地目录链进 node_modules，不装它的依赖 |
