import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { JobLog } from "../src/joblog.js";

function freshLog() {
  const dir = mkdtempSync(join(tmpdir(), "joblog-test-"));
  const log = new JobLog(dir);
  return { log, dir, cleanup: () => rmSync(dir, { recursive: true, force: true }) };
}

test("guarded 成功路径：记日志 + runs.json 记 ok", async () => {
  const { log, dir, cleanup } = freshLog();
  try {
    const r = await log.guarded("test_job", async () => "hello");
    assert.equal(r, "hello");
    const runs = JSON.parse(readFileSync(join(dir, "job_runs.json"), "utf8"));
    assert.equal(runs["test_job"].status, "ok");
    assert.ok(runs["test_job"].last);
    const logText = readFileSync(join(dir, "jobs.log"), "utf8");
    assert.ok(logText.includes("test_job 开始"));
    assert.ok(logText.includes("test_job 成功"));
    assert.ok(!existsSync(join(dir, "job_alerts.md")) || !readFileSync(join(dir, "job_alerts.md"), "utf8").includes("test_job"));
  } finally {
    cleanup();
  }
});

test("guarded 失败路径：runs.json 记 fail + 告警文件有记录 + 抛异常", async () => {
  const { log, dir, cleanup } = freshLog();
  try {
    await assert.rejects(
      () => log.guarded("fail_job", async () => { throw new Error("boom"); }),
      /boom/
    );
    const runs = JSON.parse(readFileSync(join(dir, "job_runs.json"), "utf8"));
    assert.equal(runs["fail_job"].status, "fail");
    assert.ok(runs["fail_job"].detail.includes("boom"));
    const alerts = readFileSync(join(dir, "job_alerts.md"), "utf8");
    assert.ok(alerts.includes("fail_job"));
    assert.ok(alerts.includes("boom"));
  } finally {
    cleanup();
  }
});

test("失败后成功：告警不重复累积覆盖 runs 状态", async () => {
  const { log, dir, cleanup } = freshLog();
  try {
    await assert.rejects(() => log.guarded("j", async () => { throw new Error("x"); }));
    await log.guarded("j", async () => "ok");
    const runs = JSON.parse(readFileSync(join(dir, "job_runs.json"), "utf8"));
    assert.equal(runs["j"].status, "ok");
  } finally {
    cleanup();
  }
});
