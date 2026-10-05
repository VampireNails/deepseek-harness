# JobLog

English | [中文](README.zh.md)

JobLog stores the latest state per task in `intel-joblog/job_runs.json`, appends failure alerts to `job_alerts.md`, and retains the wrapper's bounded `jobs.log`. The task runner's process audit remains in `route-runs.jsonl`; a last-success state does not erase earlier failure alerts or establish a long-term success rate.

New records carry an explicit `production` or `test` scope. `JobLog(dir, { scope: 'test' })` marks an isolated writer; the plugin's `testRecords` configuration defaults to false. Test states use `job_test_runs.json` separately, so the same task name cannot overwrite its production state. The runner and ordinary writers use production scope. Default tools omit only explicitly marked tests; `includeTests: true` reads all records. Task names never determine scope, and unmarked older records remain visible with their dates and an unmarked-source label. Filtering does not rewrite or delete stored records. Invalid state fails visibly rather than displaying an empty successful view.

Alert details are indented so failure text cannot introduce a scoped record header. `selectAlerts(text, options)` filters a caller's already bounded read; it retains unmarked or unrecognized blocks. Both tools render their selected data as logged text. An empty selected view does not establish that every task succeeded.

Preserve the three production files and `job_test_runs.json` when backing up or rolling back. Earlier readers can display marked test alerts as ordinary history and do not read the separate test-state file; the complete records remain stored. Upgrade does not reclassify unknown historical tests, modify private task definitions, or clear old alerts.

Run `node --test intel-plugins/dsh-intelligence-joblog/tests/*.test.js` on Linux. The owning fixtures use temporary directories and no production Host or models.
