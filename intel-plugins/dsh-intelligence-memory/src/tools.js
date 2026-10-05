// tools.js — memory_write / memory_read / memory_search 三个模型工具
import { defineTool } from "@deepseek-ai/dsh-tools";

function jsonOutput(render) {
  return {
    schema: { type: "json" },
    render,
  };
}

const textBlock = (text) => [{ type: "text", text }];

export function memoryWriteTool(retriever) {
  return defineTool({
    name: "memory_write",
    description:
      "记住一条长期记忆。当用户说\"记住…\"或明确要求记住偏好、事实、事件时调用。不要重复记录已存在的相同记忆。",
    parameters: {
      text: {
        type: "string",
        required: true,
        description: "要记住的内容，一句话说清楚",
      },
      kind: {
        type: "string",
        description: "记忆类型：fact（事实）/ preference（偏好）/ event（事件）/ note（备注，默认）",
      },
    },
    output: {
      ...jsonOutput((args, value) =>
        textBlock(value.ok ? `已记住 #${value.id}：${value.text}` : `记录失败：${value.error}`)
      ),
      presentationMeta: (args, value) => ({ memoryWrite: value.ok ? { ok: true, id: value.id } : { ok: false } }),
    },
    execute: async (args) => {
      try {
        const { id } = retriever.add({ text: args.text, kind: args.kind ?? "note" });
        return { ok: true, id, text: args.text.trim(), kind: args.kind ?? "note" };
      } catch (err) {
        return { ok: false, error: String(err?.message ?? err) };
      }
    },
  });
}

export function memoryReadTool(retriever) {
  return defineTool({
    name: "memory_read",
    description: "按 id 读取一条记忆的完整内容。",
    parameters: {
      id: { type: "number", required: true, description: "记忆 id（memory_write / memory_search 返回的 id）" },
    },
    output: jsonOutput((args, value) =>
      textBlock(value.found ? `#${value.id} [${value.kind}] ${value.text}` : `未找到 id=${args.id} 的记忆`)
    ),
    execute: async (args) => {
      const row = retriever.get(args.id);
      if (!row) return { found: false };
      return { found: true, id: row.id, text: row.text, kind: row.kind, createdAt: row.created_at };
    },
  });
}

export function memorySearchTool(retriever) {
  return defineTool({
    name: "memory_search",
    description:
      "搜索长期记忆。当用户的问题可能涉及之前记住的内容时，先调用此工具查一下，而不是直接说不知道。",
    parameters: {
      query: { type: "string", required: true, description: "搜索关键词或问题" },
      limit: { type: "number", description: "最多返回条数，默认 5" },
    },
    output: jsonOutput((args, value) =>
      textBlock(
        value.results.length === 0
          ? "没有找到相关记忆"
          : value.results.map((r) => `#${r.id} [${r.kind}] ${r.text}`).join("\n")
      )
    ),
    execute: async (args) => {
      const results = retriever.search(args.query, args.limit ?? 5);
      return { results };
    },
  });
}
