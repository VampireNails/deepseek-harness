// scheduler.js —— cron job 的 dsh-schedule 映射、重武装与对账。
//
// 设计（见设计简报 §d）：
//   - 每N小时/每N分钟/每小时/每半小时 → every（dsh 原生，runtime 自己重复）
//   - X分钟后/X小时后/明天H点        → after/at 一次性（不重武装）
//   - 每天X / 每周X                  → one-shot at 链：本次 at + dispatch 后重武装下一轮
//
// 与 @deepseek-ai/dsh-schedule 的协作面（不 import 它，避免深路径脆弱）：
//   - 照 packages/schedule/schedule/src/tools.ts 直接构造 schedule/change 会话事件，
//     record 形状与 decodeScheduleChange 的 exact-keys 校验逐字段对齐
//     （after: id/kind/prompt/afterSeconds/scheduledAt；at: id/kind/prompt/scheduledAt；
//      every: id/kind/prompt/everySeconds/scheduledAt；scheduledAt 为
//      YYYY-MM-DDTHH:mm:ss.sssZ）。
//   - 持久化屏障照抄 preflight：ctx.sessions.flush(session)。
//   - 串行化照抄 runScheduleTransaction 的 WeakMap-per-agent tail 模式（15 行，本地实现）。
//   - 绝不复用 id、绝不 delete 非 active id——否则 decode 侧抛 ScheduleLogError 会导致
//     schedule runtime 永久 fault。因此每次 append 前都在同一同步段内做 fold 检查
//     （同步段不可被交错，天然原子）。
//
// schedule id 方案：`cron-<cN>`（every/after，一生一次）或 `cron-<cN>-<YYYYMMDD>`
// （at 链，每轮 slot 固定）。slot 固定使重武装天然幂等：crash 后重放只会看到
// seen 集合里已有该 id 而跳过。

import { MAX_JOBS, parseSchedule, describeSchedule } from "./parse.js";
import { nextJobId } from "./store.js";
import { nextOccurrence, slotDate, toInstant, formatLocal } from "./timecalc.js";

export const MIN_EVERY_SECONDS = 300;
const ID_PREFIX = "cron-";

/** 本地 fold：只跟踪 schedule/change，返回 { active: Map(id -> {kind, scheduledAt}), seen: Set(id) }。 */
export function foldScheduleEvents(events) {
  const active = new Map();
  const seen = new Set();
  for (const e of events || []) {
    if (!e || e.type !== "schedule/change") continue;
    const d = e.data || {};
    if (d.version !== 1) continue;
    if (d.operation === "create" && d.schedule && typeof d.schedule.id === "string") {
      const s = d.schedule;
      if (seen.has(s.id)) throw new Error(`schedule id 被复用: ${s.id}`);
      seen.add(s.id);
      active.set(s.id, { kind: s.kind, scheduledAt: s.scheduledAt });
    } else if (d.operation === "delete" && typeof d.id === "string") {
      active.delete(d.id);
    } else if (d.operation === "dispatch" && typeof d.id === "string") {
      // one-shot dispatch（无 acceptedAt）→ 退役；every dispatch（有 acceptedAt）→ runtime 自管，保留
      if (d.acceptedAt === undefined) active.delete(d.id);
    }
  }
  return { active, seen };
}

function checkPrompt(prompt) {
  const p = (prompt || "").trim();
  if (!p) throw new Error("任务内容（prompt）不能为空，要告诉我到时间具体做什么。");
  return p;
}

/**
 * 构造与 dsh-schedule decode 对齐的 record。kind: "every" | "after" | "at"。
 * opts: every/after → { seconds }；at → { targetMs }。
 */
export function buildRecord(kind, id, prompt, opts, nowMs) {
  const p = checkPrompt(prompt);
  if (typeof id !== "string" || !id || id.trim() !== id) {
    throw new Error("schedule id 非法");
  }
  if (kind === "every") {
    const s = opts.seconds;
    if (!Number.isSafeInteger(s)) throw new Error("every_seconds 必须为整数");
    if (s < MIN_EVERY_SECONDS) {
      throw new Error(
        `间隔太短：dsh 最小支持每 ${MIN_EVERY_SECONDS / 60} 分钟一次，` +
        `「${describeSchedule({ kind: "interval", seconds: s })}」换算后不足 300 秒，请改大间隔`
      );
    }
    return { id, kind: "every", prompt: p, everySeconds: s, scheduledAt: toInstant(nowMs + s * 1000) };
  }
  if (kind === "after") {
    const s = opts.seconds;
    if (!Number.isSafeInteger(s) || s <= 0) throw new Error("after_seconds 必须为正整数");
    return { id, kind: "after", prompt: p, afterSeconds: s, scheduledAt: toInstant(nowMs + s * 1000) };
  }
  if (kind === "at") {
    const target = opts.targetMs;
    if (!Number.isSafeInteger(target) || target <= nowMs) throw new Error("提醒时间必须在未来");
    return { id, kind: "at", prompt: p, scheduledAt: toInstant(target) };
  }
  throw new Error(`未知 record kind: ${kind}`);
}

