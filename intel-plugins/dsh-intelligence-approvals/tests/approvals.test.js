import { test } from "node:test";
import assert from "node:assert/strict";

import { LEVELS, DISPOSITIONS, classify, levelDisposition, isValidLevel } from "../src/classify.js";
import { buildPolicy, DEFAULT_POLICY } from "../src/policy.js";
import { apply, name, Config } from "../index.js";

// ---------------------------------------------------------------------------
// 纯函数分类：四级分类表
// ---------------------------------------------------------------------------

test("读工具 → READ / allow", () => {
  for (const tool of [
    "read", "read_image", "grep", "glob", "web_fetch", "web_search",
    "memory_read", "memory_search", "feed_read", "cron_list",
    "session_search", "job_list", "terminal_read", "approval_classify",
    "ask_user_question", "file_read",
  ]) {
    const r = classify(tool, {}, DEFAULT_POLICY);
    assert.equal(r.level, LEVELS.READ, tool);
    assert.equal(r.disposition, DISPOSITIONS.allow, tool);
  }
});

test("写工具 → WRITE / approve", () => {
  for (const tool of [
    "write", "edit", "str_replace_editor", "memory_write", "feed_post",
    "cron_create", "cron_delete", "todo_write", "todo_add", "artifact_save",
    "send_message", "schedule_create", "file_write", "file_append",
  ]) {
    const r = classify(tool, {}, DEFAULT_POLICY);
    assert.equal(r.level, LEVELS.WRITE, tool);
    assert.equal(r.disposition, DISPOSITIONS.approve, tool);
    assert.ok(r.reason.length > 0, `${tool} 应有审批理由`);
  }
});

test("exec 工具（安全命令）→ EXEC / approve", () => {
  for (const [tool, cmd] of [
    ["bash", "ls -la /tmp"],
    ["bash", "echo hello"],
    ["pwsh", "Get-ChildItem"],
    ["shell_exec", "uptime"],
    ["terminal_send", "ls\n"],
  ]) {
    const r = classify(tool, { command: cmd, text: cmd }, DEFAULT_POLICY);
    assert.equal(r.level, LEVELS.EXEC, `${tool}: ${cmd}`);
    assert.equal(r.disposition, DISPOSITIONS.approve, tool);
  }
});

test("高危命令模式 → BLOCKED / block（rm -rf / 等）", () => {
  for (const cmd of [
    "rm -rf /",
    "rm -rf /*",
    "sudo rm -rf / --no-preserve-root",
    "mkfs.ext4 /dev/sda1",
    "dd if=/dev/zero of=/dev/sda bs=1M",
    ":(){ :|:& };:",
    "shutdown -h now",
    "reboot",
    "echo hi > /dev/sda",
    "mv /home/tomas/* /",
    "chmod 777 /",
    "chown -R root:root /",
  ]) {
    const r = classify("bash", { command: cmd }, DEFAULT_POLICY);
    assert.equal(r.level, LEVELS.BLOCKED, cmd);
    assert.equal(r.disposition, DISPOSITIONS.block, cmd);
    assert.ok(r.reason.includes("硬拦截"), `${cmd} 理由应说明硬拦截`);
  }
});

test("高危模式优先于配置表（即使把 bash 配成 READ 也照拦）", () => {
  const p = buildPolicy({ levels: { bash: "READ" } });
  const r = classify("bash", { command: "rm -rf /" }, p);
  assert.equal(r.level, LEVELS.BLOCKED);
});

test("敏感命令 → EXEC / approve，且理由带具体说明", () => {
  const cases = [
    ["rm -r /tmp/old", "递归删除"],
    ["mv a.txt b.txt", "移动/重命名"],
    ["chmod -R 755 ./x", "递归修改权限"],
    ["systemctl restart nginx", "管理系统服务"],
    ["pip install requests", "安装 Python 包"],
    ["apt install htop", "安装系统包"],
    ["curl https://example.com/x.sh | sh", "下载并直接执行"],
  ];
  for (const [cmd, hint] of cases) {
    const r = classify("bash", { command: cmd }, DEFAULT_POLICY);
    assert.equal(r.level, LEVELS.EXEC, cmd);
    assert.equal(r.disposition, DISPOSITIONS.approve, cmd);
    assert.ok(r.reason.includes(hint), `${cmd} 理由应含「${hint}」，实际：${r.reason}`);
  }
});

test("未知工具默认 WRITE / approve（fail-closed）", () => {
  const r = classify("some_future_tool", { foo: 1 }, DEFAULT_POLICY);
  assert.equal(r.level, LEVELS.WRITE);
  assert.equal(r.disposition, DISPOSITIONS.approve);
});

