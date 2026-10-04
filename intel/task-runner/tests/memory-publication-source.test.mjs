import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';

const memoryPlugin = new URL('../../../intel-plugins/dsh-intelligence-memory/index.js', import.meta.url).href;
const memoryStore = new URL('../../../intel-plugins/dsh-intelligence-memory/src/store.js', import.meta.url).href;
const feedPlugin = new URL('../../../intel-plugins/dsh-intelligence-feed/index.js', import.meta.url).href;
const artifactPlugin = new URL('../../../intel-plugins/dsh-intelligence-artifacts/index.js', import.meta.url).href;
const inspector = new URL('../outputs.mjs', import.meta.url).href;

function checkSource(t, mode) {
  const directory = mkdtempSync(join(tmpdir(), 'memory-publication-source-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const home = join(directory, 'home');
  // The subprocess owns the real memory plugin's SQLite connection and closes
  // it on exit. Neither fixture ever loads application profiles or credentials.
  const source = `
import assert from 'node:assert/strict';
import { existsSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { apply as applyMemory } from ${JSON.stringify(memoryPlugin)};
import { openStore } from ${JSON.stringify(memoryStore)};
import { apply as applyFeed } from ${JSON.stringify(feedPlugin)};
import { apply as applyArtifacts } from ${JSON.stringify(artifactPlugin)};
import { inspectVisibleOutputs } from ${JSON.stringify(inspector)};

const home = process.env.DSH_HOME, custom = join(home, 'configured-memory');
const defaultDirectory = join(home, 'intel-memory');
mkdirSync(home, { recursive: true });
const customDb = openStore(custom);
const reference = Number(customDb.prepare('INSERT INTO memories(text,kind,created_at) VALUES (?,?,?)')
  .run('Actual configured memory source', 'fact', 1791129600000).lastInsertRowid);
customDb.close();
if (process.argv[1] === 'same-id-other-store') {
  const other = openStore(defaultDirectory);
  try {
    const unrelated = Number(other.prepare('INSERT INTO memories(text,kind,created_at) VALUES (?,?,?)')
      .run('Unrelated default memory with the same numeric ID', 'fact', 1791129600000).lastInsertRowid);
    assert.equal(unrelated, reference);
  } finally { other.close(); }
}
const tools = new Map(), services = new Map();
const ctx = {
  provide: (name, value) => services.set(name, value),
  get: name => services.get(name),
  on: () => () => {},
  sessionProjections: { register: () => () => {} },
  tools: { register: tool => { tools.set(tool.name, tool); return () => tools.delete(tool.name); } },
  effect: factory => {
    const result = factory();
    if (result && typeof result.next === 'function') {
      for (const disposer of result) assert.equal(typeof disposer, 'function');
    } else assert.equal(typeof result, 'function');
    return () => {};
  },
};
applyMemory(ctx, { dataDir: custom });
assert.ok(services.has('memoryReferences'), 'Real memory plugin must provide the configured authority');
applyFeed(ctx, {});
applyArtifacts(ctx, {});
await tools.get('feed_post').execute({ title: 'Source report', body: 'Verified custom memory source',
  references: [reference], memoryDirectory: defaultDirectory });
await tools.get('artifact_save').execute({ title: 'Source artifact', content: 'Verified custom memory source',
  kind: 'text', references: [reference], memoryDirectory: defaultDirectory });
const accepted = inspectVisibleOutputs(home);
assert.equal(accepted.length, 2, 'Readable Feed and Artifact must use the configured memory authority');
if (process.argv[1] === 'no-default-store') {
  assert.equal(existsSync(join(defaultDirectory, 'memory.db')), false);
  for (const publication of accepted) {
    assert.equal(publication.memoryDirectory, custom, 'Model arguments cannot replace the provider directory');
    assert.deepEqual(publication.references, [reference]);
  }
} else {
  const changed = openStore(custom);
  try { changed.prepare('DELETE FROM memories WHERE id=?').run(reference); }
  finally { changed.close(); }
  assert.deepEqual(inspectVisibleOutputs(home), [], 'Same ID in a different database is not the cited source');
  const unchanged = openStore(defaultDirectory);
  try { assert.ok(unchanged.prepare('SELECT id FROM memories WHERE id=?').get(reference)); }
  finally { unchanged.close(); }
}
`;
  const inherited = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    !/KEY|SECRET|TOKEN|PASSWORD/i.test(key) && !/^INTEL_(TASK_ID|RUN_ID|RUN_DATE|RUNNER_LOCK)$/.test(key)));
  const result = spawnSync(process.execPath, ['--input-type=module', '-e', source, mode], {
    env: { ...inherited, DSH_HOME: home, INTEL_TASK_ID: 'evolve_dreaming',
      INTEL_RUN_ID: 'custom-memory-source-run', INTEL_RUN_DATE: '2026-10-05' },
    encoding: 'utf8', timeout: 15000, maxBuffer: 1024 * 1024,
  });
  assert.equal(result.error, undefined, result.error?.message);
  assert.equal(result.signal, null, result.stderr);
  assert.equal(result.status, 0, result.stderr);
}

test('actual publishing tools and provider accept configured memory with no default database', t => {
  checkSource(t, 'no-default-store');
});

test('deleting a configured memory reference cannot be masked by the same ID in a default database', t => {
  checkSource(t, 'same-id-other-store');
});
