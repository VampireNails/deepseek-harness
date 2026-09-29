// policy.js —— 分级策略的配置合并层。
//
// Config（见 index.js）允许覆盖：
//   levels:      { 工具名: 级别 }，覆盖默认分类表（非法级别值被丢弃）
//   blocked:     [正则字符串…]，追加到高危模式（非法正则在编译时丢弃）
//   sensitive:   [[正则, 说明]…] 或 {正则: 说明}，追加到敏感模式
//   execArgs:    { 工具名: [参数名…] }，扩展命令文本抽取
//   defaultLevel: 未知工具的默认级别（默认 WRITE，fail-closed）
//   monitor:     true 时只记录不拦截（灰度/审计模式）

import {
  LEVELS,
  DANGEROUS_PATTERNS,
  SENSITIVE_PATTERNS,
  DEFAULT_LEVELS,
  EXEC_ARG_NAMES,
  isValidLevel,
} from "./classify.js";

function normalizeSensitive(extra) {
  const out = [];
  if (Array.isArray(extra)) {
    for (const e of extra) {
      if (Array.isArray(e) && typeof e[0] === "string" && typeof e[1] === "string") {
        out.push([e[0], e[1]]);
      }
    }
  } else if (extra && typeof extra === "object") {
    for (const [k, v] of Object.entries(extra)) {
      if (typeof k === "string" && typeof v === "string") out.push([k, v]);
    }
  }
  return out;
}

/** 合并插件配置与内置默认，产出 classify() 消费的 policy 对象。 */
export function buildPolicy(config = {}) {
  const cfg = config || {};

  const levels = {};
  for (const [k, v] of Object.entries(cfg.levels || {})) {
    if (typeof k === "string" && isValidLevel(v)) levels[k] = v;
    // 非法级别直接丢弃 → 回落默认，绝不因配置笔误放行
  }

  const blockedPatterns = [...DANGEROUS_PATTERNS];
  for (const s of cfg.blocked || []) {
    if (typeof s === "string" && s) blockedPatterns.push(s);
  }

  const sensitivePatterns = [...SENSITIVE_PATTERNS, ...normalizeSensitive(cfg.sensitive)];

  const execArgNames = { ...EXEC_ARG_NAMES };
  for (const [k, v] of Object.entries(cfg.execArgs || {})) {
    if (typeof k === "string" && Array.isArray(v) && v.every((x) => typeof x === "string")) {
      execArgNames[k] = v;
    }
  }

  const defaultLevel = isValidLevel(cfg.defaultLevel) ? cfg.defaultLevel : LEVELS.WRITE;

  return {
    levels: Object.freeze({ ...levels }),
    mergedLevels: Object.freeze({ ...DEFAULT_LEVELS, ...levels }),
    blockedPatterns: Object.freeze(blockedPatterns),
    sensitivePatterns: Object.freeze(sensitivePatterns),
    execArgNames: Object.freeze({ ...execArgNames }),
    defaultLevel,
    monitor: cfg.monitor === true,
  };
}

/** 默认策略（无任何覆盖）。 */
export const DEFAULT_POLICY = buildPolicy();
