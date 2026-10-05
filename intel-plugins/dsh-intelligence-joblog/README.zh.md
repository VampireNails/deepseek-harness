# JobLog

[English](README.md) | 中文

JobLog 在 `intel-joblog/job_runs.json` 保存各任务的最后状态，将失败告警追加到 `job_alerts.md`，并保留包装器有界的 `jobs.log`。任务运行器的进程审计仍在 `route-runs.jsonl`；最后一次成功不会清除历史失败告警，也不能证明长期成功率。

新记录明确携带 `production` 或 `test` 来源。`JobLog(dir, { scope: 'test' })` 标记隔离测试写入器；插件配置 `testRecords` 默认为 false。测试状态独立写入 `job_test_runs.json`，同名任务不能覆盖生产状态。运行器与普通写入器使用生产来源。默认工具仅排除明确标记的测试，`includeTests: true` 读取全部记录。来源不由任务名称决定；未标记的旧记录仍保留日期并显示来源未标注。筛选不重写或删除存储记录。损坏状态明确报错，不会显示为成功的空视图。

告警详情缩进，失败文本不能引入带来源标记的记录标题。`selectAlerts(text, options)` 筛选调用方已经限制读取大小的文本，保留未标记或无法识别的块。两个工具均将选中数据渲染为已记入会话日志的文本。选中视图为空不能证明所有任务成功。

备份或回滚时保留三个生产文件和 `job_test_runs.json`。旧读取器可能将带标记的测试告警显示为普通历史，不读取独立的测试状态文件，完整记录仍保存在存储中。升级不会重新归类来源未知的历史测试、修改私有任务定义或清除旧告警。

在 Linux 运行 `node --test intel-plugins/dsh-intelligence-joblog/tests/*.test.js`。所属夹具使用临时目录，不调用生产 Host 或模型。
