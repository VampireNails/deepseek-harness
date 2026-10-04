import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { FeedStore } from '../src/feed.js';
import { ArtifactStore } from '../../dsh-intelligence-artifacts/src/artifacts.js';

function child(source) {
  return new Promise((resolve, reject) => {
    const processChild = spawn(process.execPath, ['--input-type=module', '-e', source], { stdio: ['ignore', 'pipe', 'pipe'] });
    let output = '', errors = '';
    const timer = setTimeout(() => processChild.kill('SIGKILL'), 10000);
    processChild.stdout.on('data', value => output += value);
    processChild.stderr.on('data', value => errors += value);
    processChild.once('error', error => { clearTimeout(timer); reject(error); });
    processChild.once('close', code => { clearTimeout(timer); code === 0 ? resolve(JSON.parse(output)) : reject(Error(errors)); });
  });
}

test('concurrent processes publish one Feed entry and one Artifact version for a run', async t => {
  const directory = mkdtempSync(join(tmpdir(), 'publication-concurrency-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const metadata = { taskId: 'evolve_ideas', runId: 'isolated-run', runDate: '2026-10-05', idempotencyKey: 'ideas:isolated-run', references: [] };
  const feedUrl = new URL('../src/feed.js', import.meta.url).href;
  const artifactUrl = new URL('../../dsh-intelligence-artifacts/src/artifacts.js', import.meta.url).href;
  const feedDirectory = join(directory, 'feed'), artifactDirectory = join(directory, 'artifacts');
  const source = `import {FeedStore} from ${JSON.stringify(feedUrl)};import {ArtifactStore} from ${JSON.stringify(artifactUrl)};` +
    `const f=new FeedStore(${JSON.stringify(feedDirectory)}),a=new ArtifactStore(${JSON.stringify(artifactDirectory)}),m=${JSON.stringify(metadata)};` +
    `console.log(JSON.stringify([f.addPost('report','verified','task',m),a.save('report','verified','text',m).version]));`;
  const results = await Promise.allSettled(Array.from({ length: 6 }, () => child(source)));
  for (const result of results) { assert.equal(result.status, 'fulfilled', result.reason?.message); assert.deepEqual(result.value, ['f1', 1]); }
  assert.equal(new FeedStore(feedDirectory).listPosts(100).length, 1);
  const store = new ArtifactStore(artifactDirectory), artifactId = store._findByTitle('report');
  assert.equal(store.get(artifactId).versions.length, 1);
});
