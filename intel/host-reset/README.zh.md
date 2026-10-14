# HMC 完整部署重置

[English](README.md) | 中文

本 Linux 管理服务需显式安装，运行在 DSH 主服务的 cgroup 之外。接口不接受客户端路径或命令。HMC 在原配对连接上准备操作；手机加密保存并验证独立 TLS 进度连接后，才在该连接提交。不会备份业务数据或旧凭据。

由 root 执行 `python3 intel/host-reset/install.py --config /private/manifest.json`。安装清单须为 root 所有、0600，字段及目录要求见[英文配置说明](README.md)。安装程序首次分配部署 UUID，升级时沿用。控制记录位于清除范围外，Unix socket 使用 `/run/hmc-host-reset/control.sock`。未安装或不可用时 HMC 不宣告重置能力。

重置保留源码、网络、已安装的 web 依赖和锁文件、HMC wrapper；清除其余 DSH home 和 HMC 部署状态、列出的授权文件、明确归属的 cron 命令。执行前冻结并终止 runner 后代及带任务标记的脱离进程，停止主服务；若仍有未知进程持有范围内可写文件描述符，则拒绝删除。之后以支持的 `dsh --profile web` 首次配置模式启动，不加载旧模型授权、个人 patch、凭据依赖的 intelligence guard 或 HMC 配对。管理员需重新配置模型、受保护生产入口和手机配对。

同 UUID 准备和提交去重，跨操作互斥。准备的提交窗口为 15 分钟；重启不会自动提交，也不会重放中断的删除。失败记录和手机数据保留，由管理员核查；完成证据在手机持久确认前保持可查询。确认后退休新生成的临时 TLS 凭据。取消操作可安全回收，不清除两端业务数据。

Linux 检查：`python3 -m unittest discover -s intel/host-reset/tests -v`。Android 系统清除会终止本机进程，最终真机证据通过外部 ADB 和首次配置页面收集。
