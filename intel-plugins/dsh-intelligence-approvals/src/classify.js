// classify.js —— 审批四级分级：纯函数，无副作用，可直接单元测试。
//
// 四级定义（复刻 Python 版 agent/permissions.py 的思路，按 dsh 原生工具名重制）：
//   READ    只读操作（read/grep/glob 等），免审批直接放行
//   WRITE   写操作（write/edit/memory_write 等），走审批（pre-execute 返回 ask）
//   EXEC    执行命令（bash/pwsh 等），走审批；命令参数先过高危模式扫描
//   BLOCKED 高危（删根/格盘/写裸设备/fork 炸弹/关机等），硬拦截直接拒绝，不弹卡
//
// 处置建议 disposition：allow（放行）/ approve（弹审批卡）/ block（拒绝）。
// 未知工具默认 WRITE（fail-closed：宁可多弹一次卡，也不放过未知写入）。

export const LEVELS = Object.freeze({
  READ: "READ",
  WRITE: "WRITE",
  EXEC: "EXEC",
  BLOCKED: "BLOCKED",
});

export const DISPOSITIONS = Object.freeze({
  allow: "allow",     // 放行
  approve: "approve", // 弹审批卡（HMC / Web 的 ask 通道）
  block: "block",     // 拒绝
});

/** 级别 → 处置建议。未知级别 fail-closed 走 approve。 */
export function levelDisposition(level) {
  switch (level) {
    case LEVELS.READ: return DISPOSITIONS.allow;
    case LEVELS.WRITE:
    case LEVELS.EXEC: return DISPOSITIONS.approve;
    case LEVELS.BLOCKED: return DISPOSITIONS.block;
    default: return DISPOSITIONS.approve;
  }
}

export function isValidLevel(level) {
  return level === LEVELS.READ || level === LEVELS.WRITE
    || level === LEVELS.EXEC || level === LEVELS.BLOCKED;
}

// ---------------------------------------------------------------------------
// 默认工具分类表：dsh 0.1.7-rc.1 原生工具 + intel 插件工具 + Python 版遗留别名
// ---------------------------------------------------------------------------

const READ_TOOLS = [
  // 文件/搜索只读
  "read", "read_image", "grep", "glob", "do_fetch",
  // Web 只读
  "web_fetch", "web_search",
  // 会话/事件只读
  "session_search", "session_event_read", "session_event_search",
  "session_event_trace", "session_trace",
  // 任务/终端只读
  "job_list", "job_output", "terminal_list", "terminal_read",
  // 发现类
  "list_agents", "list_subagent_models",
  "list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource",
  // 技能/呈现
  "skill", "present", "noop", "probe",
  // intel 插件只读工具
  "memory_read", "memory_search", "feed_read", "feed_render", "cron_list",
  "schedule_list", "todo_list", "goal_list",
  "artifact_get", "artifact_read", "artifact_versions",
  "hook_list",
  "browser_snapshot", "browser_downloads", "browser_screenshot",
  // 本插件自身（查分级不应弹卡）
  "approval_classify",
  // 交互式提问（本身即用户交互，无风险）
  "ask_user_question",
  // Python 版遗留别名
  "file_read",
];

const WRITE_TOOLS = [
  // 文件写入
  "write", "edit", "str_replace_editor",
  // 记忆/Feed/定时
  "memory_write", "feed_post",
  "cron_create", "cron_delete", "schedule_create", "schedule_delete",
  // 待办/目标/工件
  "todo_add", "todo_write", "artifact_save",
  "browser_open", "browser_click", "browser_fill", "browser_select",
  "browser_wait", "browser_close",
  "goal_create", "goal_log", "goal_progress", "goal_close",
  "hook_register", "hook_remove", "hook_fire",
  "create_goal", "update_goal",
  "team_task_create", "team_task_update",
  // 智能体间消息/派生
  "send_message", "spawn_teammate", "interrupt_agent",
  // 终端句柄关闭（可能顺带杀掉运行中的任务）
  "terminal_close",
  // 插件管理（改 profile bundles，敏感）
  "plugin_manager",
  // 代码智能（读写混合，保守按写处理）
  "lsp",
  // Python 版遗留别名
  "file_write", "file_append",
];

