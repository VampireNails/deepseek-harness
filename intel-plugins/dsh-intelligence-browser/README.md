# dsh-intelligence-browser

多步无头浏览器插件（树外插件）：复刻 Python 版 deepseek-harness `agent/tools.py` 的
`browser_*` 9 个工具语义。

## 工具

| 工具 | 说明 |
|---|---|
| `browser_open` | 用无头 Chromium 打开页面（会话保持、登录态持久化），返回标题与正文摘要 |
| `browser_snapshot` | 列出当前页可交互元素（编号/类型/文本），编号供 click/fill/select 使用 |
| `browser_click` | 点击元素：snapshot 编号（如 3）或 CSS 选择器；点击后返回新快照；触发下载会注明保存路径 |
| `browser_fill` | 向输入框填文本：snapshot 编号或 CSS 选择器 |
| `browser_select` | 下拉框选择：snapshot 编号或 CSS 选择器；按 option 的 value 或显示文本选择 |
| `browser_wait` | 等待文字或元素出现：传文字或 CSS 选择器，返回是否等到（默认 15s） |
| `browser_downloads` | 列出已下载文件（`downloads/`） |
| `browser_screenshot` | 截取当前页面，保存到 `screenshots/` |
| `browser_close` | **关闭浏览器并真正释放内存/进程**（profile 保留在磁盘，下次打开自动恢复） |

## 内存风险（必读）

服务器仅约 1GB 内存，Chromium 无头进程常驻约 150~300MB：

- **用完务必调用 `browser_close`**：它会关闭 page → persistent context（context 关闭即结束
  浏览器进程）→ 停止 playwright，真正释放内存。只保留磁盘上的持久化 profile
 （登录态/cookie 不丢失，下次 `browser_open` 自动恢复）。
- **浏览器按需懒加载**：只有第一次 `browser_open` 才会 launch；其他工具在浏览器未打开时
  直接返回提示，不会隐式启动（省内存）。
- 不要在无人值守的长循环里反复 `open` 而不 `close`；`browser_wait` 的 timeout 不宜设过大
  （长时间等待期间浏览器进程一直占内存）。

## 数据目录（与生产/测试隔离）

`$DSH_HOME/intel-browser/`（默认 `~/.dsh/intel-browser/`）：

- `browser_profile/` — 持久化 Chromium profile（登录态跨重启保留）
- `downloads/` — 下载文件落盘
- `screenshots/` — 截图落盘

## 安装

树外插件，包内自带 `node_modules`（不靠 pnpm link 装传递依赖）：

```sh
npm install   # 已固定 playwright@1.63.0（与服务器 ~/.cache/ms-playwright 的 chromium-1243 对应）
              # @deepseek-ai/dsh-tools 固定 0.1.7-rc.1（npm 默认的 0.0.1-rc.1 会导致 unknown tool）
```

Chromium 二进制已随 `~/.cache/ms-playwright/chromium-1243` 缓存在服务器上，无需再下载。
若缓存缺失：`npx playwright install chromium`（注意磁盘/内存）。

## 测试

```sh
npm test   # node --test tests/，29 个用例全部基于 mock playwright，不起真浏览器
```

真实浏览器冒烟（可选，手动）：

```sh
DSH_HOME=/tmp/dsh-browser-smoke node -e "
import('./src/browser.js').then(async ({BrowserSession}) => {
  const s = new BrowserSession();
  console.log((await s.open('https://example.com/')).slice(0, 200));
  console.log(await s.snapshot().then(x => x.slice(0, 200)));
  console.log(await s.close());
});"
```

## 卸载回卷

插件只通过 `ctx.effect` 注册工具并 `ctx.provide("browser", …)`，不修改 dsh 核心
`packages/`。卸载即移除插件目录（cordis effect 自动回卷工具注册），磁盘数据保留在
`~/.dsh/intel-browser/` 可手动删除。
