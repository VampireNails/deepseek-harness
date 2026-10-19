import { test, after } from "node:test";
import assert from "node:assert/strict";
import { writeFileSync, mkdtempSync, rmSync, mkdirSync, readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { checkInvariants, apply } from "../index.js";

const root = mkdtempSync("/tmp/guard-");
const hmcConfig = root + "/hmc.json";
const codexCreds = root + "/credentials.yaml";
writeFileSync(hmcConfig, "{}");
writeFileSync(codexCreds, JSON.stringify({ version: 1, records: {
  "llm-pi-ai/openai-codex": { kind: "grant", payload: {
    type: "oauth", access: "fixture-access", refresh: "fixture-refresh", expires: 1,
  } },
} }));
after(() => rmSync(root, { recursive: true, force: true }));

const cleanEnv = () => ({
  DEEPSEEK_API_KEY: "sk-test-key-12345678",
  HMC_CONFIG: hmcConfig,
  PATH: "/usr/bin",
  HOME: "/root",
});

const NO_CREDS = root + "/missing.yaml";

test("干净生产环境：全部通过", () => {
  const rs = checkInvariants(cleanEnv(), NO_CREDS);
  assert.ok(rs.every((r) => r.ok), JSON.stringify(rs.filter((r) => !r.ok)));
});

test("DEEPSEEK_BASE_URL 被设置：fail", () => {
  const env = { ...cleanEnv(), DEEPSEEK_BASE_URL: "http://127.0.0.1:18099/v1" };
  const rs = checkInvariants(env, NO_CREDS);
  const hit = rs.find((r) => r.name.includes("BASE_URL"));
  assert.ok(!hit.ok);
  assert.ok(hit.detail.includes("18099"));
});

test("缺少所有 provider 凭证：fail", () => {
  const env = { ...cleanEnv() };
  delete env.DEEPSEEK_API_KEY;
  const rs = checkInvariants(env, NO_CREDS);
  assert.ok(!rs.find((r) => r.name.includes("provider 凭证")).ok);
});

test("仅 codex 凭证也通过（不绑定 DeepSeek）", () => {
  const env = { ...cleanEnv() };
  delete env.DEEPSEEK_API_KEY;
  const rs = checkInvariants(env, codexCreds);
  const hit = rs.find((r) => r.name.includes("provider 凭证"));
  assert.ok(hit.ok, JSON.stringify(hit));
});

test("默认凭据目录是用户 HOME 下的 .dsh", () => {
  mkdirSync(root + "/.dsh");
  writeFileSync(root + "/.dsh/.credentials.yaml", readFileSync(codexCreds));
  const source = new URL("../index.js", import.meta.url).href;
  const code = `import { checkInvariants } from ${JSON.stringify(source)};
    console.log(checkInvariants({}).find(r => r.name.includes("provider 凭证")).ok);`;
  const output = execFileSync(process.execPath, ["--input-type=module", "-e", code],
    { env: { ...process.env, HOME: root }, encoding: "utf8" });
  assert.equal(output.trim(), "true");
});

test("注释、空条目、缺少 payload 或错误 YAML 不构成 codex 凭证", () => {
  const env = { ...cleanEnv() };
  delete env.DEEPSEEK_API_KEY;
  const bad = root + "/bad.yaml";
  for (const text of ["# llm-pi-ai/openai-codex\n", "llm-pi-ai/openai-codex:\n",
    "version: 1\nrecords:\n  llm-pi-ai/openai-codex: {kind: grant}\n",
    "version: 1\nrecords:\n  llm-pi-ai/openai-codex: {kind: grant, payload: {}}\n",
    "version: [broken\n"]) {
    writeFileSync(bad, text);
    const hit = checkInvariants(env, bad).find(r => r.name.includes("provider 凭证"));
    assert.equal(hit.ok, false);
  }
});

test("HMC_CONFIG 缺失或文件不存在：warning，其他检查仍通过", () => {
  for (const value of [undefined, "/tmp/no-such-file-xyz.json"]) {
    const env = { ...cleanEnv(), HMC_CONFIG: value };
    const rs = checkInvariants(env, NO_CREDS);
    const hit = rs.find((r) => r.name.includes("HMC_CONFIG"));
    assert.equal(hit.ok, false);
    assert.equal(hit.severity, "warning");
    assert.match(hit.detail, /43197/);
    assert.ok(rs.filter((r) => !r.ok).every((r) => r.severity === "warning"));
  }
});

test("首次配置允许模型凭据缺失，普通模式仍拒绝", () => {
  const env = { ...cleanEnv(), DEEPSEEK_API_KEY: "", DSH_FIRST_USE: "1" };
  const hit = checkInvariants(env, NO_CREDS).find((r) => r.name.includes("provider 凭证"));
  assert.equal(hit.ok, false);
  assert.equal(hit.severity, "warning");
  for (const value of [undefined, "0", "true"]) {
    const normal = checkInvariants({ ...env, DSH_FIRST_USE: value }, NO_CREDS);
    assert.notEqual(normal.find((r) => r.name.includes("provider 凭证")).severity, "warning");
  }
});

test("apply() 仅 warning 时记录日志并继续", (t) => {
  const originalEnv = process.env;
  process.env = { DSH_HOME: root + "/no-credentials", DSH_FIRST_USE: "1" };
  t.after(() => { process.env = originalEnv; });
  const lines = [];
  t.mock.method(console, "warn", (line) => lines.push(line));
  assert.doesNotThrow(() => apply({}, { enforce: true }));
  assert.ok(lines.some((line) => /\[WARN\].*HMC_CONFIG.*43197/.test(line)));
  assert.ok(lines.some((line) => /\[WARN\].*provider/.test(line)));
});

test("首次配置仍拒绝 mock 与端点污染", () => {
  const env = { ...cleanEnv(), HMC_CONFIG: "/tmp/no-such-file-xyz.json" };
  const rs = checkInvariants({ ...env, DSH_FIRST_USE: "1", MOCK_LLM: "1", DEEPSEEK_BASE_URL: "http://127.0.0.1:18099/v1" }, NO_CREDS);
  for (const name of ["BASE_URL", "MOCK"]) {
    const hit = rs.find((r) => r.name.includes(name));
    assert.equal(hit.ok, false);
    assert.notEqual(hit.severity, "warning");
  }
});

test("MOCK 残留变量：fail", () => {
  const env = { ...cleanEnv(), MOCK_LLM: "1" };
  const rs = checkInvariants(env, NO_CREDS);
  assert.ok(!rs.find((r) => r.name.includes("MOCK")).ok);
});

test("apply() 违反时抛错（fail-closed）", () => {
  const orig = process.env.DEEPSEEK_BASE_URL;
  process.env.DEEPSEEK_BASE_URL = "http://127.0.0.1:18099/v1";
  assert.throws(() => apply({}, { enforce: true }), /拒绝启动/);
  if (orig === undefined) delete process.env.DEEPSEEK_BASE_URL;
  else process.env.DEEPSEEK_BASE_URL = orig;
});
