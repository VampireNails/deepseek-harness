import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync, utimesSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, basename } from "node:path";
import {
  BrowserSession,
  browserDir,
  resolveTarget,
  sanitizeFilename,
  fmtSize,
  normalizeTimeoutMs,
  screenshotName,
  buildOpenResult,
  SNAPSHOT_JS,
  CLICK_JS,
  FILL_JS,
  SELECT_JS,
  NOT_OPEN,
} from "../src/browser.js";

function freshDir() {
  return mkdtempSync(join(tmpdir(), "browser-test-"));
}
function cleanup(dir) {
  rmSync(dir, { recursive: true, force: true });
}

// ---------- 假 playwright：记录调用，不起真浏览器 ----------

function makeFake({ onEvaluate, onGoto, onClick } = {}) {
  const calls = {
    launches: 0,
    launchOpts: null,
    pagesClosed: 0,
    ctxClosed: 0,
    pageClosed: 0,
    goto: [],
    evaluate: [],
    click: [],
    fill: [],
    selectOption: [],
    waitForSelector: [],
    getByText: [],
    screenshot: [],
    downloads: [],
  };
  const downloadHandlers = [];
  const page = {
    url() {
      return "https://example.com/";
    },
    _closed: false,
    isClosed() {
      return this._closed;
    },
    async close() {
      this._closed = true;
      calls.pageClosed++;
    },
    on(evt, cb) {
      if (evt === "download") downloadHandlers.push(cb);
    },
    async goto(url, opts) {
      calls.goto.push([url, opts]);
      if (onGoto) await onGoto(url, opts);
    },
    async waitForTimeout() {},
    async title() {
      return "示例标题";
    },
    async innerText() {
      return "hello   world\n多行文本";
    },
    async evaluate(js, arg) {
      calls.evaluate.push([js, arg]);
      if (onEvaluate) return onEvaluate(js, arg, calls);
      return undefined;
    },
    async click(sel, opts) {
      calls.click.push([sel, opts]);
      if (onClick) await onClick(sel, downloadHandlers);
    },
    async fill(sel, text, opts) {
      calls.fill.push([sel, text, opts]);
    },
    async selectOption(sel, opt, opts) {
      calls.selectOption.push([sel, opt, opts]);
      if (opt && opt.value === "__bad__") throw new Error("no such value");
    },
    async waitForSelector(sel, opts) {
      calls.waitForSelector.push([sel, opts]);
      const fail = sel === "__timeout__" ? new Error("Timeout 15000ms exceeded") : null;
      if (sel === "__invalid__") throw new Error("foo is not a valid selector");
      if (fail) throw fail;
    },
    getByText(t, opts) {
      calls.getByText.push([t, opts]);
      return {
        first: {
          async waitFor(o) {
            if (t === "__never__") throw new Error("Timeout");
          },
        },
      };
    },
    async screenshot(opts) {
      calls.screenshot.push(opts);
      writeFileSync(opts.path, "fakepng");
    },
  };
  const ctx = {
    pages() {
      return [{ async close() { calls.pagesClosed++; } }];
    },
    async newPage() {
      return page;
    },
    async close() {
      calls.ctxClosed++;
    },
  };
  const chromium = {
    async launchPersistentContext(profileDir, opts) {
      calls.launches++;
      calls.launchOpts = { profileDir, opts };
      return ctx;
    },
  };
  const factory = async () => ({ chromium });
  return { calls, page, ctx, chromium, factory, downloadHandlers };
}

function sessionWith(dir, fake) {
  return new BrowserSession(dir, { playwrightFactory: fake.factory });
}

// ---------- 纯逻辑 ----------

test("resolveTarget：编号 / [编号] / CSS 选择器", () => {
  assert.deepEqual(resolveTarget("3"), { kind: "ref", value: "3" });
  assert.deepEqual(resolveTarget("[3]"), { kind: "ref", value: "3" });
  assert.deepEqual(resolveTarget("  12 "), { kind: "ref", value: "12" });
  assert.deepEqual(resolveTarget(".btn"), { kind: "css", value: ".btn" });
  assert.deepEqual(resolveTarget("#login input"), { kind: "css", value: "#login input" });
  assert.deepEqual(resolveTarget(""), { kind: "css", value: "" });
  assert.deepEqual(resolveTarget(null), { kind: "css", value: "" });
});

