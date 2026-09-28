import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, readFileSync, writeFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// profile 插件的纯逻辑：read / update section
// 为可测，把文件操作收敛到小函数（与 index.js 同构）

function makeProfile(dir) {
  const file = join(dir, "user_profile.md");
  const read = () => {
    if (!existsSync(file)) return "";
    return readFileSync(file, "utf8").trim();
  };
  const update = (section, content) => {
    let text = read();
    const header = `## ${section}`;
    const esc = section.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    if (text.includes(header)) {
      const re = new RegExp(`## ${esc}\n([\\s\\S]*?)(?=\\n## |\\s*$)`);
      text = text.replace(re, `${header}\n${content}\n`);
    } else {
      text = text ? `${text}\n\n${header}\n${content}\n` : `# 用户画像\n\n${header}\n${content}\n`;
    }
    writeFileSync(file, text);
    return text;
  };
  return { read, update };
}

test("空画像读出为空", () => {
  const dir = mkdtempSync(join(tmpdir(), "profile-test-"));
  try {
    assert.equal(makeProfile(dir).read(), "");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("update 新增小节", () => {
  const dir = mkdtempSync(join(tmpdir(), "profile-test-"));
  try {
    const p = makeProfile(dir);
    p.update("偏好与禁忌", "- 喜欢简洁\n");
    const t = p.read();
    assert.ok(t.includes("## 偏好与禁忌"));
    assert.ok(t.includes("喜欢简洁"));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("update 替换已有小节，其他小节不动", () => {
  const dir = mkdtempSync(join(tmpdir(), "profile-test-"));
  try {
    const p = makeProfile(dir);
    p.update("A", "- 旧内容\n");
    p.update("B", "- B 内容\n");
    p.update("A", "- 新内容\n");
    const t = p.read();
    assert.ok(t.includes("- 新内容"));
    assert.ok(!t.includes("- 旧内容"));
    assert.ok(t.includes("- B 内容"), "B 小节应保留");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
