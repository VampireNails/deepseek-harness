// 从 session 完整快照里拼出本 turn 的对话 transcript，供复盘子智能体使用。
export async function buildTranscript(ctx, sessionId, turn, minUserChars) {
  let snapshot;
  try {
    snapshot = await ctx.sessionQuery.readSession(sessionId);
  } catch (e) {
    console.error("[dsh-intelligence-quiet] readSession failed:", e?.message);
    return null;
  }
  const events = snapshot?.events;
  if (!Array.isArray(events) || events.length === 0) {
    console.error("[dsh-intelligence-quiet] no events in snapshot");
    return null;
  }

  const lines = [];
  let userChars = 0;
  for (const ev of events) {
    const type = ev?.type || "";
    if (type !== "user/message" && type !== "assistant/message") continue;
    const data = ev?.data || {};
    // 只取本 turn（事件若带 turn 字段则过滤）
    if (data.turn !== undefined && turn !== undefined && data.turn !== turn) continue;
    const text = extractText(data.content).trim();
    if (!text) continue;
    if (type === "user/message") {
      const kind = data?.source?.kind;
      if (kind && kind !== "user") continue; // 跳过插件注入的 system-reminder
      userChars += text.length;
      lines.push(`用户：${text}`);
    } else {
      lines.push(`助手：${text.slice(0, 2000)}`);
    }
  }
  if (userChars < minUserChars) return null;
  // 只保留最近一轮：从最后一个"用户："开始
  const idx = [];
  lines.forEach((l, i) => { if (l.startsWith("用户：")) idx.push(i); });
  const lastUser = idx.length ? idx[idx.length - 1] : undefined;
  const sliced = lastUser !== undefined ? lines.slice(lastUser) : lines;
  return sliced.slice(-12).join("\n");
}

function extractText(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((b) => {
      if (typeof b === "string") return b;
      if (b && typeof b.text === "string") return b.text;
      return "";
    })
    .join("");
}
