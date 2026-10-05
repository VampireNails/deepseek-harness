# 长期记忆持久化

[English](README.md) | 中文

记忆插件在配置的 `dataDir/memory.db` 中保存原记忆行及派生的 FTS5 索引。检索器在同一个具名 savepoint 内插入两者。索引写入失败会回滚本条记忆写入；调用方的外层事务保留自身修改，并决定最终提交。搜索先关联仍存在的记忆再限制结果数，但孤儿索引仍可能影响语料排名。

原生 `memory_write` 结果在成功写入后持久化 `presentationMeta.memoryWrite` 为 `{ok:true,id}`，返回失败时为 `{ok:false}`。ID 是实际存储行的 ID。upkeep 将此元数据与匹配的工具调用、轮次和步骤身份一起用于确认运行完成，不根据渲染文本推测写入成功。此可选结果元数据使用现有 Session 事件格式。

仅供维护者使用的[维护模块](src/maintenance.js) 在 Linux 上使用已部署的 Node 运行时执行。`node src/maintenance.js inspect /absolute/memory.db` 只读打开已有数据库，只报告行数、孤儿、缺失及文本不一致的索引数，不输出记忆内容。`node src/maintenance.js repair /absolute/memory.db /absolute/unused-backup.db` 要求写入方处于静止窗口，并在修改派生索引前创建私有 SQLite 快照。它保留原记忆的所有字段，以正常写入相同的中文切分规则重建 FTS 行，返回前后计数以及指定备份路径、快照和原记忆行的哈希。调用方已知指定的备份路径，回执不回显它。失败会回滚索引修改，不删除记忆或虚构替代内容。

备份实际配置的数据库，不假定默认目录。将备份放入私有目录，核对回执后再恢复写入。回滚时，历史完整数据库快照不能覆盖后续记忆写入；应根据当前原记忆重建索引。维护不能证明召回质量、记忆正确性或长期保留策略。[SQLite savepoint 语义](https://www.sqlite.org/lang_savepoint.html)与 [Node SQLite 备份 API](https://nodejs.org/docs/latest-v24.x/api/sqlite.html#sqlitebackupsource-db-path-options)定义事务及快照行为。