test("sanitizeFilename：清洗非法字符", () => {
  assert.equal(sanitizeFilename('a/b:c*d?e"f<g>h|i.pdf'), "a_b_c_d_e_f_g_h_i.pdf");
  assert.equal(sanitizeFilename("  report.pdf  "), "report.pdf");
  assert.equal(sanitizeFilename(""), "download");
  assert.equal(sanitizeFilename("   "), "download");
  assert.equal(sanitizeFilename(null), "download");
});

test("fmtSize：字节格式化", () => {
  assert.equal(fmtSize(0), "0B");
  assert.equal(fmtSize(512), "512B");
  assert.equal(fmtSize(1024), "1.0KB");
  assert.equal(fmtSize(1536), "1.5KB");
  assert.equal(fmtSize(1048576), "1.0MB");
});

test("normalizeTimeoutMs：复刻 Python max(1, int(timeout or 15))", () => {
  assert.equal(normalizeTimeoutMs(15), 15000);
  assert.equal(normalizeTimeoutMs(0), 15000); // falsy → 15
  assert.equal(normalizeTimeoutMs(undefined), 15000);
  assert.equal(normalizeTimeoutMs(-5), 1000); // max(1, -5)
  assert.equal(normalizeTimeoutMs(3), 3000);
});

test("buildOpenResult：格式与截断", () => {
  const r = buildOpenResult({
    title: "T",
    url: "https://x/",
    text: "a   b\nc",
  });
  assert.match(r, /^\[标题\] T\n\[地址\] https:\/\/x\/\n\[正文\]\na b c\n\n\(用 browser_snapshot 查看可交互元素编号\)$/);
  const long = buildOpenResult({ title: "", url: "", text: "x".repeat(5000) });
  assert.equal(long.split("[正文]\n")[1].split("\n\n")[0].length, 3000);
});

test("screenshotName：时间戳格式", () => {
  assert.match(screenshotName(), /^shot_\d{8}_\d{6}\.png$/);
  assert.match(screenshotName(new Date(2026, 0, 2, 3, 4, 5)), /^shot_20260102_030405\.png$/);
});

test("browserDir：DSH_HOME 覆盖", () => {
  const old = process.env.DSH_HOME;
  const dir = freshDir();
  try {
    process.env.DSH_HOME = dir;
    assert.equal(browserDir(), join(dir, "intel-browser"));
  } finally {
    if (old === undefined) delete process.env.DSH_HOME;
    else process.env.DSH_HOME = old;
    cleanup(dir);
  }
});

// ---------- downloads ----------

test("downloads：空目录提示", () => {
  const dir = freshDir();
  try {
    const s = new BrowserSession(dir);
    assert.equal(s.downloads(), "(downloads/ 为空)");
  } finally {
    cleanup(dir);
  }
});

test("downloads：按 mtime 新在前、上限 50", () => {
  const dir = freshDir();
  try {
    const s = new BrowserSession(dir);
    const dd = join(dir, "downloads");
    const now = Date.now();
    for (let i = 0; i < 55; i++) {
      const p = join(dd, `f${i}.txt`);
      writeFileSync(p, "x".repeat(i + 1));
      const t = new Date(now - (54 - i) * 1000);
      // f54 最新
      utimesSync(p, t, t);
    }
    const out = s.downloads().split("\n");
    assert.equal(out.length, 50);
    assert.match(out[0], /^- downloads\/f54\.txt/);
    assert.match(out[49], /^- downloads\/f5\.txt/);
    assert.match(out[0], /55B/);
  } finally {
    cleanup(dir);
  }
});

// ---------- 懒加载 ----------

test("懒加载：未 open 时其他工具不启动浏览器", async () => {
  const dir = freshDir();
  const fake = makeFake();
  try {
    const s = sessionWith(dir, fake);
    assert.equal(await s.snapshot(), NOT_OPEN);
    assert.equal(await s.click("3"), NOT_OPEN);
    assert.equal(await s.fill("3", "x"), NOT_OPEN);
    assert.equal(await s.select("3", "v"), NOT_OPEN);
    assert.equal(await s.wait("文字"), NOT_OPEN);
    assert.equal(await s.screenshot(), NOT_OPEN);
    assert.equal(await s.close(), "浏览器未打开");
    assert.equal(fake.calls.launches, 0);
    // downloads 是纯文件操作，不需要浏览器
    assert.equal(s.downloads(), "(downloads/ 为空)");
  } finally {
    cleanup(dir);
  }
});

// ---------- open ----------