const EXEC_TOOLS = [
  "bash", "pwsh",
  "terminal_open", "terminal_send", "terminal_signal",
  "run_code",
  // Python 版遗留别名
  "exec", "shell_exec",
];

/** 默认分类表（工具名 → 级别）。policy.levels 可覆盖。 */
export const DEFAULT_LEVELS = Object.freeze(Object.fromEntries([
  ...READ_TOOLS.map((t) => [t, LEVELS.READ]),
  ...WRITE_TOOLS.map((t) => [t, LEVELS.WRITE]),
  ...EXEC_TOOLS.map((t) => [t, LEVELS.EXEC]),
]));

// ---------------------------------------------------------------------------
// 高危 shell 命令模式：命中即 BLOCKED（不问原因，直接拒）
// 复刻 Python 版 DANGEROUS_PATTERNS，另加 chmod 777 / 与 chown -R /。
// 与 Python 版的一处差异：rm -rf /、rm -rf /*、chmod 777 / 要求目标路径
// 后跟空白或结尾，避免 "rm -r /tmp/old" 被误判为删根（Python 版会误判）；
// 这类正常递归删除降为敏感模式走审批。
// ---------------------------------------------------------------------------

export const DANGEROUS_PATTERNS = Object.freeze([
  String.raw`\brm\s+(-[a-z]*r[a-z]*\s+)+/(\s|$)`,                   // rm -rf /（/ 后须跟空白或结尾）
  String.raw`\brm\s+(-[a-z]*r[a-z]*\s+)/\*(\s|$)`,                 // rm -rf /*（同上收紧）
  String.raw`\bmkfs\b`,                                            // 格式化
  String.raw`\bdd\s+.*\bof=/dev/`,                                 // 写裸设备
  String.raw`:\(\)\s*\{\s*:\|\:\s*&\s*\}\s*;?\s*:`,                 // fork 炸弹
  String.raw`\b(shutdown|reboot|halt|poweroff)\b`,                 // 关机/重启
  String.raw`>\s*/dev/sd[a-z]`,                                    // 重定向写磁盘
  String.raw`\bmv\s+.*\s+/\s*$`,                                   // mv * /
  String.raw`\bchmod\s+(-[a-z]*R[a-z]*\s+)?777\s+/(\s|$)`,          // chmod 777 /（同上收紧）
  String.raw`\bchown\s+-[a-z]*R[a-z]*\s+.*\s+/$`,                   // chown -R … /
]);

// ---------------------------------------------------------------------------
// 敏感 shell 命令模式：合法但有风险，EXEC 级别 + 更具体的审批理由
// 复刻 Python 版 APPROVAL_PATTERNS
// ---------------------------------------------------------------------------

export const SENSITIVE_PATTERNS = Object.freeze([
  [String.raw`\brm\s+-[a-z]*r`, "递归删除文件"],
  [String.raw`\bmv\s+`, "移动/重命名文件"],
  [String.raw`\b(chmod|chown)\s+-[a-z]*R`, "递归修改权限/属主"],
  [String.raw`\bsystemctl\s+`, "管理系统服务"],
  [String.raw`\bservice\s+\w+\s+(start|stop|restart)`, "管理系统服务"],
  [String.raw`\bpip3?\s+install\b`, "安装 Python 包"],
  [String.raw`\bapt(-get)?\s+install\b`, "安装系统包"],
  [String.raw`\b(curl|wget)\b.*\|\s*(sh|bash)\b`, "下载并直接执行脚本"],
]);

// ---------------------------------------------------------------------------
// 主入口
// ---------------------------------------------------------------------------

function compilePatterns(sources) {
  const out = [];
  for (const src of sources || []) {
    if (typeof src !== "string" || !src) continue;
    try { out.push({ src, re: new RegExp(src) }); }
    catch { /* 非法正则直接丢弃，不让配置错误拖垮分类 */ }
  }
  return out;
}

function firstMatch(compiled, text) {
  for (const p of compiled) {
    try { if (p.re.test(text)) return p; } catch { /* ignore */ }
  }
  return null;
}

