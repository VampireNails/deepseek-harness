---
description: "Linux Host 对话中的有界 Token 用量扫描。"
kind: "package-bundle"
---

# Token 用量

[English](README.md) | 中文

## 摘要

本组合包为 Linux Host 对话加入只读 Token 用量汇总工具。它按保留日志中的提供商和模型归属 token，在 worker 中执行有界扫描，并报告跳过的文件。它不提供计费估算。

## 目录

- [使用本包](#use-this-package)
- [理解实现](#understand-the-implementation)
- [进一步探索](#further-exploration)
- [模型体验](#model-experience)
- [已知限制与延期工作](#known-limitations-and-deferred-work)
- [开发备注](#dev-note)

<a id="use-this-package"></a>
## 使用本包

要把此组合包加入既有的 Linux `web` 与 `evolve` profile，并把 sysevents 组合包加入 `web`，请在仓库中运行 `node intel/hmc-observability.mjs <profiles-root> <new-backup-directory>`。辅助脚本保留 profile patch，并在修改前备份两份 manifest（元数据清单）。重启生产服务后加载新组合包。回滚时恢复备份的 manifest，仅移除 `links.json` 标记为 `created` 的链接，然后重启。HMC 从共享日志读取系统事件，与事件由哪个 profile 发出无关。它的 Android 用量页读取本组合包挂载的聚合服务，不创建会话或发起模型请求。

`token_usage_summary(days)` 读取 Linux Host 保留的 `session.v4.jsonl.zstd` 日志，范围为 1–30 天，按任务、日期及提供商和模型汇总输入与输出 token。每条助手消息归属其前面的请求头，同一会话内的模型切换也会计入。缺失请求头时报告 `unknown`，不推定提供商。一个会话切换模型后，可以计入多个模型的会话数。任务分组采用第一条用户消息。

时间窗口覆盖此前 `days × 24` 小时，可以跨更多自然日期。工具与聚合服务均采用可选的 profile 配置 `sessionsRoot`；未设置时读取 `DSH_HOME` 下的 `sessions/--root--`，环境变量未设置时采用 `~/.dsh`。根目录缺失或无法读取时返回 `TOKENLOG_SOURCE_UNAVAILABLE`；既有空目录报告零用量。服务调用方可显式覆盖本次扫描的根目录。

<a id="understand-the-implementation"></a>
## 理解实现

<details>
<summary>实现细节</summary>

工具在 worker 中解压，保持 HTTPS 和事件投递可响应。profile 配置中的 `timeoutMs` 限制扫描总时长（默认 60000，允许 1–300000）。每次解压超过剩余扫描时长与 `decompressTimeoutMs` 中的较小值后被终止（后者默认 5000，允许 1–300000）。取消会等待当前有界解压及 worker 退出，不会立即完成。耗尽总时限会使扫描失败，不返回部分成功。错误仅暴露固定代码。`ctx.get("tokenlog").aggregate(sessionsRoot, days, signal)` 返回 Promise；`format` 渲染结果。扫描只读日志，不发起模型请求。

</details>

<a id="further-exploration"></a>
## 进一步探索

[组合包 patch](cordis.patch.yml) · [插件入口](index.js)

<a id="model-experience"></a>
## 模型体验

间接通过组合包激活的工具；`token_usage_summary` 的格式化结果会进入调用它的对话。

#### KV Cache 影响

读取日志或检查凭据不会改变在途的模型请求。

<a id="known-limitations-and-deferred-work"></a>
## 已知限制与延期工作

报告统计保留的日志，不作为计费账本。缺失或不可读的会话文件计入跳过数。Token 记录不能证明订阅使用权限、缓存价格或实际费用，因此报告不估算金额。其他代际的会话格式及单独存储的子会话日志不在本读取器的范围内。

<a id="dev-note"></a>
## 开发备注

<details>
<summary>维护者上下文</summary>

无。

</details>