test("open：首次 launch、参数正确、只 launch 一次", async () => {
  const dir = freshDir();
  const fake = makeFake();
  try {
    const s = sessionWith(dir, fake);
    const r = await s.open("https://example.com/");
    assert.equal(fake.calls.launches, 1);
    assert.match(fake.calls.launchOpts.profileDir, /browser_profile$/);
    assert.equal(fake.calls.launchOpts.opts.headless, true);
    assert.ok(fake.calls.launchOpts.opts.args.includes("--no-sandbox"));
    assert.equal(fake.calls.launchOpts.opts.acceptDownloads, true);
    assert.equal(fake.calls.pagesClosed, 1); // 关掉 context 自带空白页
    assert.deepEqual(fake.calls.goto[0][0], "https://example.com/");
    assert.equal(fake.calls.goto[0][1].timeout, 30000);
    assert.match(r, /^\[标题\] 示例标题\n\[地址\] https:\/\/example\.com\/\n\[正文\]\nhello world 多行文本/);
    assert.ok(r.includes("用 browser_snapshot 查看可交互元素编号"));
    // SNAPSHOT_JS 预打 ref
    assert.ok(fake.calls.evaluate.some(([js]) => js === SNAPSHOT_JS));
    const r2 = await s.open("https://example.com/2");
    assert.equal(fake.calls.launches, 1); // 不重复 launch
    assert.ok(r2.includes("[标题]"));
  } finally {
    cleanup(dir);
  }
});

test("open：launch 失败返回错误文本不抛异常", async () => {
  const dir = freshDir();
  try {
    const s = new BrowserSession(dir, {
      playwrightFactory: async () => {
        throw new Error("boom");
      },
    });
    const r = await s.open("https://example.com/");
    assert.match(r, /^浏览器启动失败: boom/);
  } finally {
    cleanup(dir);
  }
});

test("snapshot：地址前缀与空元素提示", async () => {
  const dir = freshDir();
  const fake = makeFake({
    onEvaluate: (js) => (js === SNAPSHOT_JS ? "" : undefined),
  });
  try {
    const s = sessionWith(dir, fake);
    await s.open("https://example.com/");
    const r = await s.snapshot();
    assert.equal(r, "[当前地址] https://example.com/\n(无可交互元素)");
  } finally {
    cleanup(dir);
  }
});

// ---------- click / fill / select ----------

test("click：ref 走 CLICK_JS，成功后返回新快照", async () => {
  const dir = freshDir();
  const fake = makeFake({
    onEvaluate: (js) => {
      if (js === CLICK_JS) return "clicked";
      if (js === SNAPSHOT_JS) return '[0] button "确定"';
      return undefined;
    },
  });
  try {
    const s = sessionWith(dir, fake);
    await s.open("https://example.com/");
    const r = await s.click("3");
    assert.deepEqual(fake.calls.evaluate.filter(([js]) => js === CLICK_JS).map(([, a]) => a), ["3"]);
    assert.ok(r.startsWith("已点击。\n[当前地址]"));
    assert.ok(r.includes('[0] button "确定"'));
  } finally {
    cleanup(dir);
  }
});

test("click：ref 不存在返回失败提示", async () => {
  const dir = freshDir();
  const fake = makeFake({ onEvaluate: (js) => (js === CLICK_JS ? "no such ref" : "") });
  try {
    const s = sessionWith(dir, fake);
    await s.open("https://example.com/");
    const r = await s.click("[99]");
    assert.equal(r, "点击失败：no such ref，先用 browser_snapshot 确认编号");
  } finally {
    cleanup(dir);
  }
});

test("click：css 选择器走 page.click", async () => {
  const dir = freshDir();
  const fake = makeFake({ onEvaluate: () => "" });
  try {
    const s = sessionWith(dir, fake);
    await s.open("https://example.com/");
    await s.click(".submit-btn");
    assert.deepEqual(fake.calls.click, [[".submit-btn", { timeout: 8000 }]]);
  } finally {
    cleanup(dir);
  }
});

test("fill：ref 与 css 两条路径", async () => {
  const dir = freshDir();
  const fake = makeFake({ onEvaluate: (js) => (js === FILL_JS ? "filled" : "") });
  try {
    const s = sessionWith(dir, fake);
    await s.open("https://example.com/");
    const r1 = await s.fill("2", "hello");
    assert.deepEqual(
      fake.calls.evaluate.filter(([js]) => js === FILL_JS).map(([, a]) => a),
      [{ ref: "2", text: "hello" }]
    );
    assert.equal(r1, "已填写 [2]");
    const r2 = await s.fill("#kw", "world");
    assert.deepEqual(fake.calls.fill, [["#kw", "world", { timeout: 8000 }]]);
    assert.equal(r2, "已填写 [#kw]");
  } finally {
    cleanup(dir);
  }
});

