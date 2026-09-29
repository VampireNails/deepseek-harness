# dsh-intelligence-artifacts — Artifacts v2（工具层）

复刻退役 Python Harness 的 `agent/artifacts.py`：产物的存 / 取 / 版本管理。

- `artifact_save(title, content, kind?)` —— 保存产物；同标题再次保存复用 id、追加为新版本；返回 `{id, title, kind, version, url, path}`。kind ∈ markdown | html | text（非法值回退 markdown）。
- `artifact_get(id, version?)` —— 取指定版本（默认最新），返回元数据 + 内容 + 版本号。
- `artifact_versions(id)` —— 版本列表文本。
- `artifact_read(id)` —— 最新版内容的便捷读取入口（= artifact_get 不指定版本）。

数据目录：`$DSH_HOME/intel-artifacts`（默认 `~/.dsh/intel-artifacts`），与生产 / 测试隔离。
每个产物一个子目录：`meta.json` + `v1.md` / `v2.html` ……
旧格式（meta 无 versions + 单文件 `artifact.ext`）自动兼容：读时 v1 回退，写时旧文件计为 v1。

## 明确的边界：本次不做渲染层

Python 原版的 HTML 产物走 FastAPI 路由渲染：

- `/artifacts/{id}` 展示页（HTML 类产物用 `sandbox="allow-scripts"` iframe 隔离渲染）
- `/api/artifacts/{id}/raw`（iframe 的 src，HTML 原始内容）
- `/download`、`/versions`

**dsh 插件没有加 HTTP 路由的能力**，上述路由本次不复刻。本插件只做：

1. 工具层（存 / 取 / 版本）
2. 元数据标注：`kind=html` 的产物在 meta 中明确标注，供上层做沙盒 iframe 渲染

渲染层（展示页 + raw 路由 + iframe 沙盒）留待 **HMC PR** 实现。
本插件返回的 `url`（`/artifacts/{id}`）是为该渲染层预留的约定地址——
当前没有任何 HTTP 服务响应这个地址，不要把它当可访问链接发给用户。

## 安装

```sh
cd /root/intel/dsh-fork/intel-plugins/dsh-intelligence-artifacts
npm ci   # @deepseek-ai/* 已钉死 0.1.7-rc.1，不要用默认版本
dsh plugin --profile <name> add .
```
