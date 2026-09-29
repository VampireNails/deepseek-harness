// dsh-intelligence-browser: 多步无头浏览器（复刻 Python 版 deepseek-harness agent/tools.py 的 browser_* 语义）。
//
// 内存风险提示（服务器仅 ~1GB 内存，请仔细阅读）：
// - Chromium 无头进程常驻约 150~300MB 内存。browser_open 之后若长期不操作，
//   务必调用 browser_close 真正关闭浏览器进程释放内存。
// - browser_close 会关闭 page → context → 浏览器进程，并停止 playwright，
//   只保留磁盘上的持久化 profile（登录态/cookie 不丢失）。
// - 浏览器是按需懒加载的：只有第一次 browser_open 才会 launch；其他工具在
//   浏览器未打开时直接返回提示，不会隐式启动（省内存）。
// - 数据目录与生产/测试隔离：~/.dsh/intel-browser（可用 DSH_HOME 覆盖）。
import {
  existsSync,
  mkdirSync,
  readdirSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

export function browserDir() {
  return join(
    process.env.DSH_HOME || join(homedir(), ".dsh"),
    "intel-browser"
  );
}

export const NOT_OPEN = "浏览器尚未打开（先调用 browser_open 打开页面）";

// ---------- 纯逻辑 helper（可单元测试，不碰浏览器） ----------

/** target 可为快照编号（如 3 或 [3]）或 CSS 选择器；返回 {kind, value}。 */
export function resolveTarget(target) {
  const t = String(target ?? "").trim();
  const m = t.match(/^\[?(\d+)\]?$/);
  if (m) return { kind: "ref", value: m[1] };
  return { kind: "css", value: t };
}

/** 下载文件名清洗：去掉非法字符，空名回退为 "download"。 */
export function sanitizeFilename(name) {
  const cleaned = String(name ?? "")
    .replace(/[\\/:*?"<>|]/g, "_")
    .trim();
  return cleaned || "download";
}

export function fmtSize(n) {
  const units = ["B", "KB", "MB", "GB"];
  let v = n;
  for (const u of units) {
    if (v < 1024 || u === "GB") {
      return u === "B" ? `${v}B` : `${v.toFixed(1)}${u}`;
    }
    v /= 1024;
  }
  return `${v.toFixed(1)}GB`;
}

/** 复刻 Python: max(1, int(timeout or 15))，返回毫秒数。 */
export function normalizeTimeoutMs(timeout) {
  const secs = Math.max(1, parseInt(timeout || 15, 10) || 15);
  return secs * 1000;
}

/** screenshot 文件名时间戳：shot_YYYYMMDD_HHMMSS.png。 */
export function screenshotName(d = new Date()) {
  const p = (n) => String(n).padStart(2, "0");
  return (
    `shot_${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_` +
    `${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.png`
  );
}

/** 正文摘要格式化（截断 3000 字符），与 Python 版输出格式一致。 */
export function buildOpenResult({ title, url, text }) {
  const clean = String(text ?? "").replace(/\s+/g, " ").trim().slice(0, 3000);
  return (
    `[标题] ${title}\n[地址] ${url}\n[正文]\n${clean}\n\n` +
    `(用 browser_snapshot 查看可交互元素编号)`
  );
}

// ---------- 注入页面的 JS（与 Python 版 _SNAPSHOT_JS/_CLICK_JS/_FILL_JS/_SELECT_JS 同语义） ----------

export const SNAPSHOT_JS = `() => {
  const els = document.querySelectorAll('a, button, input, select, textarea, [role="button"], [onclick]');
  const out = [];
  let i = 0;
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    const tag = el.tagName.toLowerCase();
    const text = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
    const type = el.getAttribute('type');
    out.push('[' + i + '] ' + tag + (type ? '(' + type + ')' : '') + ' "' + text + '"');
    el.setAttribute('data-harness-ref', String(i));
    i++;
    if (i >= 80) break;
  }
  return out.join('\\n');
}`;

export const CLICK_JS = `(ref) => {
  const el = document.querySelector('[data-harness-ref="' + ref + '"]');
  if (!el) return 'no such ref';
  el.scrollIntoView({block: 'center'});
  el.click();
  return 'clicked';
}`;

export const FILL_JS = `(args) => {
  const el = document.querySelector('[data-harness-ref="' + args.ref + '"]');
  if (!el) return 'no such ref';
  el.scrollIntoView({block: 'center'});
  el.focus();
  el.value = args.text;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return 'filled';
}`;

export const SELECT_JS = `(args) => {
  const el = document.querySelector('[data-harness-ref="' + args.ref + '"]');
  if (!el) return 'no such ref';
  if (el.tagName.toLowerCase() !== 'select') return 'not a select';
  const want = String(args.value);
  let hit = false;
  for (const o of el.options) {
    if (o.value === want || (o.text || '').trim() === want) { o.selected = true; hit = true; break; }
  }
  if (!hit) return 'no such option: ' + want;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return 'selected';
}`;

// ---------- BrowserSession ----------

export class BrowserSession {
  /**
   * @param {string} dir 数据目录（默认 browserDir()）
   * @param {object} deps 测试注入点：{ playwrightFactory }，默认 () => import("playwright")
   */
  constructor(dir = browserDir(), deps = {}) {
    this.dir = dir;
    this.profileDir = join(dir, "browser_profile");
    this.downloadsDir = join(dir, "downloads");
    this.screenshotsDir = join(dir, "screenshots");
    this._playwrightFactory = deps.playwrightFactory || (() => import("playwright"));
    this._pw = null; // playwright 实例
    this._ctx = null; // persistent context
    this._page = null; // 单页
    this._launched = false;
    this._pendingDownloads = [];
    this._ensureDirs();
  }

  _ensureDirs() {
    for (const d of [this.dir, this.profileDir, this.downloadsDir, this.screenshotsDir]) {
      mkdirSync(d, { recursive: true });
    }
  }

  get isOpen() {
    return this._launched && this._page !== null && !this._page.isClosed();
  }

  /** 懒加载：仅 browser_open 调用。其他工具浏览器未开时直接返回 NOT_OPEN。 */
  async _launch() {
    if (this._launched) return;
    const { chromium } = await this._playwrightFactory();
    this._pw = chromium;
    this._ctx = await chromium.launchPersistentContext(this.profileDir, {
      headless: true,
      args: ["--no-sandbox", "--disable-dev-shm-usage"],
      acceptDownloads: true,
    });
    for (const p of this._ctx.pages()) {
      try {
        await p.close();
      } catch {
        /* ignore */
      }
    }
    this._page = await this._ctx.newPage();
    this._page.on("download", (dl) => this._pendingDownloads.push(dl));
    this._launched = true;
  }

  _pageOrNull() {
    return this.isOpen ? this._page : null;
  }

  async open(url) {
    try {
      await this._launch();
    } catch (e) {
      return `浏览器启动失败: ${e.message || e}`;
    }
    const page = this._page;
    try {
      await page.goto(url, { timeout: 30000 });
      await page.waitForTimeout(1200);
      try {
        await page.evaluate(SNAPSHOT_JS); // 预打 ref 标记，fill/click 可直接用编号
      } catch {
        /* ignore */
      }
      const title = await page.title();
      const text = await page.innerText("body").catch(() => "");
      return buildOpenResult({ title, url: page.url(), text });
    } catch (e) {
      return `浏览器打开失败: ${e.message || e}`;
    }
  }

  async snapshot() {
    const page = this._pageOrNull();
    if (!page) return NOT_OPEN;
    try {
      const out = await page.evaluate(SNAPSHOT_JS);
      return "[当前地址] " + page.url() + "\n" + (out || "(无可交互元素)");
    } catch (e) {
      return `快照失败: ${e.message || e}`;
    }
  }

  async click(target) {
    const page = this._pageOrNull();
    if (!page) return NOT_OPEN;
    try {
      const { kind, value } = resolveTarget(target);
      const nBefore = this._pendingDownloads.length;
      if (kind === "ref") {
        const r = await page.evaluate(CLICK_JS, value);
        if (r !== "clicked") {
          return `点击失败：${r}，先用 browser_snapshot 确认编号`;
        }
      } else {
        await page.click(value, { timeout: 8000 });
      }
      await page.waitForTimeout(1500);
      const note = this._drainDownloads(nBefore);
      return "已点击。" + note + "\n" + (await this.snapshot()).slice(0, 2000);
    } catch (e) {
      return `点击失败: ${e.message || e}`;
    }
  }

  async fill(target, text) {
    const page = this._pageOrNull();
    if (!page) return NOT_OPEN;
    try {
      const { kind, value } = resolveTarget(target);
      const nBefore = this._pendingDownloads.length;
      if (kind === "ref") {
        const r = await page.evaluate(FILL_JS, { ref: value, text });
        if (r !== "filled") {
          return `填写失败：${r}，先用 browser_snapshot 确认编号`;
        }
      } else {
        await page.fill(value, text, { timeout: 8000 });
      }
      await page.waitForTimeout(800);
      const note = this._drainDownloads(nBefore);
      return `已填写 [${target}]` + note;
    } catch (e) {
      return `填写失败: ${e.message || e}`;
    }
  }

  async select(target, value) {
    const page = this._pageOrNull();
    if (!page) return NOT_OPEN;
    try {
      const { kind, value: targetValue } = resolveTarget(target);
      if (kind === "ref") {
        const r = await page.evaluate(SELECT_JS, { ref: targetValue, value });
        if (r !== "selected") {
          return `选择失败：${r}，先用 browser_snapshot 确认编号`;
        }
      } else {
        try {
          await page.selectOption(targetValue, { value }, { timeout: 8000 });
        } catch {
          // value 对不上时按显示文本试（复刻 Python 版）
          await page.selectOption(targetValue, { label: value }, { timeout: 8000 });
        }
      }
      await page.waitForTimeout(800);
      return `已选择 [${target}] = ${value}\n` + (await this.snapshot()).slice(0, 2000);
    } catch (e) {
      return `选择失败: ${e.message || e}`;
    }
  }

  /**
   * 等待：页面文字已含 target 则等文字出现（复刻 Python 版的 innerText 先判逻辑，
   * 修复过"wait 误判 selector/文字"的 bug：先查 innerText 再判 selector），
   * 否则按 CSS 选择器等元素出现；非法 selector 回退按文字等。
   */
  async wait(target, timeout = 15) {
    const page = this._pageOrNull();
    if (!page) return NOT_OPEN;
    const t = String(target ?? "").trim();
    const secs = Math.max(1, parseInt(timeout || 15, 10) || 15);
    const ms = secs * 1000;
    const waitText = async () => {
      try {
        await page.getByText(t, { exact: false }).first.waitFor({ timeout: ms, state: "visible" });
        return `已等到文字出现：${t}`;
      } catch {
        return `等待超时（${secs}s），未出现：${t}`;
      }
    };
    try {
      const onPage = await page.evaluate(
        "(t) => (document.body ? document.body.innerText : '').includes(t)",
        t
      );
      if (onPage) return await waitText();
      try {
        await page.waitForSelector(t, { timeout: ms, state: "attached" });
        return `已等到元素出现：${t}`;
      } catch (e) {
        if (String(e.message || e).includes("Timeout")) {
          return `等待超时（${secs}s），未出现：${t}`;
        }
        return await waitText(); // 非法 selector，改按文字等
      }
    } catch (e) {
      return `等待失败: ${e.message || e}`;
    }
  }

  /** click/fill 后新触发的下载落盘到 downloads/；返回说明文本（无下载则空串）。 */
  _drainDownloads(nBefore) {
    const newDls = this._pendingDownloads.slice(nBefore);
    if (!newDls.length) return "";
    const saved = [];
    for (const dl of newDls) {
      try {
        let name = sanitizeFilename(
          dl.suggestedFilename() || `download_${Date.now()}`
        );
        let target = join(this.downloadsDir, name);
        if (existsSync(target)) {
          const dot = name.lastIndexOf(".");
          const stem = dot > 0 ? name.slice(0, dot) : name;
          const suffix = dot > 0 ? name.slice(dot) : "";
          let i = 2;
          while (existsSync(join(this.downloadsDir, `${stem}_${i}${suffix}`))) i++;
          name = `${stem}_${i}${suffix}`;
          target = join(this.downloadsDir, name);
        }
        // saveAs 可能是同步（mock）或异步（真 playwright）
        const r = dl.saveAs(target);
        if (r && typeof r.then === "function") {
          r.catch(() => {});
        }
        saved.push("downloads/" + name);
      } catch (e) {
        saved.push(`(保存失败: ${e.message || e})`);
      }
    }
    this._pendingDownloads.splice(nBefore);
    return "\n下载已保存到 " + saved.join("、");
  }

  downloads() {
    if (!existsSync(this.downloadsDir)) return "(downloads/ 为空)";
    const files = readdirSync(this.downloadsDir)
      .map((f) => join(this.downloadsDir, f))
      .filter((p) => {
        try {
          return statSync(p).isFile();
        } catch {
          return false;
        }
      })
      .sort((a, b) => statSync(b).mtimeMs - statSync(a).mtimeMs);
    if (!files.length) return "(downloads/ 为空)";
    return files
      .slice(0, 50)
      .map((p) => `- downloads/${p.split("/").pop()}（${fmtSize(statSync(p).size)}）`)
      .join("\n");
  }

  async screenshot() {
    const page = this._pageOrNull();
    if (!page) return NOT_OPEN;
    try {
      const name = screenshotName();
      await page.screenshot({ path: join(this.screenshotsDir, name) });
      return `已截图: screenshots/${name}`;
    } catch (e) {
      return `截图失败: ${e.message || e}`;
    }
  }

  /**
   * 真正释放浏览器进程与内存（区别于 Python 版只关页面）：
   * page.close → context.close（persistent context 关闭即结束浏览器进程）
   * → _pw 关闭 → 全部置空。登录态 profile 保留在磁盘，下次 open 自动恢复。
   */
  async close() {
    if (!this._launched) return "浏览器未打开";
    const errs = [];
    try {
      if (this._page && !this._page.isClosed()) await this._page.close();
    } catch (e) {
      errs.push(e.message || e);
    }
    try {
      if (this._ctx) await this._ctx.close();
    } catch (e) {
      errs.push(e.message || e);
    }
    this._page = null;
    this._ctx = null;
    this._pw = null;
    this._launched = false;
    this._pendingDownloads = [];
    if (errs.length) return `浏览器已关闭（部分资源释放异常: ${errs.join("; ")}）`;
    return "浏览器页面已关闭，浏览器进程已释放（profile 保留在磁盘）";
  }
}