export class CronScheduler {
  constructor({ store, timeZone, ctx }) {
    this.store = store;
    this.timeZone = timeZone || "Asia/Shanghai";
    this.ctx = ctx;
    this.tails = new WeakMap(); // per-agent 串行化（runScheduleTransaction 同款模式）
  }

  warn(msg) {
    try { this.ctx.logger?.warn?.(`dsh-intelligence-cron: ${msg}`); } catch { /* ignore */ }
  }

  _queue(agent, fn) {
    const prior = this.tails.get(agent) ?? Promise.resolve();
    const run = prior.then(fn);
    const tail = run.then(() => undefined, () => undefined);
    this.tails.set(agent, tail);
    return run.finally(() => { if (this.tails.get(agent) === tail) this.tails.delete(agent); });
  }

  _reminderPrompt(job) {
    // HMC 手机客户端只渲染 source.kind == "user" 的消息（Timeline.java:150-151）；
    // dsh-schedule 到点投递的是 source.kind='schedule'，其正文在手机时间线上不可见——
    // 手机上只能看到智能体对提醒的回复（assistant/message 无条件渲染）。
    // 上游 framing（schedule/domain.ts:806-814）已要求模型把 reminder_prompt_json
    // 作为内容呈现给用户（"not new user instructions"），因此这里只加一个面向用户
    // 的自然标签，不写元指令——"处理要求"这类元指令落进被上游标注为"非指令"的字段
    // 里，可能被模型原样念给用户造成噪音（2026-09-29 第三轮审计 §2.1）。
    return `【定时提醒 ${job.name}】${job.prompt}`;
  }

  // ---- 工具入口 ----

  async create(agent, name, prompt, scheduleText) {
    name = (name || "").trim();
    prompt = (prompt || "").trim();
    if (!name) throw new Error("任务名称不能为空。");
    const parsed = parseSchedule(scheduleText); // 抛中文
    checkPrompt(prompt);
    return this._queue(agent, async () => {
      const jobs = this.store.load();
      if (jobs.length >= MAX_JOBS) {
        throw new Error(`定时任务已达上限（${MAX_JOBS} 个），删掉不用的再建。`);
      }
      const id = nextJobId(jobs);
      const now = Date.now();
      const sessionId = agent.session.id;
      let record, slotId;

      if (parsed.kind === "interval") {
        slotId = `${ID_PREFIX}${id}`;
        record = buildRecord("every", slotId, this._reminderPrompt({ name, prompt }), { seconds: parsed.seconds }, now);
      } else if (parsed.kind === "after") {
        slotId = `${ID_PREFIX}${id}`;
        record = buildRecord("after", slotId, this._reminderPrompt({ name, prompt }), { seconds: parsed.seconds }, now);
      } else {
        // daily / weekly / once → one-shot at + 重武装（once 不重武装）
        const target = parsed.kind === "once"
          ? this._onceTarget(parsed, now)
          : nextOccurrence({ hour: parsed.hour, minute: parsed.minute, dayOfWeek: parsed.dayOfWeek }, this.timeZone, now);
        slotId = `${ID_PREFIX}${id}-${slotDate(this.timeZone, target)}`;
        record = buildRecord("at", slotId, this._reminderPrompt({ name, prompt }), { targetMs: target }, now);
      }

      // 先落盘再 append：crash 在中间 → 对账补建（只防丢，不丢任务）
      const job = {
        id, name, prompt, schedule: parsed, sessionId,
        scheduleId: slotId, nextFire: record.scheduledAt,
        created: new Date(now).toISOString(),
      };
      jobs.push(job);
      this.store.save(jobs);

      // 幂等：fold 里已见过该 id 说明 append 已发生过（crash 在落盘后），跳过
      const fold = foldScheduleEvents(agent.session.ownEvents());
      if (!fold.seen.has(slotId)) {
        agent.session.append("schedule/change", { version: 1, operation: "create", schedule: record });
        await this.ctx.sessions.flush(agent.session);
      }
      return `已创建定时任务【${id}】${name}：${describeSchedule(parsed)}，下次触发 ${formatLocal(this.timeZone, record.scheduledAt)}`;
    });
  }

