import { test } from "node:test";
import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import { checkInvariants, apply } from "../index.js";

writeFileSync("/tmp/definitely-exists.json", "{}");

const cleanEnv = () => ({
  DEEPSEEK_API_KEY: "sk-test-key-12345678",
  HMC_CONFIG: "/tmp/definitely-exists.json",
  PATH: "/usr/bin",
  HOME: "/root",
});

const NO_CREDS = "/tmp/no-such-creds.yaml";

test("干净生产环境：全部通过", () => {
  const rs = checkInvariants(cleanEnv());
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
  const rs = checkInvariants(env);
  const hit = rs.find((r) => r.name.includes("provider 凭证"));
  assert.ok(hit.ok, JSON.stringify(hit));
});

test("HMC_CONFIG 指向不存在文件：fail", () => {
  const env = { ...cleanEnv(), HMC_CONFIG: "/tmp/no-such-file-xyz.json" };
  const rs = checkInvariants(env);
  assert.ok(!rs.find((r) => r.name.includes("HMC_CONFIG")).ok);
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
