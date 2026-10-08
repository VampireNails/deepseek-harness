import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { openStore } from '../src/store.js';
import { FtsRetriever } from '../src/retriever.js';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/recall-golden.json', import.meta.url)));

function corpus(t) {
  const directory = mkdtempSync(join(tmpdir(), 'memory-golden-'));
  let retriever;
  t.after(() => {
    try { retriever?.close(); }
    finally { rmSync(directory, { recursive: true, force: true }); }
  });
  retriever = new FtsRetriever(openStore(directory));
  const identities = new Map();
  for (const memory of fixture.memories) {
    const { id } = retriever.add({ text: memory.text, kind: memory.kind });
    identities.set(id, memory.id);
  }
  return {
    search(query, limit) {
      return retriever.search(query, limit).map(hit => {
        assert.ok(identities.has(hit.id));
        return identities.get(hit.id);
      });
    },
  };
}

test('golden corpus keeps independent judgments and untyped storage', () => {
  assert.equal(fixture.synthetic, true);
  assert.equal(fixture.privateUserContent, false);
  assert.equal(fixture.memories.length, 37);
  assert.equal(fixture.queries.length, 40);
  assert.equal(new Set(fixture.memories.map(memory => memory.id)).size, 37);
  assert.equal(new Set(fixture.queries.map(query => query.id)).size, 40);
  assert.ok(fixture.memories.every(memory => memory.kind === 'note'));
  for (const query of fixture.queries) {
    assert.ok(query.relevant.every(id => fixture.memories.some(memory => memory.id === id)));
  }
});

for (const query of fixture.queries) {
  test(`golden ${query.id}: ${query.query}`, t => {
    const memory = corpus(t);
    for (const limit of [3, 5]) {
      const ids = memory.search(query.query, limit);
      assert.ok(ids.length <= limit);
      assert.equal(new Set(ids).size, ids.length);
      if (query.relevant.length === 0) assert.deepEqual(ids, []);
      else assert.ok(query.relevant.includes(ids[0]), `First result must be acceptable: ${JSON.stringify(ids)}`);
    }
  });
}

test('golden quality retains measured recall and precision at both result limits', t => {
  const memory = corpus(t);
  const positives = fixture.queries.filter(query => query.relevant.length);
  // These floors preserve the measured baseline; they do not claim all returned rows are relevant.
  for (const { limit, recallFloor, precisionFloor } of [
    { limit: 3, recallFloor: 0.97, precisionFloor: 0.60 },
    { limit: 5, recallFloor: 0.98, precisionFloor: 0.55 },
  ]) {
    let recall = 0, precision = 0;
    for (const query of positives) {
      const ids = memory.search(query.query, limit);
      const relevant = ids.filter(id => query.relevant.includes(id)).length;
      recall += relevant / query.relevant.length;
      precision += ids.length ? relevant / ids.length : 0;
    }
    assert.ok(recall / positives.length >= recallFloor, `Recall@${limit}: ${recall / positives.length}`);
    assert.ok(precision / positives.length >= precisionFloor, `Precision@${limit}: ${precision / positives.length}`);
  }
});
