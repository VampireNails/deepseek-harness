# dsh-intelligence-artifacts

[English](README.md) | 中文

artifact_save 保存具有标题的 Markdown、HTML 或文本产物。再次使用同一标题，会在同一 ID 下追加版本。artifact_get 读取指定或最新版本，artifact_versions 列出版本，artifact_read 读取最新内容。非法类型回退为 Markdown。

## 存储与展示

产物位于 $DSH_HOME/intel-artifacts，默认目录为 ~/.dsh/intel-artifacts。每个产物包含 meta.json 和版本内容文件。读取时将早期单文件格式作为版本 1；随后保存会将原内容保留为第一版。

插件提供工具与元数据，不提供 HTTP 渲染路由。其 /artifacts/{id} URL 是展示约定，不能证明有服务器响应这个地址。HMC 通过经过认证的桥接获取产物，并负责展示和导出。

## 安装

在插件目录运行 `npm ci`，再运行 `dsh plugin --profile <name> add .`。依赖固定到 DSH 基线，不要替换为注册表默认版本。
