# dsh-intelligence-artifacts

[English](README.md) | 中文

artifact_save 保存具有标题的 Markdown、HTML 或文本产物。再次使用同一标题，会在同一 ID 下追加版本。artifact_get 读取指定或最新版本，artifact_versions 列出版本，artifact_read 读取最新内容。非法类型回退为 Markdown。

## 存储与展示

产物位于 $DSH_HOME/intel-artifacts，默认目录为 ~/.dsh/intel-artifacts。每个产物包含 meta.json 和版本内容文件。读取时将早期单文件格式作为版本 1；随后保存会将原内容保留为第一版。

发布工具在保存前根据现存记忆 ID 核验可选的 `references`。Linux [任务运行器](../../intel/task-runner/README.zh.md) 注入任务、运行与日期身份，各版本保留各自的身份与引用。重复发布返回已有版本；标题、内容、类型或身份冲突时拒绝写入。存储跨进程串行写入，原子替换元数据。手动保存仍按标题追加版本，不虚构运行身份。

插件提供工具与元数据，不提供 HTTP 渲染路由。save/get/read 结果增加 `reference={version:1,endpoint:"muse/artifacts/get",args:{id,version}}`，供已配对 HMC 桥接的消费者使用，版本固定。消费者先检查支持的引用版本与精确 endpoint，再通过认证 `/v1/rpc` 提交有界参数。引用不授予访问权限，也不含凭据。现有仅 ID 的读取仍取最新版。存储的 `url=/artifacts/{id}` 保留为旧标识；工具正文给出 ID 和版本，引导 HMC 用户使用产物阅读器与版本选择，不再宣称悬空地址是展示链接。HMC 负责离线 HTML 预览；此处不表示 Android 自动处理 JSON 引用。

## 安装

在插件目录运行 `npm ci`，再运行 `dsh plugin --profile <name> add link:/absolute/path/to/dsh-fork/intel-plugins/dsh-intelligence-artifacts`。profile 链接仓库源码，保留旁边共享发布模块的可访问性；只复制插件目录不够。依赖固定到 DSH 基线，不要替换为注册表默认版本。
