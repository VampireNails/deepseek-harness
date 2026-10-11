# 长期记忆持久化

[English](README.md) | 中文

记忆插件在配置的 `dataDir/memory.db` 中保存原记忆行及派生的 FTS5 索引。检索器在同一个具名 savepoint 内插入两者。索引写入失败会回滚本条记忆写入；调用方的外层事务保留自身修改，并决定最终提交。默认读取、搜索和自动召回在限制结果数前排除已失效记录。孤儿索引仍可能影响语料排名。

`memory_write` 从执行器取得来源会话，不采用模型参数。非 event 写入仅复用同一已知会话、同一类型且仍有效的完全相同去首尾空白原文。复用返回 `deduplicated:true`、原 ID 和不变的创建时间；正文明确未新增版本或更新时间。这是幂等复写，不是新发生事件的证据。`event` 写入、未知来源、不同会话、类型或正文，以及失效后的重写保留独立原文。既有重复行、纠错版本和审计保持。SQLite 在查找重复前取得写锁；争用安全失败，不让两个插入竞态发生。加载不运行 schema 迁移或历史清理。

`warnMemoryRows` 和 `warnMemoryBytes` 是非负安全整数，默认 0（关闭）；Loader 的空值输入解析为该默认值。启用任一项后，成功写入和搜索包含 `capacity`，达到或超过阈值时正文给出软提示。统计包含所有保留原文，包括失效记录：`rows` 为原文行数，`textBytes` 为 UTF-8 原文字节数，不是数据库磁盘大小。复用不增加任一计数。写入在 savepoint 内观测容量，观测失败回滚新写入。关闭观测不扫描原文聚合；启用时每次写入或搜索扫描原文，不拒绝继续写入。

自动召回搜索已接受首步输入中的最后一条真人用户消息，排除其他插件注入的 user。`minQueryChars` 默认 2 个非空白字符：单汉字不自动召回，但仍能用 `memory_search` 显式搜索，或显式配置阈值 1 后自动召回。单个 ASCII 字母不作为索引查询词。早先主题仍在模型历史中，但不会自动拼入新的召回查询。首步召回追加进持久 Session 日志，无须重复注入仍可供后续请求使用；后来的压缩可能改变保留历史。

搜索保留闲聊问题后面的主题。`maxQueryChars`（默认 65536 个 UTF-16 码元）限制原始输入，`maxQueryTerms`（默认 2048 个不同汉字及 ASCII 词）同时限制输入和同义词扩展后的查询。超过任一上限时，在进入 SQLite 前以 `MEMORY_QUERY_TOO_LARGE` 失败。各词是引号包围的字面 OR 操作数。存在其他主题时忽略常见英文闲聊词；`for` 等字面查询仍可搜索。`synonymGroups` 默认提供偏好、职责、回复和审批词汇；维护者可配置至多 16 组、每组 2–16 个字面词，每词含 1–32 个汉字、ASCII 字母、数字或空格。

检索器按相邻汉字双字、ASCII 整词、单汉字查询及匹配的同义概念评分。IDF 统计不同的匹配证据模式，重复日志不会增加同一模式的频率。`minimumScoreRatio`（默认 0.1）移除低于最佳分数该比例的候选；`duplicatePatternPenalty`（默认 0.1）使同一证据模式的每次后续选择减去最佳分数的该比例。两者可配置为 0 到 1 的有限比例。平分时沿用 BM25 顺序。有限的中文个人偏好问句在候选上限前筛选字面个人陈述；preferenceSubjects 默认为我和用户，可配置 1–16 个、每个含 1–32 个汉字、ASCII 字母、数字或空格的字面别名以覆盖既有署名陈述；偏好设置字段名和普通事实请求仍走通用搜索。这种词法启发式不能推断未陈述偏好，也不保证语义召回。

`maxCandidates`（默认 128）限制一次 SQL 候选窗口的总量，`maxCandidateBytes`（默认 1048576）在读取任何候选原文前限制其 UTF-8 总字节数。两者均为可配置的正安全整数。字节超限以 `MEMORY_RECALL_TOO_LARGE` 失败；应具体化查询，或审慎提高上限。自动召回跳过任一种大小失败，按日志导出设置发出不含查询或记忆原文的警告；显式搜索返回原生工具错误。搜索不截断已存原文，也不静默丢弃过大的候选。有界 BM25 窗口仍可能遗漏大型异质语料中的相关记录。