test("fill：ref 失败返回提示", async () => {
  const dir = freshDir();
  const fake = makeFake({ onEvaluate: (js) => (js === FILL_JS ? "no such ref" : "") });
  try {
    const s = sessionWith(dir, fake);
    await s.open("https://example.com/");
    assert.equal(await s.fill("9", "x"), "填写失败：no such ref，先用 browser_snapshot 确认编号");
  } finally {
    cleanup(dir);
  }
});

test("select：ref 走 SELECT_JS", async () => {
  const dir = freshDir();
  const fake = makeFake({
    onEvaluate: (js) => {
      if (js === SELECT_JS) return "selected";
      if (js === SNAPSHOT_JS) return '[5] select "选项"';
      return "";
    },
  });
  try {
    const s = sessionWith(dir, fake);
    await s.open("https://example.com/");
    const r = await s.select("5", "opt1");
    assert.deepEqual(
      fake.calls.evaluate.filter(([js]) => js === SELECT_JS).map(([, a]) => a),
      [{ ref: "5", value: "opt1" }]
    );
    assert.ok(r.startsWith("已选择 [5] = opt1\n[当前地址]"));
  } finally {
    cleanup(dir);
  }
});

test("select：css 先按 value、失败按 label 重试", async () => {
  const dir = freshDir();
  const fake = makeFake({ onEvaluate: () => "" });
  try {
    const s = sessionWith(dir, fake);
    await s.open("https://example.com/");
    // value 对得上：只调一次
    await s.select("#sel", "good");
    assert.deepEqual(fake.calls.selectOption, [["#sel", { value: "good" }, { timeout: 8000 }]]);
    // value 对不上：按 label 重试
    fake.calls.selectOption.length = 0;
    await s.select("#sel", "__bad__");
    assert.deepEqual(fake.calls.selectOption, [
      ["#sel", { value: "__bad__" }, { timeout: 8000 }],
      ["#sel", { label: "__bad__" }, { timeout: 8000 }],
    ]);
  } finally {
    cleanup(dir);
  }
});

// ---------- wait ----------

test("wait：文字已在页面 → 等文字出现", async () => {
  const dir = freshDir();
  const fake = makeFake({ onEvaluate: () => true });
  try {
    const s = sessionWith(dir, fake);
    await s.open("https://example.com/");
    const r = await s.wait("多行文本", 5);
    assert.equal(r, "已等到文字出现：多行文本");
    assert.deepEqual(fake.calls.getByText[0][0], "多行文本");
    assert.equal(fake.calls.waitForSelector.length, 0);
  } finally {
    cleanup(dir);
  }
});

test("wait：文字等待超时", async () => {
  const dir = freshDir();
  const fake = makeFake({ onEvaluate: () => true });
  try {
    const s = sessionWith(dir, fake);
    await s.open("https://example.com/");
    const r = await s.wait("__never__", 2);
    assert.equal(r, "等待超时（2s），未出现：__never__");
  } finally {
    cleanup(dir);
  }
});

test("wait：文字不在页面 → 按 selector 等元素", async () => {
  const dir = freshDir();
  const fake = makeFake({ onEvaluate: () => false });
  try {
    const s = sessionWith(dir, fake);
    await s.open("https://example.com/");
    const r = await s.wait("#app", 5);
    assert.equal(r, "已等到元素出现：#app");
    assert.deepEqual(fake.calls.waitForSelector[0], ["#app", { timeout: 5000, state: "attached" }]);
  } finally {
    cleanup(dir);
  }
});

test("wait：selector 超时", async () => {
  const dir = freshDir();
  const fake = makeFake({ onEvaluate: () => false });
  try {
    const s = sessionWith(dir, fake);
    await s.open("https://example.com/");
    const r = await s.wait("__timeout__", 7);
    assert.equal(r, "等待超时（7s），未出现：__timeout__");
  } finally {
    cleanup(dir);
  }
});

