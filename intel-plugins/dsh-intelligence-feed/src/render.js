// 轻量 Markdown → HTML（零依赖）：标题、段落、加粗、斜体、行内代码、
// 链接（含裸 URL 自动链接）、围栏代码块、表格、列表、引用、分割线。
// 输入先整体 HTML 转义，再做 Markdown 解析，保证原始 HTML 永不透出。

export function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[c]);
}

// 占位符分隔符：控制字符，转义后仍安全（不含任何 HTML 特殊字符）
const PH = "\x1f";

function renderInline(escaped) {
  const stash = [];
  const put = (html) => {
    stash.push(html);
    return `${PH}${stash.length - 1}${PH}`;
  };
  // 1) 行内代码：先藏起来，避免内部被二次解析
  let t = escaped.replace(/`([^`\n]+)`/g, (_, c) => put(`<code>${c}</code>`));
  // 2) Markdown 链接 [text](url)，只放行 http(s)，其余原样保留
  t = t.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (m, text, url) =>
    /^https?:\/\//i.test(url) ? put(`<a href="${url}">${text}</a>`) : m
  );
  // 3) 裸 URL 自动链接（去掉尾部标点）
  t = t.replace(/\bhttps?:\/\/[^\s<>"']+/gi, (u) => {
    const m = u.match(/^(.*?)[.,;:!?。、，；：？！」』）\]]+$/);
    const url = m ? m[1] : u;
    const tail = m ? u.slice(url.length) : "";
    return put(`<a href="${url}">${url}</a>`) + tail;
  });
  // 4) 加粗 / 斜体
  t = t.replace(/\*\*([^*]+?)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/(^|[\s([{])\*([^*\n]+?)\*(?=[\s)\].,;:!?，。、；：？！]|$)/g, "$1<em>$2</em>");
  // 5) 还原占位符
  t = t.replace(new RegExp(`${PH}(\\d+)${PH}`, "g"), (_, i) => stash[Number(i)]);
  return t;
}

const FENCE_RE = /^```[^\n]*\n([\s\S]*?)^```[ \t]*$/gm;

function splitCells(line) {
  return line
    .trim()
    .replace(/^\||\|$/g, "")
    .split("|")
    .map((c) => c.trim());
}

function isTableSep(line) {
  const cells = splitCells(line);
  return cells.length > 0 && cells.every((c) => /^:?-+:?$/.test(c));
}

export function mdToHtml(src) {
  const text = escapeHtml(src).replace(/\r\n?/g, "\n");
  // 围栏代码块先整体摘出（内容已转义），行内不再解析
  const fences = [];
  const noFence = text.replace(FENCE_RE, (_, code) => {
    fences.push(code.replace(/\n$/, ""));
    return `${PH}F${fences.length - 1}${PH}`;
  });
  const fenceRe = new RegExp(`^${PH}F(\\d+)${PH}$`);

  const lines = noFence.split("\n");
  const out = [];
  const para = [];
  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${renderInline(para.join("\n"))}</p>`);
      para.length = 0;
    }
  };

  let i = 0;
  let m;
  while (i < lines.length) {
    const line = lines[i];
    if ((m = line.match(fenceRe))) {
      flushPara();
      out.push(`<pre><code>${fences[Number(m[1])]}</code></pre>`);
      i++;
    } else if ((m = line.match(/^(#{1,6})\s+(.*?)\s*$/))) {
      flushPara();
      const lv = m[1].length;
      out.push(`<h${lv}>${renderInline(m[2])}</h${lv}>`);
      i++;
    } else if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
      flushPara();
      out.push("<hr>");
      i++;
    } else if (
      /^\s*\|.*\|\s*$/.test(line) &&
      i + 1 < lines.length &&
      lines[i + 1].includes("|") &&
      isTableSep(lines[i + 1])
    ) {
      flushPara();
      const head = splitCells(line);
      const rows = [];
      i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        rows.push(splitCells(lines[i]));
        i++;
      }
      out.push(
        "<table><thead><tr>" +
          head.map((c) => `<th>${renderInline(c)}</th>`).join("") +
          "</tr></thead><tbody>" +
          rows
            .map((r) => "<tr>" + r.map((c) => `<td>${renderInline(c)}</td>`).join("") + "</tr>")
            .join("") +
          "</tbody></table>"
      );
    } else if (/^\s*&gt;\s?/.test(line)) {
      // 注意：正文已整体转义，引用符 '>' 此时为 '&gt;'
      flushPara();
      const qs = [];
      while (i < lines.length && (m = lines[i].match(/^\s*&gt;\s?(.*)$/))) {
        qs.push(m[1]);
        i++;
      }
      out.push(`<blockquote><p>${renderInline(qs.join("\n"))}</p></blockquote>`);
    } else if ((m = line.match(/^\s*[-*+]\s+(.*)$/))) {
      flushPara();
      const items = [];
      while (i < lines.length && (m = lines[i].match(/^\s*[-*+]\s+(.*)$/))) {
        items.push(m[1]);
        i++;
      }
      out.push("<ul>" + items.map((c) => `<li>${renderInline(c)}</li>`).join("") + "</ul>");
    } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
      flushPara();
      const items = [];
      while (i < lines.length && (m = lines[i].match(/^\s*\d+[.)]\s+(.*)$/))) {
        items.push(m[1]);
        i++;
      }
      out.push("<ol>" + items.map((c) => `<li>${renderInline(c)}</li>`).join("") + "</ol>");
    } else if (/^\s*$/.test(line)) {
      flushPara();
      i++;
    } else {
      para.push(line);
      i++;
    }
  }
  flushPara();
  return out.join("\n");
}

