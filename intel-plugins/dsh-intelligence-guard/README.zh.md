---
description: "生产 profile 的启动环境与凭据存在性检查。"
kind: "package-bundle"
---

# 生产环境守卫

[English](README.md) | 中文

## 摘要

生产启动器在启动 `web` profile 前调用 `checkInvariants`，拒绝被覆盖的 DeepSeek 端点、mock 环境变量及缺失的模型凭据。插件本身报告激活错误；启动器负责以 fail-closed 方式退出进程。

## 目录

- [使用本包](#use-this-package)
- [理解实现](#understand-the-implementation)
- [进一步探索](#further-exploration)
- [模型体验](#model-experience)
- [已知限制与延期工作](#known-limitations-and-deferred-work)
- [开发备注](#dev-note)

<a id="use-this-package"></a>
## 使用本包

生产启动器在启动 `web` profile 前调用 `checkInvariants`，拒绝被覆盖的 DeepSeek 端点、mock 环境变量及缺失的模型凭据。插件本身报告激活错误；启动器负责以 fail-closed 方式退出进程。

<a id="understand-the-implementation"></a>
## 理解实现

<details>
<summary>实现细节</summary>

DeepSeek 环境密钥或结构完整的 Codex OAuth 授权记录均可满足凭据存在性检查。授权记录取自 `DSH_HOME` 下的版本化凭据文档（默认使用当前用户的 `.dsh`），或显式指定的检查路径。注释、空记录与格式错误的 YAML 不计入。检查不会返回 YAML 错误详情或凭据值。已存储的授权记录不能证明当前授权有效、模型使用权限或调用成功；这些事实须通过 Host 模型接入 API 验证。

`HMC_CONFIG` 未设置或指向的文件缺失时，检查产生 `[WARN]`，明确手机端口 43197 不可用，并允许 web 启动。未通过的检查仍为 `ok: false`；调用者须从阻断项中排除 `severity: "warning"` 并记录 warning。插件遵循相同规则。

版本化的[生产启动器](dsh-prod-start)安装于 `/usr/local/bin/dsh-prod-start`，读取 `/etc/deepseek-harness/.env`，检查实际 HMC 配置路径、输出 warning，并以隔离环境启动 8080 上的 `web`。仅该管理员维护的文件可启用 `DSH_FIRST_USE=1`：缺少模型凭据时改为 warning，web/HMC 保持可用以完成首次配置。配置模型接入后须移除该标记。端点覆盖和 mock 变量始终阻断启动。同文件的可选 `DSH_TRUSTED_HOST` 保留代理后的 web 主机名。

</details>

<a id="further-exploration"></a>
## 进一步探索

[组合包 patch](cordis.patch.yml) · [插件入口](index.js)

<a id="model-experience"></a>
## 模型体验

间接通过启动检查决定 profile 是否启动；本守卫不发起模型请求。

#### KV Cache 影响

读取日志或检查凭据不会改变在途的模型请求。

<a id="known-limitations-and-deferred-work"></a>
## 已知限制与延期工作

凭据存在性不能证明模型使用权限或调用成功；请通过 Host 模型接入 API 执行连接验证。

<a id="dev-note"></a>
## 开发备注

<details>
<summary>维护者上下文</summary>

无。

</details>
