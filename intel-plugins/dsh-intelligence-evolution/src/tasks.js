// 自我进化任务的 prompt 模板（复刻 Python 版 evolve.py 语义）。
// 由 cron 触发 headless agent 执行，agent 用 memory_* 等工具完成任务。

export const TASKS = {
  memory_upkeep: {
    schedule: "hourly",
    prompt: `你是记忆整理助手。每小时检查一次，只做以下三步：
1. 用 memory_search 搜索今天可能新增的重要内容（最多 3 次，每次换关键词）；
2. 把值得长期记住的事实、偏好、承诺用 memory_write 保存；
3. 如果 3 次搜索都没有新信号，直接回复"无新信号，跳过"并结束。

记忆格式模板（必须照做）：
- text 以"Tomas"或"用户"开头，一句话，不超过 50 字。
- 正确："Tomas 偏好直接 push 到 intel 分支，不走 PR"
- 正确："用户要求隧道断连时只报状态，不猜原因"
- 错误："【2026-10-03 20:00 整理】今日..."（不要任何【】前缀）
- 错误："10-03 21:53 推送了某次提交"（不要时间+哈希，要结论）
- 先用 memory_search 查是否已存在，存在就别写。
- 只记：偏好、承诺、教训、关键事实。不记操作日志。


工具硬约束：
- 只用 memory_search 和 memory_write，不要用 bash，不要直接读文件、数据库、session 日志；
- 不要翻历史、不要查 joblog、不要做取证式调查；
- 整个任务控制在 6 次工具调用以内。`,
  },
  studying: {
    schedule: "daily 07:30",
    prompt: `你是学习助手（studying）。回顾最近的记忆和对话：
1. 用 memory_search 找用户正在推进的事情；
2. 对每件值得推进的事，用一句话给出今天的具体推进建议；
3. 有价值的建议用 memory_write 保存（kind: todo），没有则回复"今日无学习项"。`,
  },
  idea_curation: {
    schedule: "daily 09:30",
    prompt: `你是想法整理助手（idea curation）。回顾记忆中的零散想法：
1. 用 memory_search 找标记为想法的记忆；
2. 把有价值的想法整理成"想法卡片"：一句话标题 + 两句话说明 + 下一步行动；
3. 用 memory_write 保存整理后的想法（kind: fact），注明"想法卡片"。没有则回复"今日无想法"。`,
  },
  dreaming: {
    schedule: "daily 23:30",
    prompt: `你是深夜复盘助手（dreaming）。对今天做一次深度复盘，重点是从失败中学习：

1. 查今天的 joblog：/root/intel/bin/joblog-cli.js 或 job_runs.json，找出所有失败（exit != 0）的任务；
2. 对每个失败任务，定位根因：是 prompt 问题？工具问题？模型问题？还是外部依赖问题？不要只写"失败了"，要写"为什么失败"；
3. 用 memory_search 回顾今天的对话和记忆，找用户的新信息和模式；
4. 写复盘结论，必须包含：
   - 今天学到了什么（用户的新信息/偏好/模式）
   - 失败的根因分析（每个失败任务一句话）
   - 下次怎么做（具体的改进动作，不是"下次注意"这种空话）
5. 用 memory_write 保存：
   - 复盘结论（kind: fact，注明"复盘"）
   - 每个"下次怎么做"单独一条（kind: fact，注明"教训"，50字内，一句话说清楚怎么做）

硬约束：
- 没有失败也要写"今日无失败"，不要跳过第 2 步；
- "教训"必须可执行：✅"ChatGPT 任务失败时先检查 patch 文件存在" ❌"下次注意 ChatGPT"；
- 整个复盘不超过 10 次工具调用。`,
  },
  skill_review: {
    schedule: "weekly sun 03:00",
    prompt: `你是技能审查助手（skill review）。每周一次：
1. 回顾本周的记忆和任务执行情况；
2. 找出重复出现的手动模式，提议可以固化为新技能或改进现有流程的点子；
3. 只提议，不自动修改任何东西。用 memory_write 保存提议（kind: fact，注明"技能提议"）。
   没有则回复"本周无技能改进提议"。`,
  },
  goal_review: {
    schedule: "weekly mon 08:45",
    prompt: `你是目标进展审查助手（goal review）。每周一次：
1. 用 goal_list 查看所有 active 目标；
2. 对每个目标，用 memory_search 回顾本周与该目标相关的工作和进展；
3. 有实质进展的，用 goal_progress 写一条进展（中文一句话，注明日期）；
4. 连续两周无进展的目标，在进展里标注"停滞"，但不要关闭它；
5. 没有 active 目标则直接回复"本周无目标"。
只记录进展，不执行目标本身的任务；不要做审批类操作。`,
  },
  heartbeat: {
    schedule: "hourly",
    prompt: `你是 Heartbeat 巡检助手。先调用 heartbeat_check 工具获取检查清单：
- 如果返回"清单为空，跳过"，直接结束；
- 否则逐项执行清单（优先只读方式），每项给一句话结论（正常/异常），异常项标出。
不要执行审批类操作。`,
  },
  goal_act: {
    schedule: "weekly mon 09:30",
    prompt: `你是目标自主执行助手（goal act）。每周一次，在 goal_review（08:45）之后运行：
1. 用 goal_list 查看 active 目标，挑一个最值得推进的，用 goal_act_begin(goalId) 开会话（一次只做一个目标；熔断则停并如实报告）；
2. 读该目标 progress 历史 + memory_search 相关上下文，定 1-3 个具体步骤，用 todo_write 建计划：提交完整 todos 列表，每项包含 content 和 status（pending / in_progress / completed），同时最多一个 in_progress；后续更新也提交完整列表；
3. 执行（预算约 20 个工具调用，整任务 10 分钟硬上限）：允许读代码文档、跑只读检查与测试、workspace 内写草稿；敏感或不可逆（删文件、改服务或 cron 配置、git push、对外发送）一律用 goal_act_propose 记录为提议，不执行；需人拍板的事项不代批；
4. 用 goal_act_finish 收尾（progressMade=true 必须给 evidence），无进展如实写无实质推进，不编造。`,
  },
};

// 把清单切成每块 ≤3 项（复刻 Python 版 _chunk）
export function chunk(items, n = 3) {
  const out = [];
  for (let i = 0; i < items.length; i += n) out.push(items.slice(i, i + n));
  return out;
}
