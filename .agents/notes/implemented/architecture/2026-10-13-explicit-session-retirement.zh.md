# Agent Note: Explicit Session retirement

Status: implemented

[English](2026-10-13-explicit-session-retirement.md) | 中文

## 问题

dispose 活动 Agent 会释放所有权，但保留持久 Session。归档仅改变发现元数据。两者均不提供不可逆删除契约；仅移除当前日志会使保留的历史 generation 复活该身份。

## 决策

显式授权的 Linux 持久化删除会退役一个精确的普通冷 Session。JSONL 提供方持有现有进程内写入声明与跨进程 flock，验证全部 generation 身份与目录所有权，持久发布独占的根级退役记录，然后仅移除记录中的日志 generation。稳定锁 inode 与退役记录保留。退役独立于相邻格式迁移：迁移继续保留所有已提交前代；显式删除则有意移除退役 Session 的 generations。

许可检查拒绝每个活动 Session 或仍挂载的后代，包括持有写句柄的 idle Agent，以及活动持久 Schedule 记录。提供方不会取得其他拥有者的 dispose 权限。缺少持久对象检查器时拒绝，而非假定无活动。业务控制器仅移除退役身份的 workspace 关联，并在物理清理后发出现有移除通知；重试会重复该元数据清理。共享记忆、产物、缓存和独立 Session 不属于删除所有权。

## 考虑过的替代方案

**dispose 或归档：** 两者有意保留持久历史，因此均无法阻止重启恢复。

**移除目录：** unlink flock inode 会允许新 inode 接纳另一个写者；广泛目录删除还会把未知文件纳入删除所有权。

**仅删除当前 generation：** 历史 generation 会重新被发现。移除任何 generation 之前必须持久记录身份退役。

**强制停止所有活动：** 调用方不拥有任意 Agent handle。拒绝活动拥有者保留现有所有权模型；发起者关闭自己的 handle 后重试。

## 后果

支持的发现范围为 `cold-ordinary-session`。turn 完成或取消本身不够。活动拥有者删除与子代理对象删除仍不支持；SDK 创建的活动 Session 在其 runtime 释放所有权后可删除。永久身份退役以放弃同 ID 复用换取重启安全。成功清理仍保留锁文件与退役记录。不完整清理保持隐藏，列表不可见不能确认物理移除。[提供方契约](../../../../packages/session/session-persistence-jsonl/README.zh.md#irreversible-deletion-on-linux)负责描述结果、取消与文件系统假设。

## 验证

提供方的 Linux 删除测试覆盖精确 ID 邻近对象、链接与头拒绝、压缩、重启、取消和部分清理。SDK 测试覆盖真实持久化所有权与应答解码。受支持的具名 Linux Host profile 覆盖真实 Typert 和 HMC HTTPS 方法、活动 turn、job、持久提醒、所有权释放与重连。独立 Linux 进程探针覆盖 flock 竞争及标记发布后、unlink 前的 SIGKILL。SIGKILL 恢复是进程失败证据，不是真实断电实验。
