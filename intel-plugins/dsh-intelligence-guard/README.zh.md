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
