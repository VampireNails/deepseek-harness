import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// soul 插件纯逻辑：自带 soul.md / DSH_HOME 覆盖 / 小节更新
// readSoul/updateSoulSection 每次调用都读 env 与文件，无模块状态，可直接测

function withTempHome(fn) {
  const dir = mkdtempSync(join(tmpdir(), "soul-test-"));
  const prev = process.env.DSH_HOME;
  process.env.DSH_HOME = dir;
  return Promise.resolve()
    .then(() => fn(dir))
    .finally(() => {
      if (prev === undefined) delete process.env.DSH_HOME;
      else process.env.DSH_HOME = prev;
      rmSync(dir, { recursive: true, force: true });
    });
}

test("自带 soul.md 非空且包含关键规则", () =>
  withTempHome(async () => {
    const m = await import("../index.js");
    const t = m.readSoul();
    assert.ok(t.length > 500, "soul 文本太短");
    for (const key of ["倾销式完成", "求真", "先验证再汇报", "烧 token", "园丁"]) {
      assert.ok(t.includes(key), `缺少关键规则：${key}`);
    }
  }));

test("DSH_HOME 下的 soul.md 覆盖自带版本", () =>
  withTempHome(async (dir) => {
    const m = await import("../index.js");
    mkdirSync(join(dir, "intel-soul"), { recursive: true });
    writeFileSync(join(dir, "intel-soul", "soul.md"), "# 自定义 Soul\n- 自定义规则\n");
    const t = m.readSoul();
    assert.ok(t.includes("自定义 Soul"));
    assert.ok(!t.includes("倾销式完成"));
  }));

test("updateSoulSection 新增小节", async () => {
  const m = await import("../index.js");
  const t = m.updateSoulSection("", "做事方式", "- 测试规则\n");
  assert.ok(t.includes("## 做事方式"));
  assert.ok(t.includes("测试规则"));
});

test("updateSoulSection 替换已有小节、其他不动", async () => {
  const m = await import("../index.js");
  const t = m.updateSoulSection("# Soul\n\n## A\n- 旧\n\n## B\n- B 内容\n", "A", "- 新\n");
  assert.ok(t.includes("- 新"));
  assert.ok(!t.includes("- 旧"));
  assert.ok(t.includes("## B") && t.includes("B 内容"));
});

test("soulFile 落在 DSH_HOME/intel-soul/soul.md", () =>
  withTempHome(async (dir) => {
    const m = await import("../index.js");
    assert.equal(m.soulFile(), join(dir, "intel-soul", "soul.md"));
  }));
