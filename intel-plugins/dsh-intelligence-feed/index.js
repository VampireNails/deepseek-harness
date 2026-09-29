import z from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { writeFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { FeedStore, feedDir } from "./src/feed.js";
import { renderFeedHtml } from "./src/render.js";

export const name = "dsh-intelligence-feed";
export const inject = ["agents", "tools"];

export const Config = z.object({}).default({});

const textBlock = (text) => [{ type: "text", text }];

export function apply(ctx, config) {
  const feed = new FeedStore(feedDir());
  ctx.provide("feed", feed);

  ctx.effect(
    function* () {
      yield ctx.tools.register(
        defineTool({
          name: "feed_post",
          description: "往个人 Feed 发一条简报（标题+正文+来源）。晨间简报等定时内容用它发布。",
          parameters: {
            title: { type: "string", required: true, description: "标题" },
            body: { type: "string", required: true, description: "正文（中文）" },
            source: { type: "string", description: "来源，如'晨间简报'" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => {
            const id = feed.addPost(args.title, args.body, args.source || "");
            return { text: `已发布 ${id}：${args.title}` };
          },
        })
      );
      yield ctx.tools.register(
        defineTool({
          name: "feed_read",
          description: "读个人 Feed 的最新简报（新在前）。",
          parameters: {
            n: { type: "number", description: "条数，默认 5" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => {
            const posts = feed.listPosts(args.n ?? 5);
            if (!posts.length) return { text: "Feed 暂无内容" };
            const text = posts
              .map((p) => `[${p.id}] ${p.ts} ${p.title}\n${p.body}`)
              .join("\n---\n");
            return { text };
          },
        })
      );
    },
    "intelFeed.tools"
  );

  ctx.effect(
    function* () {
      yield ctx.tools.register(
        defineTool({
          name: "feed_render",
          description:
            "把个人 Feed 渲染成深色卡片流 HTML 页面（自包含、内联 CSS、无外部资源；正文 Markdown 渲染，支持表格与代码块）。智能体可存为 artifact 或直接展示；空 Feed 显示友好占位。",
          parameters: {
            n: { type: "number", description: "渲染最近 N 条，默认 20" },
            output_path: { type: "string", description: "可选：同时把 HTML 写到此文件路径" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => {
            const html = renderFeedHtml(feed.listPosts(args.n ?? 20));
            if (args.output_path) {
              mkdirSync(dirname(args.output_path), { recursive: true });
              writeFileSync(args.output_path, html, "utf8");
            }
            return { text: html };
          },
        })
      );
    },
    "intelFeed.render"
  );
}