// 视觉复刻 Python 版 /feed 页面：深色背景 + 卡片流。
// 自包含：全部 CSS 内联，不引用任何外部资源。
const PAGE = `<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>信息流</title>
<style>
body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;max-width:820px;margin:0 auto;padding:24px;line-height:1.7;background:#141417;color:#e4e4e7}
a{color:#7aa2ff}
h1{font-size:22px;margin:4px 0 20px}
.card{background:#1d1d21;border:1px solid #2b2b31;border-radius:12px;padding:18px 20px;margin-bottom:16px}
.card h2{font-size:17px;margin:6px 0 10px;color:#f4f4f5}
.card .meta{font-size:12px;color:#8b8b93;display:flex;gap:10px;align-items:center}
.card .src{background:#2b2b31;border-radius:6px;padding:1px 8px;font-size:12px;color:#a1a1aa}
.card .body{font-size:14.5px;color:#d4d4d8}
.card .body h1,.card .body h2,.card .body h3,.card .body h4{font-size:15.5px;margin:14px 0 8px;color:#f4f4f5}
.card .body p{margin:8px 0}
.card .body ul,.card .body ol{margin:8px 0;padding-left:22px}
.card .body li{margin:4px 0}
.card .body blockquote{margin:10px 0;padding:2px 12px;border-left:3px solid #3a3a41;color:#a1a1aa}
.card .body hr{border:none;border-top:1px solid #2b2b31;margin:14px 0}
.card .body pre{background:#101013;padding:12px;border-radius:8px;overflow:auto}
.card .body pre code{background:none;padding:0}
.card .body code{background:#2b2b31;padding:2px 6px;border-radius:4px;font-size:13px}
.card .body table{border-collapse:collapse;margin:10px 0}
.card .body td,.card .body th{border:1px solid #3a3a41;padding:6px 10px}
.card .body th{background:#232328}
.empty{color:#8b8b93;padding:24px 0}
</style>
</head>
<body>
<h1>信息流（{count}）</h1>
{cards}
</body>
</html>`;

const EMPTY_HTML =
  '<p class="empty">还没有动态。晨间简报或定时任务会把值得看的内容推到这里。</p>';

function renderCard(p) {
  const src = p.source ? `<span class="src">${escapeHtml(p.source)}</span>` : "";
  const title = p.title ? escapeHtml(p.title) : "（无标题）";
  return (
    `<article class="card"><div class="meta"><span>${escapeHtml(p.ts ?? "")}</span>${src}</div>` +
    `<h2>${title}</h2><div class="body">${mdToHtml(p.body ?? "")}</div></article>`
  );
}

// posts: FeedStore.listPosts() 返回的数组（新在前）
export function renderFeedHtml(posts) {
  const list = Array.isArray(posts) ? posts : [];
  const cards = list.length ? list.map(renderCard).join("\n") : EMPTY_HTML;
  return PAGE.replace("{count}", () => String(list.length)).replace("{cards}", () => cards);
}
