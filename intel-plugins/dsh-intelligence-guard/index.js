// dsh-intelligence-guard —— 生产环境不变式守卫
//
// 只挂载在 web（生产）profile。插件 apply() 在 profile 加载时执行，
// 早于任何 agent 回合。若生产环境契约被违反，直接抛错，
// 使 dsh 拒绝启动（fail-closed），而不是带着污染的环境继续跑。
//
// 防的是 2026-09-28 事故类：测试 shell 的 DEEPSEEK_BASE_URL 泄漏进
// 手动启动的生产进程，导致 HMC 收到 mock 回复。
// dsh 上游把 DEEPSEEK_BASE_URL 列为 BOOTSTRAP_NAMES（只能来自进程
// ambient env，.env 文件设不了），故必须在进程启动层拦截。

import z from "@deepseek-ai/schemastery";
import { existsSync } from "node:fs";

export const name = "dsh-intelligence-guard";
export const inject = [];

export const Config = z.object({
  // 允许通过环境变量覆盖，便于测试；默认按生产契约检查
  enforce: z.boolean().default(true),
}).default({});

// 生产环境不变式：返回 [{ ok, name, detail }]
export function checkInvariants(env = process.env) {
  const results = [];

  // 1. DEEPSEEK_BASE_URL 必须未设置（mock 泄漏主向量）
  //    生产走 api.deepseek.com 默认值；任何显式设置都是可疑的
  const baseUrl = env.DEEPSEEK_BASE_URL;
  results.push({
    ok: !baseUrl,
    name: "DEEPSEEK_BASE_URL 未设置",
    detail: baseUrl
      ? `检测到 DEEPSEEK_BASE_URL=${baseUrl}。生产 profile 禁止覆盖 LLM 入口。` +
        `修复：在干净 shell 中启动，或用 dsh-prod-start；` +
        `测试 mock 请用 --profile web-intel。`
      : "ok",
  });

  // 2. DEEPSEEK_API_KEY 必须存在且非空
  const key = env.DEEPSEEK_API_KEY;
  results.push({
    ok: !!key && key.length >= 8,
    name: "DEEPSEEK_API_KEY 已设置",
    detail: key && key.length >= 8 ? "ok" : "生产 profile 必须有真 key。检查 /etc/deepseek-harness/.env。",
  });

  // 3. HMC_CONFIG 指向的文件必须存在（HMC 生产依赖它）
  const hmcConfig = env.HMC_CONFIG;
  if (hmcConfig) {
    results.push({
      ok: existsSync(hmcConfig),
      name: "HMC_CONFIG 文件存在",
      detail: existsSync(hmcConfig) ? "ok" : `HMC_CONFIG=${hmcConfig} 不存在，HMC 服务起不来。`,
    });
  }

  // 4. 明显的测试残留：MOCK 标记
  const mockKeys = Object.keys(env).filter((k) =>
    /MOCK/i.test(k) && !/TELEMETRY/i.test(k)
  );
  results.push({
    ok: mockKeys.length === 0,
    name: "无 MOCK* 环境残留",
    detail: mockKeys.length === 0 ? "ok" : `发现可疑变量: ${mockKeys.join(", ")}`,
  });

  return results;
}

export function apply(ctx, config) {
  if (config && config.enforce === false) return;
  const failures = checkInvariants().filter((r) => !r.ok);
  if (failures.length > 0) {
    const lines = failures.map((f) => `  [FAIL] ${f.name}: ${f.detail}`);
    throw new Error(
      `[dsh-intelligence-guard] 生产环境契约违反，拒绝启动：\n${lines.join("\n")}\n` +
      `这是 fail-closed 保护。如需测试，用 --profile web-intel；如需生产，用 dsh-prod-start。`
    );
  }
  // 通过时静默，不污染日志
}
