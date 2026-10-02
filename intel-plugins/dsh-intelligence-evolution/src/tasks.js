// 自我进化任务的 prompt 模板（复刻 Python 版 evolve.py 语义）。
// 由 cron 触发 headless agent 执行，agent 用 memory_* 等工具完成任务。

export const TASKS = {
  memory_upkeep: {
    schedule: "hourly",
    prompt: `你是记忆整理助手。检查今天是否有新的对话记录或笔记值得整理：
1. 用 memory_search 搜索今天可能新增的重要内容；
2. 把值得长期记住的事实、偏好、承诺用 memory_write 保存（text 简明中文，kind 选最贴切的）；
3. 如果没有新信号，直接回复"无新信号，跳过"，不要写任何东西。`,
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
    prompt: `你是深夜复盘助手（dreaming）。对今天做一次深度复盘：
1. 用 memory_search 回顾今天的对话和记忆；
2. 写一段复盘：今天学到了什么用户的新信息？有什么模式？有什么需要调整的？
3. 把复盘结论用 memory_write 保存（kind: fact，注明"复盘"）；
4. 如果发现自己（助手）的行为模式需要改进，用一句话记下来（kind: fact，注明"自我改进"）。`,
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
};

// 把清单切成每块 ≤3 项（复刻 Python 版 _chunk）
export function chunk(items, n = 3) {
  const out = [];
  for (let i = 0; i < items.length; i += n) out.push(items.slice(i, i + n));
  return out;
}
