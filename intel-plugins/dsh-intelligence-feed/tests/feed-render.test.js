import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { renderFeedHtml, mdToHtml, escapeHtml } from "../src/render.js";

const post = (over = {}) => ({
  id: "f1",
  ts: "2026-09-29 09:51",
  title: "测试标题",
  body: "测试正文",
  source: "测试源",
  ...over,
});

test("空 feed 显示友好占位", () => {
  const html = renderFeedHtml([]);
  assert.ok(
    html.includes("还没有动态。晨间简报或定时任务会把值得看的内容推到这里。"),
    "占位文案应与 Python 版一致"
  );
  assert.ok(html.includes('class="empty"'));
  assert.ok(!html.includes("<article"), "空 feed 不应有卡片");
  assert.ok(html.includes("信息流（0）"));
});

test("自包含：无外部资源引用", () => {
  const html = renderFeedHtml([post()]);
  assert.ok(html.includes("<style>"), "CSS 必须内联");
  assert.ok(!html.includes("<link"), "无外链样式表");
  assert.ok(!html.includes("<script"), "无外部脚本");
  assert.ok(!html.includes("@import"), "无 CSS import");
  assert.ok(!html.includes("url("), "无 url() 资源引用");
});

test("卡片含时间戳 / 来源标签 / 标题", () => {
  const html = renderFeedHtml([
    post({ ts: "2026-09-29 09:51", title: "晨间要闻", source: "晨间简报" }),
  ]);
  assert.ok(html.includes("2026-09-29 09:51"), "时间戳");
  assert.ok(html.includes('<span class="src">晨间简报</span>'), "来源标签");
  assert.ok(html.includes("<h2>晨间要闻</h2>"), "标题");
  assert.ok(html.includes("<article"), "卡片元素");
  assert.ok(html.includes("信息流（1）"), "计数");
});

test("无来源时不渲染来源标签", () => {
  const html = renderFeedHtml([post({ source: "" })]);
  assert.ok(!html.includes('class="src"'));
});

test("markdown 表格渲染", () => {
  const body = "| 名称 | 值 |\n| --- | --- |\n| CPU | 42% |\n| 内存 | 1GB |";
  const html = renderFeedHtml([post({ body })]);
  assert.ok(html.includes("<table>"));
  assert.ok(html.includes("<thead>") && html.includes("<tbody>"));
  assert.ok(html.includes("<th>名称</th>"));
  assert.ok(html.includes("<th>值</th>"));
  assert.ok(html.includes("<td>CPU</td>"));
  assert.ok(html.includes("<td>42%</td>"));
});

test("围栏代码块渲染且内容转义", () => {
  const body = "示例：\n```js\nconst el = document.querySelector('<div>');\n```\n结束";
  const html = renderFeedHtml([post({ body })]);
  assert.ok(html.includes("<pre><code>"), "代码块容器");
  assert.ok(html.includes("&lt;div&gt;"), "代码内 HTML 转义");
  assert.ok(!html.includes("'<div>'"), "原始尖括号不得透出");
  assert.ok(html.includes("<p>示例：</p>") && html.includes("<p>结束</p>"));
});

test("行内元素：加粗 / 斜体 / 链接 / 行内代码 / 裸 URL", () => {
  const body =
    "**粗体** 和 *斜体*，见[文档](https://example.com/x?a=1&b=2)，运行 `npm test`，官网 https://example.org/。";
  const html = renderFeedHtml([post({ body })]);
  assert.ok(html.includes("<strong>粗体</strong>"));
  assert.ok(html.includes("<em>斜体</em>"));
  assert.ok(html.includes('<a href="https://example.com/x?a=1&amp;b=2">文档</a>'));
  assert.ok(html.includes("<code>npm test</code>"));
  assert.ok(html.includes('<a href="https://example.org/">https://example.org/</a>'));
  assert.ok(!html.includes("https://example.org/。"), "尾部中文句号不应吞进链接");
});

test("标题 / 列表 / 引用 / 分割线渲染", () => {
  const body = "# 大标题\n\n- 一\n- 二\n\n1. 甲\n2. 乙\n\n> 引用文字\n\n---\n尾段";
  const html = renderFeedHtml([post({ body })]);
  assert.ok(html.includes("<h1>大标题</h1>"));
  assert.ok(html.includes("<ul>") && html.includes("<li>一</li>") && html.includes("<li>二</li>"));
  assert.ok(html.includes("<ol>") && html.includes("<li>甲</li>"));
  assert.ok(html.includes("<blockquote>"));
  assert.ok(html.includes("引用文字"));
  assert.ok(html.includes("<hr>"));
});

