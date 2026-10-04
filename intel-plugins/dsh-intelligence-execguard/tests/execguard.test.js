import { test } from "node:test";
import assert from "node:assert/strict";

import { LEVELS, DANGEROUS_PATTERNS } from "../../dsh-intelligence-approvals/src/classify.js";
import { apply, name } from "../index.js";

function mockCtx() {
  const handlers = {};
  const warnings = [];
  const ctx = {
    handlers,
    warnings,
    effect(thunk) { return thunk(); },
    on(event, handler) { handlers[event] = handler; return () => {}; },
    logger: { warn(msg) { warnings.push(String(msg)); } },
  };
  return ctx;
}

async function runPreExecute(ctx, toolName, args) {
  const h = ctx.handlers["tools/pre-execute"];
  assert.ok(h, "pre-execute handler 已注册");
  let nextCalled = false;
  const out = await h(
    { name: toolName, arguments: args },
    () => { nextCalled = true; return "NEXT"; }
  );
  return { out, nextCalled };
}

test("插件名与注册", () => {
  assert.equal(name, "dsh-intelligence-execguard");
  const ctx = mockCtx();
  apply(ctx);
  assert.ok(ctx.handlers["tools/pre-execute"], "pre-execute 已注册");
});

test("全部 DANGEROUS_PATTERNS 在无头下被 deny", async () => {
  const ctx = mockCtx();
  apply(ctx);
  // 每个高危模式至少有一个代表命令被拦（与 classify.js 的语义一致）
  const samples = [
    "rm -rf /",
    "rm -rf /*",
    "mkfs.ext4 /dev/sda1",
    "dd if=/dev/zero of=/dev/sda",
    ":(){ :|:& };:",
    "shutdown -h now",
    "reboot",
    "echo x > /dev/sda",
    "mv foo / ",
    "chmod 777 /",
    "chown -R root /",
  ];
  assert.ok(samples.length >= DANGEROUS_PATTERNS.length, "样本覆盖全部模式数");
  for (const cmd of samples) {
    const { out, nextCalled } = await runPreExecute(ctx, "bash", { command: cmd });
    assert.equal(nextCalled, false, `高危命令不应放行：${cmd}`);
    assert.equal(out.kind, "deny", cmd);
    assert.match(out.reason, /高危/, cmd);
    assert.equal(out.info.code, "EXECGUARD_BLOCKED", cmd);
  }
});

test("正常命令与非执行工具一律放行（现有 cron 零变化）", async () => {
  const ctx = mockCtx();
  apply(ctx);
  for (const [tool, args] of [
    ["bash", { command: "ls /tmp && echo ok" }],
    ["bash", { command: "rm -r /tmp/stale-dir" }], // 敏感但非 BLOCKED：放行（审批语义不在本插件）
    ["memory_write", { kind: "fact", text: "hi" }], // WRITE：本插件不管（无头下由各任务自律）
    ["feed_post", { text: "hi" }],
    ["read", { path: "/tmp/x" }],
  ]) {
    const { out, nextCalled } = await runPreExecute(ctx, tool, args);
    assert.equal(nextCalled, true, `${tool} 应放行`);
    assert.equal(out, "NEXT", tool);
  }
});

test("deny 会写 warn 日志", async () => {
  const ctx = mockCtx();
  apply(ctx);
  await runPreExecute(ctx, "bash", { command: "rm -rf /" });
  assert.ok(ctx.warnings.some((w) => w.includes("BLOCKED")), "应有 BLOCKED 日志");
});

test("classify 异常拒绝该调用，日志不包含命令或异常私有详情，后续安全调用可继续", async () => {
  const ctx = mockCtx();
  apply(ctx);
  const args = { get command() { throw new Error('PRIVATE_ARGUMENT_CONTENT'); } };
  const { out, nextCalled } = await runPreExecute(ctx, "bash", args);
  assert.equal(nextCalled, false);
  assert.equal(out.kind, 'deny');
  assert.equal(out.info.code, 'EXECGUARD_CLASSIFICATION_FAILED');
  assert.ok(ctx.warnings.some(w => w.includes('CLASSIFICATION_FAILED')));
  assert.ok(ctx.warnings.every(w => !w.includes('PRIVATE_ARGUMENT_CONTENT')));
  const safe = await runPreExecute(ctx, 'bash', { command: 'echo ok' });
  assert.equal(safe.nextCalled, true);
});