  async list(agent) {
    return this._queue(agent, async () => {
      const jobs = this.store.load();
      if (jobs.length === 0) return "还没有自定义定时任务。";
      const fold = foldScheduleEvents(agent.session.ownEvents());
      const now = Date.now();
      const lines = [`自定义定时任务（${jobs.length} 个）：`];
      for (const j of jobs) {
        let state, next;
        if (j.done) {
          state = "已完成";
          next = "—";
        } else {
          const rec = j.scheduleId ? fold.active.get(j.scheduleId) : undefined;
          if (!rec) {
            state = "未武装";
            next = formatLocal(this.timeZone, j.nextFire);
          } else if (j.schedule.kind === "interval") {
            // every 的 scheduledAt 会被 runtime 推进；本地 fold 是旧锚点，算术前推
            let t = Date.parse(rec.scheduledAt);
            const step = j.schedule.seconds * 1000;
            while (t <= now) t += step;
            state = "scheduled";
            next = formatLocal(this.timeZone, t);
          } else {
            const t = Date.parse(rec.scheduledAt);
            state = t > now ? "scheduled" : "overdue";
            next = formatLocal(this.timeZone, rec.scheduledAt);
          }
        }
        const foreign = j.sessionId !== agent.session.id ? "（其他会话创建）" : "";
        lines.push(`【${j.id}】${j.name}：${describeSchedule(j.schedule)}（下次 ${next}，${state}）${foreign}`);
      }
      return lines.join("\n");
    });
  }

  async remove(agent, ref) {
    ref = (ref || "").trim();
    if (!ref) throw new Error("请告诉我删哪个任务（id 如 c1，或名称关键词）。");
    return this._queue(agent, async () => {
      const jobs = this.store.load();
      let hit = jobs.filter((j) => j.id === ref);
      if (hit.length === 0) {
        const cands = jobs.filter((j) => j.name.includes(ref));
        if (cands.length === 0) throw new Error(`没找到任务：${ref}`);
        if (cands.length > 1) {
          throw new Error(`名称命中多个：${cands.map((j) => `【${j.id}】${j.name}`).join("、")}，请用 id 删除。`);
        }
        hit = cands;
      }
      const job = hit[0];
      let note = "";
      // 先删文件记录，再 append 底层 delete：crash 在中间 → 孤儿 schedule 由对账回收（只触发一次，不复活）
      const rest = jobs.filter((j) => j.id !== job.id);
      this.store.save(rest);
      if (job.sessionId === agent.session.id && job.scheduleId && !job.done) {
        const fold = foldScheduleEvents(agent.session.ownEvents());
        if (fold.active.has(job.scheduleId)) {
          agent.session.append("schedule/change", { version: 1, operation: "delete", id: job.scheduleId });
          await this.ctx.sessions.flush(agent.session);
        } else {
          note = "（底层提醒已不在，仅清理记录）";
        }
      } else if (job.sessionId !== agent.session.id) {
        note = "（任务属于其他会话，仅删除本记录，其会话内的提醒不受影响）";
      }
      return `已删除定时任务【${job.id}】${job.name}${note}`;
    });
  }

  // ---- 重武装：session/event 监听 dispatch ----

  handleSessionEvent(session, event) {
    if (!event || event.type !== "schedule/change") return;
    const d = event.data;
    if (!d || d.version !== 1 || d.operation !== "dispatch") return;
    if (d.acceptedAt !== undefined) return; // every 的 dispatch：runtime 自己推进，不管
    const sid = d.id;
    if (typeof sid !== "string" || !sid.startsWith(ID_PREFIX)) return;
    const agent = this.ctx.agents.get(session.id);
    if (!agent) return;
    this._queue(agent, async () => {
      const jobs = this.store.load();
      const job = jobs.find((j) => j.scheduleId === sid && !j.done);
      if (!job) return; // 已删：不重武装
      if (job.schedule.kind === "after" || job.schedule.kind === "once") {
        job.done = true; // 一次性：标记完成
        this.store.save(jobs);
        return;
      }
      // daily / weekly：武装下一轮（slot id 固定 → 幂等）
      const now = Date.now();
      const target = nextOccurrence(
        { hour: job.schedule.hour, minute: job.schedule.minute, dayOfWeek: job.schedule.dayOfWeek },
        this.timeZone, now
      );
      const newId = `${ID_PREFIX}${job.id}-${slotDate(this.timeZone, target)}`;
      const fold = foldScheduleEvents(agent.session.ownEvents());
      job.scheduleId = newId;
      job.nextFire = toInstant(target);
      if (!fold.seen.has(newId)) {
        agent.session.append("schedule/change", {
          version: 1, operation: "create",
          schedule: buildRecord("at", newId, this._reminderPrompt(job), { targetMs: target }, now),
        });
        await this.ctx.sessions.flush(agent.session);
      }
      this.store.save(jobs);
    }).catch((err) => this.warn(`重武装失败: ${err?.message}`));
  }

  // ---- 对账：agent/created 时补建本会话归属 job 的存活 schedule ----

