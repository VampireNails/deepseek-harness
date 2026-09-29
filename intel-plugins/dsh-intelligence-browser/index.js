import z from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { BrowserSession, browserDir } from "./src/browser.js";

export const name = "dsh-intelligence-browser";
export const inject = ["agents", "tools"];

export const Config = z.object({}).default({});

const textBlock = (text) => [{ type: "text", text }];

export function apply(ctx, config) {
  const browser = new BrowserSession(browserDir());
  ctx.provide("browser", browser);

  ctx.effect(
    function* () {
      yield ctx.tools.register(
        defineTool({
          name: "browser_open",
          description:
            "用无头 Chromium 打开页面（会话保持，登录态持久化），返回标题与正文摘要。之后用 snapshot/click/fill 继续操作。",
          parameters: {
            url: { type: "string", required: true, description: "要打开的页面 URL" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => ({ text: await browser.open(args.url) }),
        })
      );
      yield ctx.tools.register(
        defineTool({
          name: "browser_snapshot",
          description: "列出当前页面的可交互元素（编号/类型/文本），编号供 click/fill/select 使用。",
          parameters: {},
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async () => ({ text: await browser.snapshot() }),
        })
      );
      yield ctx.tools.register(
        defineTool({
          name: "browser_click",
          description: "点击元素：snapshot 编号（如 3）或 CSS 选择器；点击后返回新快照；若触发下载会注明保存路径。",
          parameters: {
            target: { type: "string", required: true, description: "snapshot 编号或 CSS 选择器" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => ({ text: await browser.click(args.target) }),
        })
      );
      yield ctx.tools.register(
        defineTool({
          name: "browser_fill",
          description: "向输入框填文本：snapshot 编号或 CSS 选择器。若填写后触发下载会注明保存路径。",
          parameters: {
            target: { type: "string", required: true, description: "snapshot 编号或 CSS 选择器" },
            text: { type: "string", required: true, description: "要填写的文本" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => ({ text: await browser.fill(args.target, args.text) }),
        })
      );
      yield ctx.tools.register(
        defineTool({
          name: "browser_select",
          description: "下拉框选择：snapshot 编号或 CSS 选择器；按 option 的 value 或显示文本选择。",
          parameters: {
            target: { type: "string", required: true, description: "snapshot 编号或 CSS 选择器" },
            value: { type: "string", required: true, description: "option 的 value 或显示文本" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => ({ text: await browser.select(args.target, args.value) }),
        })
      );
      yield ctx.tools.register(
        defineTool({
          name: "browser_wait",
          description: "等待文字或元素出现：传文字或 CSS 选择器，返回是否等到。",
          parameters: {
            target: { type: "string", required: true, description: "要等的文字或 CSS 选择器" },
            timeout: { type: "number", description: "超时秒数，默认 15" },
          },
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async (args) => ({ text: await browser.wait(args.target, args.timeout ?? 15) }),
        })
      );
      yield ctx.tools.register(
        defineTool({
          name: "browser_downloads",
          description: "列出浏览器已下载的文件（intel-browser/downloads/）。",
          parameters: {},
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async () => ({ text: browser.downloads() }),
        })
      );
      yield ctx.tools.register(
        defineTool({
          name: "browser_screenshot",
          description: "截取当前页面，保存到 intel-browser/screenshots/，返回相对路径。",
          parameters: {},
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async () => ({ text: await browser.screenshot() }),
        })
      );
      yield ctx.tools.register(
        defineTool({
          name: "browser_close",
          description:
            "关闭浏览器并真正释放内存/进程（登录态 profile 保留在磁盘，下次 browser_open 自动恢复）。用完浏览器后务必调用。",
          parameters: {},
          output: { schema: { type: "json" }, render: (args, value) => textBlock(value.text) },
          execute: async () => ({ text: await browser.close() }),
        })
      );
    },
    "intelBrowser.tools"
  );
}
