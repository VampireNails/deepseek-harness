# dsh-intelligence-browser

[English](README.md) | 中文

插件通过无头 Chromium 提供 browser_open、browser_snapshot、browser_click、browser_fill、browser_select、browser_wait、browser_downloads、browser_screenshot 和 browser_close。交互目标使用快照中的元素编号或 CSS 选择器定位。

## 资源管理

Chromium 在 browser_open 时按需启动。其他工具会提示浏览器已关闭，不会启动它。用完后调用 browser_close：它关闭页面和持久化 context，并释放浏览器进程。磁盘 profile 保留登录状态。Chromium 在小型服务器上可能占用 150–300 MB，无人值守任务需要及时释放它。

数据位于 $DSH_HOME/intel-browser，默认目录为 ~/.dsh/intel-browser。browser_profile 保存持久状态，downloads 保存下载文件，screenshots 保存图片。HMC 通过桥接提供下载和截图；私有浏览器 profile 不属于产物。

## 安装与验证

运行 npm ci 安装锁定的 Playwright 和 DSH 工具依赖。缺少 Chromium 时，运行 npx playwright install chromium。npm test 运行模拟浏览器的单测；真实浏览器启动、交互、下载和关闭需要在独立数据目录中另行验证。
