import { test, after } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

const root = mkdtempSync(join(tmpdir(), "guard-launcher-"));
after(() => rmSync(root, { recursive: true, force: true }));
const envFile = join(root, "production.env");
const hmc = join(root, "hmc.json");
const entry = join(root, "launcher");
const dsh = join(root, "dsh");
writeFileSync(dsh, '#!/bin/bash\nprintf "%s\\n" "HMC_CONFIG=${HMC_CONFIG-unset}" "DSH_FIRST_USE=${DSH_FIRST_USE-unset}" "$@"\n', { mode: 0o700 });
// Redirect only installed paths; keep shell isolation and the real guard intact.
writeFileSync(entry, readFileSync(new URL("../dsh-prod-start", import.meta.url), "utf8")
  .replaceAll("/etc/deepseek-harness/.env", envFile)
  .replaceAll("/root/intel/hmc-prod/hmc-config.json", hmc)
  .replaceAll("/root/intel/dsh-fork/intel-plugins/dsh-intelligence-guard/index.js", new URL("../index.js", import.meta.url).pathname)
  .replaceAll("DSH_HOME=/root/.dsh", `DSH_HOME=${root}/production-home/.dsh`)
  .replaceAll("HOME=/root", `HOME=${root}/production-home`)
  .replaceAll("/usr/local/bin/dsh --profile", `${dsh} --profile`));

function launch(contents, extra = {}) {
  writeFileSync(envFile, contents);
  return spawnSync("bash", [entry], {
    encoding: "utf8", timeout: 15000,
    env: { PATH: process.env.PATH, HOME: root, ...extra },
  });
}

test("缺 HMC config 仍启动 web，明确 warning，正常 config 继续注入", () => {
  const result = launch('DEEPSEEK_API_KEY="fixture-key-12345678"\n');
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stderr, /\[WARN\].*HMC_CONFIG.*43197/);
  assert.match(result.stdout, /--port\n8080/);
  writeFileSync(hmc, "{}");
  try {
    const configured = launch('DEEPSEEK_API_KEY="fixture-key-12345678"\n');
    assert.equal(configured.status, 0, configured.stderr);
    assert.match(configured.stdout, new RegExp(`HMC_CONFIG=${hmc}`));
    assert.doesNotMatch(configured.stderr, /HMC_CONFIG/);
  } finally { rmSync(hmc); }
});

test("首次配置无模型密钥可启动且保留 trusted host，调用方不能放行正常模式", () => {
  const result = launch('DSH_FIRST_USE=1\nDSH_TRUSTED_HOST=example.test\n');
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stderr, /\[WARN\].*provider/);
  assert.match(result.stdout, /--trusted-host\nexample.test/);
  const normal = launch("", { DSH_FIRST_USE: "1" });
  assert.equal(normal.status, 1);
  assert.match(normal.stderr, /拒绝启动/);
});

test("首次配置仍拒绝调用方 endpoint/mock 污染", () => {
  for (const pollution of [{ DEEPSEEK_BASE_URL: "http://127.0.0.1:18099/v1" }, { MOCK_LLM: "1" }]) {
    const result = launch("DSH_FIRST_USE=1\n", pollution);
    assert.equal(result.status, 1);
    assert.match(result.stderr, /拒绝启动/);
  }
});

test("调用方 HOME/DSH_HOME 的凭据不能满足生产凭据检查", () => {
  mkdirSync(join(root, ".dsh"));
  writeFileSync(join(root, ".dsh/.credentials.yaml"), JSON.stringify({ version: 1, records: {
    "llm-pi-ai/openai-codex": { kind: "grant", payload: {
      type: "oauth", access: "fixture-access", refresh: "fixture-refresh", expires: 1,
    } },
  } }));
  const result = launch("", { DSH_HOME: join(root, ".dsh") });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /拒绝启动/);
});
