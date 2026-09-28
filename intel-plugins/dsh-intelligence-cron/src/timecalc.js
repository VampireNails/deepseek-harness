// timecalc.js —— 在 IANA 时区内计算"下一个 H:M 触发点"（严格未来）。
//
// dsh-schedule 没有每天/每周语义：每天X / 每周X 由本插件实现为"一次性 at 链"——
// 每次触发后（dispatch 事件）或会话初始化对账时，计算下一个未来触发点并追加新的
// one-shot at。本模块只做纯时间计算，不碰 session。

/** epochMs 处该时区的 UTC 偏移（毫秒）。DST 安全。 */
function tzOffsetMs(timeZone, epochMs) {
  const dtf = new Intl.DateTimeFormat("en-US", {
    timeZone, hourCycle: "h23",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
  const parts = {};
  for (const p of dtf.formatToParts(new Date(epochMs))) parts[p.type] = p.value;
  const asUTC = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second);
  return asUTC - Math.floor(epochMs / 1000) * 1000;
}

/** epochMs 在该时区的 Y/M/D。 */
function tzYMD(timeZone, epochMs) {
  const dtf = new Intl.DateTimeFormat("en-CA", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit",
  });
  const parts = {};
  for (const p of dtf.formatToParts(new Date(epochMs))) parts[p.type] = p.value;
  return { year: +parts.year, month: +parts.month, day: +parts.day };
}

const DOW_EN = { Sun: 7, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };

/** epochMs 在该时区的周几：1=周一 … 7=周日。 */
function tzWeekday(timeZone, epochMs) {
  const dtf = new Intl.DateTimeFormat("en-US", { timeZone, weekday: "short" });
  return DOW_EN[dtf.format(new Date(epochMs))] ?? 1;
}

/** 把时区本地墙钟 (y,mo,d,h,mi) 换算为 epoch 毫秒。 */
function wallToEpochMs(timeZone, y, mo, d, h, mi) {
  const wall = Date.UTC(y, mo - 1, d, h, mi, 0, 0);
  let guess = wall - tzOffsetMs(timeZone, wall);
  for (let k = 0; k < 3; k++) {
    const next = wall - tzOffsetMs(timeZone, guess);
    if (next === guess) break;
    guess = next;
  }
  return guess;
}

function addDays(ymd, n) {
  const d = new Date(Date.UTC(ymd.year, ymd.month - 1, ymd.day + n));
  return { year: d.getUTCFullYear(), month: d.getUTCMonth() + 1, day: d.getUTCDate() };
}

/**
 * 下一个 H:M 触发点（严格大于 nowMs），返回 epoch 毫秒。
 * @param opts { hour, minute, dayOfWeek? } dayOfWeek 1=周一…7=周日；缺省为每天。
 */
export function nextOccurrence(opts, timeZone, nowMs) {
  const { hour, minute, dayOfWeek } = opts;
  const today = tzYMD(timeZone, nowMs);
  if (dayOfWeek !== undefined) {
    // 每周：按周步进（今天是目标周几但时刻已过 → 下周）
    const daysAhead = (dayOfWeek - tzWeekday(timeZone, nowMs) + 7) % 7;
    for (let w = 0; w < 3; w++) {
      const d = addDays(today, daysAhead + w * 7);
      const target = wallToEpochMs(timeZone, d.year, d.month, d.day, hour, minute);
      if (target > nowMs) return target;
    }
    throw new Error("无法计算下一次触发时间");
  }
  // 每天：今天或明天
  for (let i = 0; i < 2; i++) {
    const d = addDays(today, i);
    const target = wallToEpochMs(timeZone, d.year, d.month, d.day, hour, minute);
    if (target > nowMs) return target;
  }
  throw new Error("无法计算下一次触发时间");
}

/** 触发点在该时区的日期串 YYYYMMDD（用作 schedule id 的 slot 后缀，保证幂等）。 */
export function slotDate(timeZone, epochMs) {
  const { year, month, day } = tzYMD(timeZone, epochMs);
  return `${year}${String(month).padStart(2, "0")}${String(day).padStart(2, "0")}`;
}

/** epoch 毫秒 → dsh-schedule 要求的 canonical RFC3339 UTC instant（YYYY-MM-DDTHH:mm:ss.sssZ）。 */
export function toInstant(epochMs) {
  return new Date(epochMs).toISOString();
}

/** epoch 毫秒 / ISO 串 → 该时区的 "YYYY-MM-DD HH:mm"（展示用）。 */
export function formatLocal(timeZone, msOrIso) {
  const ms = typeof msOrIso === "string" ? Date.parse(msOrIso) : msOrIso;
  const dtf = new Intl.DateTimeFormat("en-CA", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  });
  const parts = {};
  for (const p of dtf.formatToParts(new Date(ms))) parts[p.type] = p.value;
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}
