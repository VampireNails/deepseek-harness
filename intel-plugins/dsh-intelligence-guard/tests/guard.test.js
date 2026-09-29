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

test("干净生产环境：全部通过", () => {
  const rs = checkInvariants(cleanEnv());
  assert.ok(rs.every((r) => r.ok), JSON.stringify(rs.filter((r) => !r.ok)));
});

test("DEEPSEEK_BASE_URL 被设置：fail", () => {
  const env = { ...cleanEnv(), DEEPSEEK_BASE_URL: "http://127.0.0.1:18099/v1" };
  const rs = checkInvariants(env);
  const hit = rs.find((r) => r.name.includes("BASE_URL"));
  assert.ok(!hit.ok);
  assert.ok(hit.detail.includes("18099"));
});

test("缺少 API_KEY：fail", () => {
  const env = { ...cleanEnv() };
  delete env.DEEPSEEK_API_KEY;
  const rs = checkInvariants(env);
  assert.ok(!rs.find((r) => r.name.includes("API_KEY")).ok);
});

test("HMC_CONFIG 指向不存在文件：fail", () => {
  const env = { ...cleanEnv(), HMC_CONFIG: "/tmp/no-such-file-xyz.json" };
  const rs = checkInvariants(env);
  assert.ok(!rs.find((r) => r.name.includes("HMC_CONFIG")).ok);
});

test("MOCK 残留变量：fail", () => {
  const env = { ...cleanEnv(), MOCK_LLM: "1" };
  const rs = checkInvariants(env);
  assert.ok(!rs.find((r) => r.name.includes("MOCK")).ok);
});

test("apply() 违反时抛错（fail-closed）", () => {
  const orig = process.env.DEEPSEEK_BASE_URL;
  process.env.DEEPSEEK_BASE_URL = "http://127.0.0.1:18099/v1";
  assert.throws(() => apply({}, { enforce: true }), /拒绝启动/);
  if (orig === undefined) delete process.env.DEEPSEEK_BASE_URL;
  else process.env.DEEPSEEK_BASE_URL = orig;
});

test("apply() enforce=false 时跳过", () => {
  const orig = process.env.DEEPSEEK_BASE_URL;
  process.env.DEEPSEEK_BASE_URL = "http://127.0.0.1:18099/v1";
  assert.doesNotThrow(() => apply({}, { enforce: false }));
  if (orig === undefined) delete process.env.DEEPSEEK_BASE_URL;
  else process.env.DEEPSEEK_BASE_URL = orig;
});
