# intel-plugins — 智能层插件（dsh 树外插件）

把复刻版 Harness 的"智能"能力搬进 dsh 的插件，HMC 零改动即可遥控。

- 基线: dsh `0.1.7-rc.1`（见 `../dsh-baseline.json`），本分支就建在这个提交上
- 每个插件是独立 npm 包，用 `dsh plugin --profile <name> add <path>` 安装
- **先装依赖**：`node_modules/` 被 `.gitignore` 排除，clone 后每个插件目录先跑 `npm ci`
  （lockfile 已钉死版本），否则启动只报 `failed to import` 且日志不给原因（阶段 0 踩坑）
- 约束: 不改 dsh `packages/` 核心包；所有注册走 `ctx.effect`；密钥不落盘

| 插件 | 状态 | 说明 |
|---|---|---|
| hello-intel | 阶段 0 验证用 | 最小挂载验证，可删除 |
