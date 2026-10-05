# Heartbeat 巡检清单
# 执行方式：逐项跑命令，每项一句话结论（正常/异常）；异常项用 sysevents_emit 发事件（type: heartbeat.anomaly, severity: high）
# 设计原则：只查真实出过事的地方（incident-driven），每项都有明确的"正常"标准；不查虚荣指标

- dsh-prod 存活：systemctl is-active dsh-prod 应为 active；NRestarts 应为 0（09-29 mock 泄漏曾让生产发假回复，崩过必须标异常）
- HMC 控制链路：ss -tlnp | grep 43197 应有 100.73.148.102:43197 在 LISTEN（手机经 Tailscale 控制全靠它）
- cron 任务健康：先调用 joblog_status 和 joblog_alerts，核对 job_runs.json 最新状态与告警时间；需确认退出码或未结束运行时，只读 /root/.dsh/intel-joblog/route-runs.jsonl 最近一小时记录。无 finish 的 start 只能记为未确认完成；upkeep-backoff、upkeep-no-signal 是安静跳过，不是失败。历史失败告警须保留其日期，不把旧告警说成新故障。
- 磁盘余量：df -h / | tail -1，可用应 > 2G（30G 盘只剩 7.3G，snapd 曾用 1.5G）
- 内存余量：free -m | grep Mem，available 应 > 100M（总量 890M，snapd 看门狗曾把整机拖死）
- dsh-prod 错误：journalctl -u dsh-prod --since "1 hour ago" -p err --no-pager | head -5，有新增错误行则标异常
- 隧道代理（可选）：仅当需要上机时查；Connection closed 只代表"我连不上"，不代表服务器有问题，不要误报
