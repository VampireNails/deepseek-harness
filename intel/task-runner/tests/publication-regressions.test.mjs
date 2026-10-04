import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { FeedStore } from '../../../intel-plugins/dsh-intelligence-feed/src/feed.js';
import { ArtifactStore } from '../../../intel-plugins/dsh-intelligence-artifacts/src/artifacts.js';
import { runTask } from '../runner.mjs';

function fixture(t) {
  const directory = mkdtempSync(join(tmpdir(), 'publication-regression-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const metadata = { taskId: 'evolve_dreaming', runId: 'review-run', runDate: '2026-10-05',
    idempotencyKey: 'evolve_dreaming:2026-10-05:review-run', references: [] };
  return { directory, metadata };
}

test('an Artifact publication key cannot create another artifact by changing its title', t => {
  const { directory, metadata } = fixture(t);
  const store = new ArtifactStore(join(directory, 'artifacts'));
  const original = store.save('Original report', 'Verified result', 'text', metadata);
  const reopened = new ArtifactStore(store.dir);
  assert.throws(() => reopened.save('Renamed report', 'Verified result', 'text', metadata),
    /idempotency conflict/);
  assert.equal(reopened._findByTitle('Renamed report'), null);
  assert.equal(reopened.get(original.id).versions.length, 1);
  assert.equal(reopened.get(original.id, 1).content, 'Verified result');
});

test('Feed publication keys survive eviction from the latest 100 posts and store reopening', t => {
  const { directory, metadata } = fixture(t);
  const store = new FeedStore(join(directory, 'feed'));
  const original = store.addPost('Original report', 'Verified result', 'dreaming', metadata);
  for (let index = 0; index < 101; index++) store.addPost('Later report ' + index, 'Later result', 'task');
  const reopened = new FeedStore(store.dir);
  const before = reopened.listPosts(100);
  assert.equal(before.length, 100);
  assert.equal(before.some(post => post.id === original), false);
  assert.equal(reopened.addPost('Original report', 'Verified result', 'dreaming', metadata), original);
  assert.deepEqual(reopened.listPosts(100), before, 'Replay must not evict another visible post');
  assert.throws(() => reopened.addPost('Changed report', 'Verified result', 'dreaming', metadata),
    /idempotency conflict/);
});

test('repeated failures after Feed JSON publication cannot lose a key when the visible window evicts it', t => {
  const { directory, metadata } = fixture(t);
  class FailAfterJson extends FeedStore {
    _save(posts) {
      super._save(posts);
      throw Error('injected failure after JSON publication');
    }
  }
  const failing = new FailAfterJson(join(directory, 'feed'));
  assert.throws(() => failing.addPost('Original report', 'Verified result', 'dreaming', metadata),
    /injected failure/);
  const original = new FeedStore(failing.dir).listPosts(1)[0].id;
  for (let index = 0; index < 101; index++) {
    const later = { ...metadata, runId: 'later-run-' + index, idempotencyKey: 'later-key-' + index };
    assert.throws(() => failing.addPost('Later report ' + index, 'Later result', 'task', later),
      /injected failure/);
  }
  const reopened = new FeedStore(failing.dir);
  const before = reopened.listPosts(100);
  assert.equal(before.some(post => post.id === original), false);
  assert.equal(reopened.addPost('Original report', 'Verified result', 'dreaming', metadata), original);
  assert.deepEqual(reopened.listPosts(100), before, 'Recovery must not republish or evict a visible post');
});

test('successful fallback still requires its own persisted visible output and run audit identity', async t => {
  const { metadata } = fixture(t);
  const config = { defaultRoute: 'primary', routes: {
    primary: { provider: 'primary-provider', model: 'primary-model' },
    fallback: { provider: 'fallback-provider', model: 'fallback-model' },
  }, tasks: { evolve_dreaming: { route: 'primary', fallback: 'fallback', retrySafe: true,
    visibleOutput: 'required' } } };
  const executions = [], inspections = [], audit = [];
  const exit = await runTask(config, 'evolve_dreaming', undefined, async plan => {
    executions.push(plan);
    return executions.length === 1 ? 1 : 0;
  }, entry => audit.push(entry), { ...metadata, timeZone: 'Asia/Shanghai',
    inspectVisibleOutputs: async plan => { inspections.push(plan); return []; } });
  assert.equal(executions.length, 2, 'Missing output must not cause another execution');
  assert.equal(inspections.length, 1, 'Successful fallback needs the same output validation');
  assert.equal(inspections[0].provider, 'fallback-provider');
  assert.equal(exit, 65, 'A process exit alone cannot satisfy required output');
  const fallback = audit.find(entry => entry.kind === 'fallback');
  assert.equal(fallback.runId, metadata.runId);
  assert.equal(fallback.runDate, metadata.runDate);
  const finish = audit.at(-1);
  assert.equal(finish.kind, 'finish');
  assert.equal(finish.provider, 'fallback-provider');
  assert.equal(finish.runId, metadata.runId);
  assert.equal(finish.runDate, metadata.runDate);
  assert.equal(finish.processExit, 0);
  assert.equal(finish.exit, 65);
});
