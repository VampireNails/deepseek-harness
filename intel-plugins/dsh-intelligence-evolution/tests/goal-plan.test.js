import { test } from 'node:test';
import assert from 'node:assert/strict';
import { TASKS } from '../src/tasks.js';

test('goal planning names the registered full-list todo tool and its schema', () => {
  assert.doesNotMatch(TASKS.goal_act.prompt, /\btodo_add\b/);
  assert.match(TASKS.goal_act.prompt, /todo_write/);
  assert.match(TASKS.goal_act.prompt, /todos/);
  assert.match(TASKS.goal_act.prompt, /content/);
  assert.match(TASKS.goal_act.prompt, /in_progress/);
  assert.match(TASKS.goal_act.prompt, /完整/);
  assert.match(TASKS.goal_act.prompt, /goal_act_propose/);
});