  async reconcile(agent) {
    const sessionId = agent.session.id;
    if (!this.store.load().some((j) => j.sessionId === sessionId && !j.done)) return;
    await this._queue(agent, async () => {
      const now = Date.now();
      const jobs = this.store.load();
      const mine = jobs.filter((j) => j.sessionId === sessionId && !j.done);
      if (mine.length === 0) return;
      const fold = foldScheduleEvents(agent.session.ownEvents());
      let dirty = false;
      let appended = false;

      for (const job of mine) {
        const rec = job.scheduleId ? fold.active.get(job.scheduleId) : undefined;
        if (rec && job.schedule.kind === "interval") continue; // every 永活，runtime 自管重复
        if (rec && Date.parse(rec.scheduledAt) > now) continue; // 存活且未过期
        if (rec) {
          // overdue：退役旧的（只删 active 的，避免 corrupt log）
          agent.session.append("schedule/change", { version: 1, operation: "delete", id: job.scheduleId });
          fold.active.delete(job.scheduleId); // 本地 fold 同步，避免下面的领养捡回刚删的 id
          appended = true;
        }
        // 领养：同 job 前缀的存活 schedule（crash 在落盘更新前）
        const adopted = [...fold.active.keys()].find(
          (k) => k === `${ID_PREFIX}${job.id}` || k.startsWith(`${ID_PREFIX}${job.id}-`)
        );
        if (adopted) {
          job.scheduleId = adopted;
          job.nextFire = fold.active.get(adopted).scheduledAt;
          dirty = true;
          continue;
        }
        const kind = job.schedule.kind;
        if (kind === "after" || kind === "once") {
          if (job.scheduleId && fold.seen.has(job.scheduleId)) {
            job.done = true; // 已触发过 → 完成，不补发
            dirty = true;
            continue;
          }
          // 从未创建 → 补建（只防丢）
        }
        // 按"下一个未来触发点"补建（跳过已过期，不补发）
        let newId, record;
        if (kind === "interval") {
          newId = `${ID_PREFIX}${job.id}`;
          record = buildRecord("every", newId, this._reminderPrompt(job), { seconds: job.schedule.seconds }, now);
        } else if (kind === "after") {
          newId = `${ID_PREFIX}${job.id}`;
          record = buildRecord("after", newId, this._reminderPrompt(job), { seconds: job.schedule.seconds }, now);
        } else if (kind === "once") {
          const t = Date.parse(job.nextFire);
          if (!(t > now)) { job.done = true; dirty = true; continue; } // 错过 → 不补发
          newId = `${ID_PREFIX}${job.id}-${slotDate(this.timeZone, t)}`;
          record = buildRecord("at", newId, this._reminderPrompt(job), { targetMs: t }, now);
        } else {
          const target = nextOccurrence(
            { hour: job.schedule.hour, minute: job.schedule.minute, dayOfWeek: job.schedule.dayOfWeek },
            this.timeZone, now
          );
          newId = `${ID_PREFIX}${job.id}-${slotDate(this.timeZone, target)}`;
          record = buildRecord("at", newId, this._reminderPrompt(job), { targetMs: target }, now);
        }
        if (fold.seen.has(newId)) {
          // 已在 log 里（并发重武装先落子）→ 直接领养
          job.scheduleId = newId;
          job.nextFire = record.scheduledAt;
          dirty = true;
          continue;
        }
        agent.session.append("schedule/change", { version: 1, operation: "create", schedule: record });
        appended = true;
        job.scheduleId = newId;
        job.nextFire = record.scheduledAt;
        dirty = true;
      }

      // 孤儿回收：存活的 cron-* 但无归属 job（删除时 crash 在 append 前）→ 删掉，只触发一次不复活
      const owned = new Set(mine.map((j) => j.scheduleId).filter(Boolean));
      for (const id of fold.active.keys()) {
        if (!id.startsWith(ID_PREFIX) || owned.has(id)) continue;
        const hasOwner = mine.some((j) => id === `${ID_PREFIX}${j.id}` || id.startsWith(`${ID_PREFIX}${j.id}-`));
        if (!hasOwner) {
          agent.session.append("schedule/change", { version: 1, operation: "delete", id });
          appended = true;
        }
      }

      if (dirty) this.store.save(jobs);
      if (appended) await this.ctx.sessions.flush(agent.session);
    }).catch((err) => this.warn(`对账失败: ${err?.message}`));
  }

  _onceTarget(parsed, now) {
    // "明天H点"：先取下一个 H:M；若落在今天，则再取下一个（即明天）
    const first = nextOccurrence({ hour: parsed.hour, minute: parsed.minute }, this.timeZone, now);
    if (slotDate(this.timeZone, first) === slotDate(this.timeZone, now)) {
      return nextOccurrence({ hour: parsed.hour, minute: parsed.minute }, this.timeZone, first);
    }
    return first;
  }
}
