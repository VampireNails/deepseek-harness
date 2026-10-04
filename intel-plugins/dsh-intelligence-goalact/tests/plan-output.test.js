import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { apply } from '../index.js';

test('begin presents a complete todo_write plan and keeps sensitive actions as proposals', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'goal-plan-'));
  const originalHome = process.env.DSH_HOME;
  process.env.DSH_HOME = directory;
  const tools = new Map();
  try {
    apply({
      tools: { register(tool) { tools.set(tool.name, tool); return () => {}; } },
      effect(factory) { for (const dispose of factory()) assert.equal(typeof dispose, 'function'); },
    });
    const result = await tools.get('goal_act_begin').execute({ goalId: 'g1' });
    assert.doesNotMatch(result.text, /\btodo_add\b/);
    assert.match(result.text, /todo_write.*完整 todos.*content.*in_progress/);
    assert.match(result.text, /敏感事项 goal_act_propose/);
    assert.match(result.text, /不执行/);
  } finally {
    if (originalHome === undefined) delete process.env.DSH_HOME;
    else process.env.DSH_HOME = originalHome;
    rmSync(directory, { recursive: true, force: true });
  }
});
