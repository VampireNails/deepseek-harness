// dsh-intelligence-artifacts —— Artifacts v2（复刻 Python 版 agent/artifacts.py）。
//
// 本插件只做工具层 + 元数据：artifact_save / artifact_get / artifact_versions / artifact_read，
// HTML 产物在元数据中标注 kind=html，供上层（HMC）做沙盒 iframe 渲染。
//
// 边界（重要）：dsh 插件没有加 HTTP 路由的能力。FastAPI 原版的
// /artifacts/{id}（展示页）、/api/artifacts/{id}/raw（HTML 原始内容）、/download、/versions
// 路由在这里不复刻，渲染层留待 HMC PR 实现。本插件返回的 url（/artifacts/{id}）
// 是为该渲染层预留的约定地址，当前没有对应的 HTTP 服务。

import z from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { ArtifactStore, artifactsDir } from "./src/artifacts.js";
import { publicationIdentity } from '../shared/publication.js';

export const name = "dsh-intelligence-artifacts";
export const inject = ["agents", "tools"];

export const Config = z.object({}).default({});

const textBlock = (text) => [{ type: "text", text }];

const metaLine = (m) => `产物：${m.title}（id=${m.id}，kind=${m.kind}，v${m.version}）\n展示链接：${m.url}`;

export function apply(ctx, config) {
  const store = new ArtifactStore(artifactsDir());
  ctx.provide("artifacts", store);

  ctx.effect(
    function* () {
      yield ctx.tools.register(
        defineTool({
          name: "artifact_save",
          description:
            "保存产物（markdown/html/text）。同标题再次保存则复用 id、追加为新版本。kind=html 的产物会在元数据中标注，供上层（HMC）做沙盒 iframe 渲染。",
          parameters: {
            title: { type: "string", required: true, description: "产物标题（同标题再次保存=追加新版本）" },
            content: { type: "string", required: true, description: "产物内容" },
            kind: { type: "string", description: "markdown | html | text，默认 markdown" },
            references: { type: "array", items: { type: "number" }, description: "正文引用的现存记忆 ID" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => {
            const identity = publicationIdentity('artifact', args.references, ctx.get?.('memoryReferences'));
            const m = store.save(args.title, args.content, args.kind || "markdown", identity);
            const htmlNote =
              m.kind === "html" ? "\n（HTML 产物：渲染层由 HMC 端沙盒 iframe 实现）" : "";
            return { text: `产物已发布：${m.title}（v${m.version}）\n展示链接：${m.url}${htmlNote}` };
          },
        })
      );
      yield ctx.tools.register(
        defineTool({
          name: "artifact_get",
          description: "取产物：返回元数据 + 指定版本内容（不指定版本则取最新版）。",
          parameters: {
            id: { type: "string", required: true, description: "产物 id" },
            version: { type: "number", description: "版本号，不指定取最新" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => {
            const m = store.get(args.id, args.version ?? null);
            if (!m) return { text: "未找到该产物" };
            return { text: `${metaLine(m)}\n\n${m.content}` };
          },
        })
      );
      yield ctx.tools.register(
        defineTool({
          name: "artifact_versions",
          description: "列出产物的全部版本（版本号/时间/文件名）。",
          parameters: {
            id: { type: "string", required: true, description: "产物 id" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => ({ text: store.versionsText(args.id) }),
        })
      );
      yield ctx.tools.register(
        defineTool({
          name: "artifact_read",
          description: "读取产物最新版内容（= artifact_get 不指定版本时的便捷入口）。",
          parameters: {
            id: { type: "string", required: true, description: "产物 id" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => {
            const m = store.read(args.id);
            if (!m) return { text: "未找到该产物" };
            return { text: `产物「${m.title}」（v${m.version}，kind=${m.kind}）\n\n${m.content}` };
          },
        })
      );
    },
    "intelArtifacts.tools"
  );
}