test("标题 / 来源 / 时间戳 HTML 转义", () => {
  const html = renderFeedHtml([
    post({
      ts: '<b>ts</b>',
      title: '<script>alert("x")</script>',
      source: 'a"b<c>',
    }),
  ]);
  assert.ok(!html.includes("<script>alert"), "标题中的标签不得透出");
  assert.ok(html.includes("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;"));
  assert.ok(html.includes("&lt;b&gt;ts&lt;/b&gt;"), "时间戳转义");
  assert.ok(html.includes("a&quot;b&lt;c&gt;"), "来源转义");
});

test("正文中的原始 HTML 被转义（不透出标签）", () => {
  const html = renderFeedHtml([post({ body: '<img src=x onerror=alert(1)>' })]);
  assert.ok(!html.includes("<img"), "原始 HTML 标签不得透出");
  assert.ok(html.includes("&lt;img"));
});

test("非 http(s) 链接不生成 a 标签", () => {
  const html = renderFeedHtml([post({ body: "[点我](javascript:alert(1))" })]);
  assert.ok(!html.includes("<a href"), "javascript: 伪协议不得成链接");
  assert.ok(html.includes("javascript:alert(1)"));
});

test("mdToHtml / escapeHtml 单元行为", () => {
  assert.equal(escapeHtml('<a href="x">&\'y'), "&lt;a href=&quot;x&quot;&gt;&amp;&#39;y");
  assert.equal(mdToHtml(""), "");
  assert.equal(mdToHtml("hello"), "<p>hello</p>");
  assert.ok(mdToHtml("```\ncode & <\n```").includes("<pre><code>code &amp; &lt;</code></pre>"));
});

// ---- feed_render 工具接线测试（伪造最小 ctx） ----

test("feed_render 工具：注册 / 空库占位 / n 参数 / output_path 写文件", async () => {
  const home = mkdtempSync(join(tmpdir(), "feed-render-tool-"));
  const prevHome = process.env.DSH_HOME;
  process.env.DSH_HOME = home;
  try {
    const { apply } = await import("../index.js");
    const registered = [];
    const effects = [];
    const ctx = {
      provide() {},
      effect(genFn) {
        effects.push(
          (async () => {
            const it = genFn();
            let v;
            for (;;) {
              const r = it.next(v);
              if (r.done) break;
              v = await r.value;
            }
          })()
        );
      },
      tools: {
        register: (tool) => {
          registered.push(tool);
          return Promise.resolve(() => {});
        },
      },
    };
    apply(ctx);
    await Promise.all(effects);

    const names = registered.map((t) => t.name);
    assert.ok(names.includes("feed_render"), "feed_render 已注册");
    assert.ok(names.includes("feed_post"), "feed_post 保持不动");
    assert.ok(names.includes("feed_read"), "feed_read 保持不动");

    const renderTool = registered.find((t) => t.name === "feed_render");

    // 空库 → 占位
    const r0 = await renderTool.execute({});
    assert.ok(r0.text.includes("还没有动态。晨间简报或定时任务会把值得看的内容推到这里。"));
    assert.ok(r0.text.includes("<!DOCTYPE html>"));

    // 发一条再渲染
    const postTool = registered.find((t) => t.name === "feed_post");
    await postTool.execute({ title: "工具测试", body: "| a |\n| - |\n| 1 |", source: "测试源" });
    await postTool.execute({ title: "第二条", body: "正文二", source: "" });
    const r1 = await renderTool.execute({ n: 1 });
    assert.ok(r1.text.includes("<h2>第二条</h2>"), "n=1 只取最新一条");
    assert.ok(!r1.text.includes("工具测试"));
    const r2 = await renderTool.execute({ n: 2 });
    assert.ok(r2.text.includes("工具测试") && r2.text.includes("<table>"));
    assert.ok(r2.text.includes("信息流（2）"));

    // output_path 写文件
    const out = join(home, "out", "feed.html");
    const r3 = await renderTool.execute({ n: 2, output_path: out });
    assert.equal(readFileSync(out, "utf8"), r3.text, "文件内容与返回值一致");
  } finally {
    if (prevHome === undefined) delete process.env.DSH_HOME;
    else process.env.DSH_HOME = prevHome;
    rmSync(home, { recursive: true, force: true });
  }
});
