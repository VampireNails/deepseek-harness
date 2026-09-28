// parse.js —— 中文时间描述解析
//
// 完整移植自 /opt/deepseek-harness/agent/custom_cron.py 的 parse_schedule / _parse_hm
// （正则级联：每周X → 每N小时/每N分钟/每小时/每半小时 → 每天X），另新增 dsh 原生支持的
// 一次性表达：X分钟后 / X小时后 / 明天H点（Python 版没有）。
//
// 解析失败抛 Error（中文信息），调用方直接展示给用户。

export const MAX_JOBS = 20;

// 周几：1=周一 … 7=周日（与 Python _WEEK 对应 mon..sun）
const WEEK = { 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 日: 7, 天: 7 };
export const WEEK_CN = { 1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日" };

function parseHm(t) {
  // 9:30 / 9：30
  let m = t.match(/(\d{1,2})\s*[:：]\s*(\d{1,2})/);
  if (m) return [parseInt(m[1], 10), parseInt(m[2], 10)];
  // 9点半
  m = t.match(/(\d{1,2})\s*点半/);
  if (m) return [parseInt(m[1], 10), 30];
  // 9点 / 9时
  m = t.match(/(\d{1,2})\s*[点时]/);
  if (m) return [parseInt(m[1], 10), 0];
  return null;
}

function checkHm(hour, minute) {
  if (!(hour >= 0 && hour <= 23 && minute >= 0 && minute <= 59)) {
    throw new Error("时间不合法：小时要在 0-23 之间，分钟要在 0-59 之间");
  }
}

/**
 * 解析中文时间描述 -> schedule 对象。失败抛中文 Error。
 *
 * 返回：
 *   { kind: "daily", hour, minute }            每天9点 / 每天9:30 / 每天9点半
 *   { kind: "weekly", dayOfWeek: 1-7, hour, minute }  每周一9点 / 星期三18:30
 *   { kind: "interval", seconds }               每2小时 / 每30分钟 / 每小时 / 每半小时
 *   { kind: "after", seconds }                  30分钟后 / 2小时后（一次性，Python 版没有）
 *   { kind: "once", hour, minute }              明天8点（一次性，Python 版没有）
 */
export function parseSchedule(text) {
  const t = (text || "").trim().replace(/：/g, ":");
  if (!t) {
    throw new Error("时间描述不能为空，例如：每天9点、每2小时、每周一9点");
  }

  // 每周X（含"星期X"）
  let m = t.match(/(?:每?周|星期)([一二三四五六日天])/);
  if (m) {
    const hm = parseHm(t);
    if (!hm) throw new Error("每周任务需要指定时间，例如：每周一9点、每周三18:30");
    checkHm(hm[0], hm[1]);
    return { kind: "weekly", dayOfWeek: WEEK[m[1]], hour: hm[0], minute: hm[1] };
  }

  // X分钟后 / X小时后（一次性）
  m = t.match(/(\d+)\s*分(钟)?后/);
  if (m) {
    const n = parseInt(m[1], 10);
    if (!(n >= 1 && n <= 43200)) throw new Error("X分钟后：分钟数要在 1-43200 之间");
    return { kind: "after", seconds: n * 60 };
  }
  m = t.match(/(\d+)\s*小时后/);
  if (m) {
    const n = parseInt(m[1], 10);
    if (!(n >= 1 && n <= 720)) throw new Error("X小时后：小时数要在 1-720 之间");
    return { kind: "after", seconds: n * 3600 };
  }

  // 明天H点（一次性）
  if (t.includes("明天")) {
    const hm = parseHm(t);
    if (!hm) throw new Error("明天任务需要指定时间，例如：明天8点、明天9:30");
    checkHm(hm[0], hm[1]);
    return { kind: "once", hour: hm[0], minute: hm[1] };
  }

  // 每N小时 / 每N分钟
  m = t.match(/每\s*(\d+)\s*小时/);
  if (m) {
    const n = parseInt(m[1], 10);
    if (!(n >= 1 && n <= 24)) throw new Error("每N小时：N 要在 1-24 之间");
    return { kind: "interval", seconds: n * 3600 };
  }
  m = t.match(/每\s*(\d+)\s*分(钟)?/);
  if (m) {
    const n = parseInt(m[1], 10);
    if (!(n >= 1 && n <= 1440)) throw new Error("每N分钟：N 要在 1-1440 之间");
    return { kind: "interval", seconds: n * 60 };
  }
  if (t.includes("每小时")) return { kind: "interval", seconds: 3600 };
  if (t.includes("每半小时")) return { kind: "interval", seconds: 1800 };

  // 每天X / 每日X
  if (t.includes("每天") || t.includes("每日")) {
    const hm = parseHm(t);
    if (!hm) throw new Error("每天任务需要指定时间，例如：每天9点、每天9:30、每天9点半");
    checkHm(hm[0], hm[1]);
    return { kind: "daily", hour: hm[0], minute: hm[1] };
  }

  throw new Error(
    `看不懂这个时间描述：${text}\n支持的说法：每天9点 / 每天9:30 / 每天9点半 / ` +
    `每2小时 / 每30分钟 / 每周一9点 / 每周三18:30 / 30分钟后 / 2小时后 / 明天8点`
  );
}

/** 中文描述（cron_list / 创建回执用，对标 Python _desc）。 */
export function describeSchedule(s) {
  if (s.kind === "interval") {
    if (s.seconds % 3600 === 0) return `每 ${s.seconds / 3600} 小时`;
    return `每 ${Math.round(s.seconds / 60)} 分钟`;
  }
  if (s.kind === "after") {
    if (s.seconds % 3600 === 0) return `${s.seconds / 3600} 小时后（一次性）`;
    return `${Math.round(s.seconds / 60)} 分钟后（一次性）`;
  }
  const hm = `${s.hour}:${String(s.minute).padStart(2, "0")}`;
  if (s.kind === "weekly") return `每${WEEK_CN[s.dayOfWeek]} ${hm}`;
  if (s.kind === "daily") return `每天 ${hm}`;
  if (s.kind === "once") return `明天 ${hm}（一次性）`;
  return hm;
}
