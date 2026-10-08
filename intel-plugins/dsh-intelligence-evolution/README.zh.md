---
description: "通过现有 DSH Host 配置原始人工输入截取和有界记忆维护。"
kind: "package-bundle"
---

# 演进与 upkeep 来源

[English](README.md) | 中文

## 概述

通过现有 Host 为记忆维护截取新增的原始人工输入。空批次可以跳过模型执行，结果不确定的写入保留为待确认供检查。任务模板、heartbeat 清单读取和原生自动任务会话归类支持 [Linux 运行器](../../intel/task-runner/README.zh.md)，由运行器负责调度、模型路由、检查点和待确认运行恢复。

## 目录

- [使用本插件](#use-this-package)
- [了解实现](#understand-the-implementation)
- [进一步阅读](#further-exploration)
- [模型体验](#model-experience)
- [已知限制与暂缓工作](#known-limitations-and-deferred-work)
- [开发备注](#dev-note)

<a id="use-this-package"></a>
## 使用本插件

仅在提供 sessions、sessionQuery 和 sessionPersistence 的现有 Host profile 中配置 `upkeepSourceSocket`。默认值为空，因此 runner profile 不提供来源服务。Linux 绝对 socket 路径必须与运行器的 `upkeep.socketPath` 一致。插件要求仅所有者可访问的私有父目录，发布权限为 600 的 Unix socket，拒绝覆盖活跃 socket 和普通文件，卸载时只删除自己发布的 inode。请求只支持原始截取和确切运行检查，不能启动智能体、执行工具或修改会话。断开连接或达到配置期限时中止读取，卸载等待所属读取结束。

`upkeepSourceTimeoutMs` 默认为 30,000 毫秒。`upkeepMaxSessionBytes` 默认为每个冷会话文件 67,108,864 压缩字节。

`heartbeat_check` 按每组三项读取私有的 `intel-evolution/HEARTBEAT.md` 清单。附带的 cron 检查调用 `joblog_status` 和 `joblog_alerts`，需要时从 `route-runs.jsonl` 核对运行与退出信息。未结束的 start 保持未确认，安静 upkeep 跳过属于预期，历史告警保留日期。更新本插件不会覆盖自定义私有清单；迁移前先备份并比较附带的 cron 检查项。

来源等待三个读取服务全部就绪，包含 Host 启动过程中稍后注册的提供方。服务就绪后才发布 socket；满足依赖后的来源初始化失败会使启动拒绝。来源归 evolution 插件生命周期管理，插件卸载时关闭。

<a id="understand-the-implementation"></a>
## 了解实现

<details>
<summary>读取所有权与游标处理</summary>

冷读取串行进行，使用官方只读持久化句柄。活跃会话使用不可变事件快照。两个路径都不挂载冷智能体、不跟随更新，也不把合成的中断闭合事件加入原始游标。来源日志缺失、不可读或过大时整批失败，不视为空。

截取仅包含来源 kind 为 `user` 的原始 `user/message` 事件，排除分支继承事件、标准子智能体和自动任务索引中的根会话。未知历史根会话保持未分类；首次建检查点时记录其现有事件，不重放。普通分支保留自身后续人工输入。来源在截取后再次检查自动任务归类。配置的批次限制保留完整原文，并为延后到下一批的输入保留游标。

</details>

<a id="further-exploration"></a>
## 进一步阅读

- [运行器准入与待确认恢复](../../intel/task-runner/README.zh.md)
- [记忆持久化与写入元数据](../dsh-intelligence-memory/README.zh.md)
- [Cordis effect 与卸载](../../docs/cordis-primer.zh.md)

<a id="model-experience"></a>
## 模型体验

Dreaming 先调用 `joblog_status` 和 `joblog_alerts`，再按运行 ID 配对 `route-runs.jsonl` 的当日记录。最新成功不抹去此前失败；用量限制与授权 token 失效须分别核对原始错误。提示词将错误调用计入十次预算，最多写两条教训，并预留一次 Feed 发布。证据不可用或预算用尽时停止调查，发布已检查范围与缺口。这是提示指引，不是运行时工具上限；自然运行仍须核对调用数和发布结果。

runner 身份为 `evolve_upkeep` 或 `memory_upkeep` 时，实际工具准入只允许顶层原生 `memory_search` 和 `memory_write`。`upkeepToolBudget` 默认每个智能体六次调用，包含失败调用。其他任务身份保留现有工具。upkeep 提示词把原始批次作为来源数据，记忆搜索只用于检查重复或冲突。持久化完成检查要求确切归类的根会话与运行、已完成轮次、身份匹配且全部结束的工具调用，以及规范的成功记忆写入元数据。准入拒绝、工具错误或写入失败都保持运行未确认。

<a id="known-limitations-and-deferred-work"></a>
## 已知限制与暂缓工作

压缩字节检查限制输入文件，不限制解压后内存使用；提供方当前会解码整份会话日志。空轮次测试和缩短的退避时钟不能作为生产 24 小时采样证据。

<a id="dev-note"></a>
## 开发备注

安装插件锁定依赖后，在 Linux 上运行 `node --test intel-plugins/dsh-intelligence-evolution/tests/*.test.js`。原始来源用例使用已发布的 Session、JSONL 持久化和 SQLite 查询提供方，不启动另一个 Host，也不请求模型。
