---
description: "settings 与凭据配置界面的 Host Remote owner，涵盖脱敏读取、写入、凭据引用与原生文档打开。"
kind: "package-reference"
---
# Settings Controller

[English](README.md) | 中文

## 概述

`@deepseek-ai/dsh-api-settings-controller` 为配置页面提供生成的 `ctx.remote.settings`、`ctx.remote.credentials` 与 `ctx.remote.modelAccess` namespace。它返回脱敏配置和凭据元数据，支持写入而不返回机密值，管理订阅登录，并在 Host 桌面打开提供方持有的配置位置。提供方缺失时，调用返回明确错误。

## 目录

- [使用本包](#use-this-package)
- [配置](#configuration)
- [模型体验](#model-experience)
- [已知限制与延期工作](#known-limitations-and-deferred-work)
- [开发备注](#dev-note)

-----

<a id="use-this-package"></a>
## 使用本包

请把本包作为 Loader entry 挂载到提供浏览器配置的 profile 中。本 entry 不依赖提供方是否存在而注册全部四个 namespace，因此缺少提供方会在调用时产生具名配置错误。它生成的 descriptor 进入严格 Typert 注册表，而 settings 与凭据 Definition 仍是普通 Cordis 服务，自身不承担任何 wire 义务。

`describe(refs)` 以请求的名字为键返回一份 map，因此设置页描述其各行携带的全部引用时，这些行会一起落定。单次调用最多接受 64 个名字，无效名字或空写入值报告为 `bad-request`，并逐字段复制每个答案——提供方返回超出 `CredentialInfo` 声明的内容也无法扩大跨越 wire 的字段。有效的 `set(ref, value)` 与 `unset(ref)` 调用把提供方拒绝报告为 `credential-rejected`，携带提供方的消息，details 中只有该引用。机密值只在这个方向跨越 wire：这里没有任何方法会返回它。

`settings.describe()` 返回部署信息，以及在 `redactSecrets: true` 下读取的所有 namespace。`settings.update`、`settings.replace` 与 `settings.mutate` 暴露 settings 服务的三种写入操作，并返回该 namespace 的新脱敏视图；陈旧写入使用 `settings-conflict`，其他提供方拒绝使用 `settings-rejected`。

`settings.openSettingsDocument()` 准备提供方持有的文档，并用原生文本编辑器打开；该方法不接受浏览器提供的文件系统目标。

`modelAccess.configuration()` 包含已安装但未启用的提供商、已启用配置、namespace revision、登录方式，以及独立的引用和记录元数据。可编辑提供商在 LLM 目录声明 `credentialScope: 'llm-pi-ai'`。`save(request)` 要求明确的认证方式和当前 revision；新输入的 API Key 使用专用 Host 引用。OAuth 激活要求已存授权记录，并拒绝继承的端点、协议、Key 引用或请求头覆盖。`remove` 只移除用户配置，并拒绝移除继承的提供商；`clearKey` 移除页面管理的引用和 API Key 记录；`logout` 只删除订阅授权。所选认证方式、已存凭据和实际调用验证是独立事实。

`start({ provider, method })` 立即返回随机 attempt ID 和 owner token。`status`、`respond` 与 `cancel` 都要求这两项。轮询返回当前通知与问题，不保留用户答案。flow 撤销问题后，其 prompt ID 失效。同一提供商不允许并行授权；尝试会超时，终态只保留有限时间。错误使用安全 reason code，不转发凭据或提供商的错误原文。

`verify({ provider, model })` 执行最多输出 16 token 的固定请求，只返回成功或安全错误码。`discover(request)` 委托已安装 Host adapter 查询模型，只返回 ID 与名称。只读提供商返回其已注册的模型目录，并拒绝草稿端点、协议和 Key 覆盖；其配置仍为只读。

-----

<a id="configuration"></a>
## 配置

| 字段 | 默认值 | 含义 |
|---|---|---|
| `modelAccess.attemptTtlMs` | `600000` | 授权有效期，1000–3600000 ms |
| `modelAccess.retentionMs` | `300000` | 终态保留时间，1000–3600000 ms |
| `modelAccess.maxAttempts` | `32` | 运行及保留的尝试上限，1–256 |
| `modelAccess.verificationTimeoutMs` | `30000` | 连接检查期限，1000–120000 ms |

生成的[配置目录](../../../docs/config-catalog.zh.md#deepseek-aidsh-api-settings-controller)是所有受支持字段及其 JSDoc 的完整来源。

-----

<a id="model-experience"></a>
## 模型体验

无，因为配置与授权不注册提示词、工具或会话事件。

#### KV Cache 影响

读取或写入配置不会改变已经在途的模型请求。明确执行连接检查时，会把固定用户输入 `Reply OK.` 发送给所选模型，输出上限为 16 token；这会消耗提供商用量，且不返回回复原文。

## 已知限制与延期工作

<a id="known-limitations-and-deferred-work"></a>

- 批量上限固定为 64 个引用，不是可按部署配置的字段。
- 配置与凭据分别提交。配置提交后若凭据写入被拒绝，会返回 `configuration-saved-credential-failed`；重试前需重新读取配置。陈旧或无效的配置写入不会修改 Key。
- 模型账号编辑当前支持 pi-ai 提供商；其他 adapter 的目录信息仍可读取。
- 来源元数据覆盖已存记录和显式 `apiKeyEnv` 引用。退出订阅后，提供商的环境自动发现仍可能认证成功；本 API 不枚举或清除这些来源。

<a id="dev-note"></a>
### 开发备注

<details>
<summary>维护者工作上下文——点击展开</summary>

构建产物的传输验收使用 `tests/model-access-wire.e2e.ts`，依赖本包生成的 `lib/typert.host.js`。普通源码测试无需该产物。

</details>

**运行时不变式：** 不发布伴生入口。settings 与凭据 seam 负责存储和更新事件，本包只把它们的方法投影到 wire。
