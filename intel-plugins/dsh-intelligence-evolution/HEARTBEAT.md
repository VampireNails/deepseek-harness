# HEARTBEAT.md — 每小时巡检清单

_每行一项。空行和 # 开头行会被忽略。清单为空时 heartbeat 自动跳过（不烧 token）。_

# 示例（按需修改）：
# - 检查 dsh web-intel 服务是否在监听 3080（ss -ltn | grep 3080）
# - 检查磁盘使用率是否超过 85%（df -h / | tail -1）
# - 检查记忆数据库大小是否异常（ls -lh ~/.dsh/intel-memory/memory.db）
