import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { openStore } from '../src/store.js';
import { FtsRetriever } from '../src/retriever.js';

function fixture(t, options) {
  const directory = mkdtempSync(join(tmpdir(), 'memory-query-bounds-'));
  const db = openStore(directory);
  t.after(() => { try { db.close(); } finally { rmSync(directory, { recursive: true }); } });
  return new FtsRetriever(db, options);
}

test('query character bound accepts the boundary and rejects overflow before SQLite', t => {
  const r = fixture(t, { maxQueryChars: 16, maxQueryTerms: 4 });
  const { id } = r.add({ text: 'heliostat' });
  assert.deepEqual(r.search('       heliostat').map(hit => hit.id), [id]);
  r.searchStmt = { all() { assert.fail('An excessive query must not reach SQLite'); } };
  assert.throws(() => r.search('        heliostat'), error => error.code === 'MEMORY_QUERY_TOO_LARGE');
});

test('query term bound counts distinct Han and Latin terms together without truncating', t => {
  const r = fixture(t, { maxQueryChars: 100, maxQueryTerms: 3 });
  const { id } = r.add({ text: '青铜 heliostat' });
  assert.deepEqual(r.search('青铜 heliostat 青铜 HELIOSTAT').map(hit => hit.id), [id]);
  r.searchStmt = { all() { assert.fail('Term overflow must not reach SQLite'); } };
  for (const query of ['青铜 heliostat manual', 'manual 青铜 heliostat']) {
    assert.throws(() => r.search(query), error => error.code === 'MEMORY_QUERY_TOO_LARGE');
  }
});

test('invalid query bounds fail when constructing the retriever', t => {
  for (const key of ['maxQueryChars', 'maxQueryTerms']) {
    for (const value of [0, -1, 1.5, Infinity, NaN]) {
      assert.throws(() => fixture(t, { [key]: value }), /positive safe integer/);
    }
  }
});
