import { test } from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const PROD_START = "/usr/local/bin/dsh-prod-start";

// 启动级回归（第五轮审计 N2）：安全属性是"进程拒绝启动"，不是"apply() 抛异常"。
// 仅有权读取生产环境并执行启动器的账户才能验证；隔离 CI 不访问生产凭据。
const isProd = await import("node:fs/promises").then((fs) =>
  Promise.all([
    fs.access("/etc/deepseek-harness/.env", fs.constants.R_OK),
    fs.access(PROD_START, fs.constants.X_OK),
  ]).then(() => true).catch(() => false)
);

test("污染 DEEPSEEK_BASE_URL → dsh-prod-start 拒绝启动（exit 1）", { skip: !isProd }, async () => {
  try {
    await execFileAsync(PROD_START, ["--help"], {
      env: { ...process.env, DEEPSEEK_BASE_URL: "http://127.0.0.1:18099/v1" },
      timeout: 15000,
    });
    assert.fail("应当拒绝启动，但 exit 0");
  } catch (e) {
    assert.equal(e.code, 1, "退出码应为 1");
    assert.match(e.stderr, /拒绝启动/, "stderr 应包含拒绝启动");
    assert.match(e.stderr, /DEEPSEEK_BASE_URL/, "stderr 应指明污染源");
  }
});

test("MOCK* 残留 → dsh-prod-start 拒绝启动（exit 1）", { skip: !isProd }, async () => {
  try {
    await execFileAsync(PROD_START, ["--help"], {
      env: { ...process.env, MOCK_LLM: "1" },
      timeout: 15000,
    });
    assert.fail("应当拒绝启动，但 exit 0");
  } catch (e) {
    assert.equal(e.code, 1, "退出码应为 1");
    assert.match(e.stderr, /拒绝启动/, "stderr 应包含拒绝启动");
  }
});