原生 `memory_write` 结果在成功写入后持久化 `presentationMeta.memoryWrite` 为 `{ok:true,id}`。失败走执行器错误协议，不提供成功元数据。ID 是实际存储行的 ID。upkeep 将此元数据与匹配的工具调用、轮次和步骤身份一起用于确认运行完成，不根据渲染文本推测写入成功。此可选结果元数据使用现有 Session 事件格式。

有效状态 schema 版本 1 新增独立的 `memory_state` 和仅追加的 `memory_audit` 表；纠错不覆盖或删除原记忆。`memory_history` 显式读取完整原文、当前版本和最近 50 次审计，包括失效记录。`memory_invalidate` 和 `memory_restore` 要求填写原因和已读版本，并通过原生工具审批服务展示完整原文请求确认。缺少 agent 或审批通道、拒绝、取消及版本过期均阻止修改。状态、FTS 和审计共用 savepoint；恢复时索引原文。新 Feed/Artifact 引用通过已挂载服务或只读数据库备用路径检查记忆仍有效；历史产物保持可读。

已挂载的 `memoryManagement` 服务提供有界列表、显式详情和配对手机纠错。手机沿用既有配对认证，提交前确认完整原文和原因；模型调用则必须通过原生审批。审计来源和会话身份由受信执行上下文填写。修改将 UUID 与参数及调用者绑定：同请求重试返回已记录结果，不追加审计，即使随后已恢复该记忆；不同参数复用 UUID 会失败。版本冲突要求重新读取。响应丢失或关闭窗口不能撤销已提交纠错。列表提供 240 字符预览；详情保留完整原文，手机网关拒绝超过 524288 字符显示上限的原文。

仅供维护者使用的[维护模块](src/maintenance.js) 在 Linux 上使用已部署的 Node 运行时执行。`node src/maintenance.js inspect /absolute/memory.db` 报告原记录和索引行数，以及孤儿、缺失、不一致、失效记忆及失效索引数，不输出记忆内容。`node src/maintenance.js repair /absolute/memory.db /absolute/unused-backup.db` 要求写入方处于静止窗口，在修改派生索引前创建私有 SQLite 快照。它保留原字段、有效状态及审计，以正常中文切分规则仅重建有效记忆的 FTS 行。回执含前后计数及指定备份路径、快照和原记录哈希，不回显路径。失败会回滚索引修改。

新数据库初始化有效状态 schema；已有数据库不会隐式准备，仍可读取且不声明纠错能力。停止写入后，`node src/maintenance.js prepare-validity /absolute/memory.db /absolute/unused-backup.db` 先创建私有备份，再事务创建 schema，核对原记录保持，并在失败时保留备份。准备后重新挂载插件。未知或不完整的状态 schema 拒绝继续。不能让不理解有效状态的源码版本使用已准备的库，它们可能重新召回失效原文；回退到此类版本不属于支持的回滚。

备份实际配置的数据库，不假定默认目录。将备份放入私有目录，核对回执后再恢复写入。回滚时，历史完整数据库快照不能覆盖后续记忆写入；应根据当前原记忆重建索引。维护不能证明召回质量、记忆正确性或长期保留策略。[SQLite savepoint 语义](https://www.sqlite.org/lang_savepoint.html)与 [Node SQLite 备份 API](https://nodejs.org/docs/latest-v24.x/api/sqlite.html#sqlitebackupsource-db-path-options)定义事务及快照行为。

memory_write 通过执行器错误协议以 MEMORY_INVALID_TEXT 拒绝空白文本，以 MEMORY_WRITE_FAILED 报告其他写入失败。失败正文不包含原始异常或输入回显。写入失败的结果可能尚未确认：决定重试前先查询。成功的 memoryWrite.id 展示元信息与 {ok:true,id,text,kind} 仍然可用。

memory_read 和 memory_search 的来源异常分别使用 MEMORY_READ_FAILED 和 MEMORY_SEARCH_FAILED；既有 MEMORY_QUERY_TOO_LARGE、MEMORY_RECALL_TOO_LARGE 上限错误也走安全错误协议。不存在的 ID 和空搜索仍为成功结果。