test("wait：非法 selector 回退按文字等", async () => {
  const dir = freshDir();
  const fake = makeFake({ onEvaluate: () => false });
  try {
    const s = sessionWith(dir, fake);
    await s.open("https://example.com/");
    const r = await s.wait("__invalid__", 5);
    assert.equal(r, "已等到文字出现：__invalid__");
    assert.ok(fake.calls.getByText.length > 0);
  } finally {
    cleanup(dir);
  }
});

// ---------- screenshot / close ----------

test("screenshot：落盘到 screenshots/ 并返回相对路径", async () => {
  const dir = freshDir();
  const fake = makeFake();
  try {
    const s = sessionWith(dir, fake);
    await s.open("https://example.com/");
    const r = await s.screenshot();
    assert.match(r, /^已截图: screenshots\/shot_\d{8}_\d{6}\.png$/);
    const p = fake.calls.screenshot[0].path;
    assert.ok(p.startsWith(join(dir, "screenshots")));
    assert.ok(p.endsWith(".png"));
  } finally {
    cleanup(dir);
  }
});

test("close：真正释放 page/context，profile 保留", async () => {
  const dir = freshDir();
  const fake = makeFake();
  try {
    const s = sessionWith(dir, fake);
    await s.open("https://example.com/");
    assert.ok(s.isOpen);
    const r = await s.close();
    assert.match(r, /浏览器页面已关闭，浏览器进程已释放/);
    assert.equal(fake.calls.pageClosed, 1);
    assert.equal(fake.calls.ctxClosed, 1);
    assert.ok(!s.isOpen);
    assert.equal(await s.snapshot(), NOT_OPEN);
    // profile 目录保留在磁盘
    assert.ok(existsSync(join(dir, "browser_profile")));
    // 再次 close 幂等
    assert.equal(await s.close(), "浏览器未打开");
    assert.equal(fake.calls.launches, 1);
  } finally {
    cleanup(dir);
  }
});

// ---------- 下载落盘 ----------

test("click 触发下载：落盘到 downloads/，重名加 _2", async () => {
  const dir = freshDir();
  const fake = makeFake({
    onEvaluate: (js) => (js === CLICK_JS ? "clicked" : ""),
    onClick: (_sel, handlers) => {
      const mkDl = (name) => ({
        suggestedFilename: () => name,
        saveAs: (p) => writeFileSync(p, "data"),
      });
      // 模拟点击过程中触发两次下载事件（同名）
      handlers[0](mkDl("a/b.pdf"));
      handlers[0](mkDl("a/b.pdf"));
    },
  });
  try {
    const s = sessionWith(dir, fake);
    await s.open("https://example.com/");
    const r = await s.click(".dl"); // CSS 路径：fake page.click 会触发 onClick → 下载事件
    assert.ok(r.includes("下载已保存到 downloads/a_b.pdf、downloads/a_b_2.pdf"), r);
    const list = s.downloads();
    assert.ok(list.includes("downloads/a_b.pdf"));
    assert.ok(list.includes("downloads/a_b_2.pdf"));
  } finally {
    cleanup(dir);
  }
});

// ---------- index.js 插件装配 ----------

test("index.js：注册 9 个工具，effect 可回卷", async () => {
  const dir = freshDir();
  const old = process.env.DSH_HOME;
  process.env.DSH_HOME = dir;
  try {
    const mod = await import("../index.js");
    assert.equal(mod.name, "dsh-intelligence-browser");
    assert.deepEqual(mod.inject, ["agents", "tools"]);
    assert.ok(mod.Config);

    const registered = [];
    const provided = {};
    const effects = [];
    const ctx = {
      provide: (k, v) => (provided[k] = v),
      tools: {
        register: (tool) => {
          registered.push(tool.name);
          return Promise.resolve(() => registered.splice(registered.indexOf(tool.name), 1));
        },
      },
      effect: (genFn, label) => {
        effects.push(label);
        for (const _ of genFn()) {
          /* 消费 generator，执行 register */
        }
      },
    };
    await mod.apply(ctx, {});
    assert.ok(provided.browser instanceof (await import("../src/browser.js")).BrowserSession);
    assert.deepEqual(registered, [
      "browser_open",
      "browser_snapshot",
      "browser_click",
      "browser_fill",
      "browser_select",
      "browser_wait",
      "browser_downloads",
      "browser_screenshot",
      "browser_close",
    ]);
    assert.deepEqual(effects, ["intelBrowser.tools"]);
  } finally {
    if (old === undefined) delete process.env.DSH_HOME;
    else process.env.DSH_HOME = old;
    cleanup(dir);
  }
});