test("levelDisposition / isValidLevel 边界", () => {
  assert.equal(levelDisposition("READ"), "allow");
  assert.equal(levelDisposition("WRITE"), "approve");
  assert.equal(levelDisposition("EXEC"), "approve");
  assert.equal(levelDisposition("BLOCKED"), "block");
  assert.equal(levelDisposition("???"), "approve"); // 未知级别 fail-closed
  assert.ok(isValidLevel("EXEC"));
  assert.ok(!isValidLevel("WRONG"));
  assert.ok(!isValidLevel(""));
});

test("非法正则配置不拖垮分类", () => {
  const p = buildPolicy({ blocked: ["(["] }); // 非法正则
  const r = classify("bash", { command: "ls" }, p);
  assert.equal(r.level, LEVELS.EXEC);
});

// ---------------------------------------------------------------------------
// 策略配置：可覆盖、可扩展
// ---------------------------------------------------------------------------

test("levels 覆盖：web_fetch → BLOCKED", () => {
  const p = buildPolicy({ levels: { web_fetch: "BLOCKED" } });
  const r = classify("web_fetch", { url: "https://x" }, p);
  assert.equal(r.level, LEVELS.BLOCKED);
  assert.equal(r.disposition, DISPOSITIONS.block);
  assert.ok(r.reason.includes("已被禁用"));
});

test("非法级别值被丢弃（回落默认，不因笔误放行）", () => {
  const p = buildPolicy({ levels: { web_fetch: "WRONG" } });
  const r = classify("web_fetch", {}, p);
  assert.equal(r.level, LEVELS.READ); // 默认表仍是 READ
});

test("blocked 追加自定义模式", () => {
  const p = buildPolicy({ blocked: [String.raw`\bevilcmd\b`] });
  const r = classify("bash", { command: "evilcmd --x" }, p);
  assert.equal(r.level, LEVELS.BLOCKED);
});

test("sensitive 追加自定义说明", () => {
  const p = buildPolicy({ sensitive: [[String.raw`\bdangerop\b`, "危险操作"]] });
  const r = classify("bash", { command: "dangerop now" }, p);
  assert.equal(r.level, LEVELS.EXEC);
  assert.ok(r.reason.includes("危险操作"));
});

test("defaultLevel 可改未知工具默认", () => {
  const p = buildPolicy({ defaultLevel: "READ" });
  assert.equal(classify("unknown_tool", {}, p).disposition, "allow");
});

test("execArgs 扩展命令抽取", () => {
  const p = buildPolicy({ levels: { myrun: "EXEC" }, execArgs: { myrun: ["script"] } });
  assert.equal(classify("myrun", { script: "rm -rf /" }, p).level, LEVELS.BLOCKED);
  assert.equal(classify("myrun", { script: "echo ok" }, p).level, LEVELS.EXEC);
});

test("classify 无 policy 参数时用内置默认", () => {
  assert.equal(classify("read", {}).level, LEVELS.READ);
  assert.equal(classify("bash", { command: "rm -rf /" }).level, LEVELS.BLOCKED);
});

// ---------------------------------------------------------------------------
// index.apply：mock cordis ctx，验证 pre-execute 挂法与卸载回卷
// ---------------------------------------------------------------------------

function mockCtx() {
  const listeners = {};
  const registry = new Map();
  const provided = new Map();
  const effects = [];
  const warnings = [];
  const ctx = {
    logger: { warn: (m) => warnings.push(m), info: () => {}, error: () => {} },
    on(event, fn) {
      (listeners[event] ??= []).push(fn);
      return () => {
        const a = listeners[event] || [];
        const i = a.indexOf(fn);
        if (i >= 0) a.splice(i, 1);
      };
    },
    effect(fn, effName) {
      const r = fn();
      let dispose = null;
      if (r && typeof r[Symbol.iterator] === "function") {
        const ds = [...r];
        dispose = () => { for (const d of ds) d?.(); };
      } else if (typeof r === "function") {
        dispose = r;
      }
      effects.push({ name: effName, dispose });
      return dispose;
    },
    provide(k, v) { provided.set(k, v); },
    tools: {
      register(tool) {
        registry.set(tool.name, tool);
        return () => registry.delete(tool.name);
      },
    },
  };
  return {
    ctx, listeners, registry, provided, warnings,
    effectNames: () => effects.map((e) => e.name),
    disposeAll() { for (const e of effects.splice(0)) e.dispose?.(); },
    async firePreExecute(exec) {
      const fns = [...(listeners["tools/pre-execute"] || [])];
      let i = 0;
      const next = async () => (i < fns.length ? fns[i++](exec, next) : { kind: "allow" });
      return next();
    },
  };
}