function truncate(s, n = 120) {
  const t = String(s);
  return t.length > n ? t.slice(0, n) + "…" : t;
}

function shortPattern(src, n = 48) {
  return src.length > n ? src.slice(0, n) + "…" : src;
}

/** 从执行类工具的参数里抽命令文本；非执行类工具返回 null。 */
function extractCommand(toolName, args, policy) {
  const fields = policy.execArgNames && policy.execArgNames[toolName];
  if (!fields) return null;
  for (const f of fields) {
    const v = args[f];
    if (typeof v === "string" && v.trim()) return v;
    if (Array.isArray(v)) {
      const s = v.filter((x) => typeof x === "string").join(" ");
      if (s.trim()) return s;
    }
  }
  return null;
}

function defaultReason(toolName, level) {
  switch (level) {
    case LEVELS.READ: return "";
    case LEVELS.WRITE: return `写操作，需用户批准（${toolName}）`;
    case LEVELS.EXEC: return `执行命令，需用户批准（${toolName}）`;
    case LEVELS.BLOCKED: return `工具 ${toolName} 已被禁用`;
    default: return `未知级别，需用户批准（${toolName}）`;
  }
}

function makeResult(tool, level, reason) {
  return {
    tool,
    level,
    disposition: levelDisposition(level),
    reason: reason || "",
  };
}

/**
 * 分类主函数（纯函数）。
 * @param {string} toolName 工具名
 * @param {object} args 工具参数（用于高危命令模式检测）
 * @param {object} policy buildPolicy() 产物；缺省用 DEFAULT_POLICY
 * @returns {{tool, level, disposition, reason}}
 */
export function classify(toolName, args, policy) {
  const p = policy || DEFAULT_POLICY;
  const name = typeof toolName === "string" ? toolName : String(toolName ?? "");
  const a = args && typeof args === "object" ? args : {};

  // 1) 执行类工具：先扫高危命令模式。红线永远优先于配置表。
  const cmd = extractCommand(name, a, p);
  if (cmd !== null) {
    const hit = firstMatch(compilePatterns(p.blockedPatterns), cmd);
    if (hit) {
      return makeResult(
        name,
        LEVELS.BLOCKED,
        `高危命令被硬拦截，不弹审批卡：${truncate(cmd)}（命中 ${shortPattern(hit.src)}）`
      );
    }
  }

  // 2) 查表：配置覆盖 > 默认表 > 默认级别（WRITE，fail-closed）
  const level = (p.mergedLevels && p.mergedLevels[name]) || p.defaultLevel || LEVELS.WRITE;

  // 3) 敏感命令给出更具体的审批理由
  let reason = "";
  if (cmd !== null && level !== LEVELS.BLOCKED) {
    const sens = p.sensitivePatterns || [];
    for (const entry of sens) {
      const src = Array.isArray(entry) ? entry[0] : entry;
      const why = Array.isArray(entry) ? entry[1] : "敏感操作";
      let re;
      try { re = new RegExp(src); } catch { continue; }
      if (re.test(cmd)) {
        reason = `敏感操作需确认：${why}（命令：${truncate(cmd)}）`;
        break;
      }
    }
  }
  if (!reason) reason = defaultReason(name, level);
  return makeResult(name, level, reason);
}

// 执行类工具 → 承载命令文本的参数名（高危模式扫描用）
export const EXEC_ARG_NAMES = Object.freeze({
  bash: ["command"],
  pwsh: ["command"],
  exec: ["command"],
  shell_exec: ["command"], // Python 版遗留别名
  terminal_send: ["text"],
  run_code: ["code"],
});

// classify 独立使用时的内置默认策略（policy.js 在其上做配置合并）
export const DEFAULT_POLICY = Object.freeze({
  levels: Object.freeze({}),
  mergedLevels: DEFAULT_LEVELS,
  blockedPatterns: DANGEROUS_PATTERNS,
  sensitivePatterns: SENSITIVE_PATTERNS,
  execArgNames: EXEC_ARG_NAMES,
  defaultLevel: LEVELS.WRITE,
  monitor: false,
});