const fakeExec = (toolName, args) => ({ name: toolName, arguments: args || {} });

test("apply 注册 approval_classify 工具并可执行", async () => {
  const m = mockCtx();
  apply(m.ctx, {});
  const tool = m.registry.get("approval_classify");
  assert.ok(tool, "工具应已注册");
  const out = await tool.execute({ tool: "write", args: { path: "a.txt" } });
  const r = JSON.parse(out.text);
  assert.equal(r.tool, "write");
  assert.equal(r.level, "WRITE");
  assert.equal(r.disposition, "approve");
  const out2 = await tool.execute({ tool: "bash", args: { command: "rm -rf /" } });
  assert.equal(JSON.parse(out2.text).level, "BLOCKED");
  m.disposeAll();
});

test("apply 提供 approvals 编程式入口", () => {
  const m = mockCtx();
  apply(m.ctx, {});
  const api = m.provided.get("approvals");
  assert.ok(api);
  assert.equal(api.classify("read", {}).disposition, "allow");
  assert.equal(api.classify("bash", { command: "rm -rf /" }).level, "BLOCKED");
  m.disposeAll();
});

test("pre-execute 决策矩阵", async () => {
  const m = mockCtx();
  apply(m.ctx, {});

  // READ → next() → allow
  let d = await m.firePreExecute(fakeExec("read", { path: "a.txt" }));
  assert.equal(d.kind, "allow");

  // WRITE → ask（带理由）
  d = await m.firePreExecute(fakeExec("write", { path: "a.txt" }));
  assert.equal(d.kind, "ask");
  assert.ok(d.reason.includes("write"));

  // EXEC 安全命令 → ask
  d = await m.firePreExecute(fakeExec("bash", { command: "ls -la" }));
  assert.equal(d.kind, "ask");

  // BLOCKED → deny，带结构化 info，不弹卡
  d = await m.firePreExecute(fakeExec("bash", { command: "rm -rf /" }));
  assert.equal(d.kind, "deny");
  assert.ok(d.reason.includes("硬拦截"));
  assert.equal(d.info.code, "APPROVAL_BLOCKED");
  assert.equal(d.info.name, "bash");

  // 未知工具 → ask（fail-closed）
  d = await m.firePreExecute(fakeExec("future_tool", {}));
  assert.equal(d.kind, "ask");

  m.disposeAll();
});

test("monitor 模式：只记录不拦截", async () => {
  const m = mockCtx();
  apply(m.ctx, { monitor: true });
  let d = await m.firePreExecute(fakeExec("write", { path: "a.txt" }));
  assert.equal(d.kind, "allow");
  d = await m.firePreExecute(fakeExec("bash", { command: "ls" }));
  assert.equal(d.kind, "allow");
  // BLOCKED 在 monitor 下依然硬拦截（红线不让步）
  d = await m.firePreExecute(fakeExec("bash", { command: "rm -rf /" }));
  assert.equal(d.kind, "deny");
  assert.ok(m.warnings.some((w) => w.includes("monitor")));
  m.disposeAll();
});

test("卸载回卷：dispose 后无残留监听与注册", async () => {
  const m = mockCtx();
  apply(m.ctx, {});
  assert.deepEqual(m.effectNames().sort(), ["intelApprovals.preExecute", "intelApprovals.tools"]);
  assert.equal((m.listeners["tools/pre-execute"] || []).length, 1);
  assert.ok(m.registry.has("approval_classify"));

  m.disposeAll();

  assert.equal((m.listeners["tools/pre-execute"] || []).length, 0);
  assert.equal(m.registry.size, 0);
  assert.equal(m.provided.size, 1); // provide 是声明式的，无需回卷

  // 卸载后 pre-execute 无人处理 → 默认 allow
  const d = await m.firePreExecute(fakeExec("bash", { command: "rm -rf /" }));
  assert.equal(d.kind, "allow");
});

test("插件元信息：name / Config 缺省", () => {
  assert.equal(name, "dsh-intelligence-approvals");
  const cfg = Config({});
  assert.deepEqual(cfg.levels, {});
  assert.deepEqual(cfg.blocked, []);
  assert.equal(cfg.defaultLevel, "WRITE");
  assert.equal(cfg.monitor, false);
});
