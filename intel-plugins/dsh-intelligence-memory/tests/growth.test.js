import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { openStore } from '../src/store.js';
import { FtsRetriever } from '../src/retriever.js';
import { Config } from '../index.js';

function fixture(t, options = {}) {
  const directory = mkdtempSync(join(tmpdir(), 'memory-growth-'));
  let db = openStore(directory), retriever = new FtsRetriever(db, options);
  t.after(() => { try { db.close(); } finally { rmSync(directory, { recursive: true }); } });
  return { get db() { return db; }, get r() { return retriever; }, reopen() {
    db.close(); db = openStore(directory); retriever = new FtsRetriever(db, options);
  } };
}

test('identical active writes from one source reuse ID across restart without growing original or FTS rows', t => {
  const f = fixture(t), input = { text: ' 用户偏好简洁中文 ', kind: 'preference', sessionId: 'source-a' };
  const first = f.r.add(input).id;
  for (let n = 0; n < 100; n++) assert.equal(f.r.add(input).id, first);
  f.reopen(); assert.equal(f.r.add(input).id, first);
  for (const table of ['memories', 'memories_fts']) assert.equal(f.db.prepare(`SELECT count(*) AS n FROM ${table}`).get().n, 1);
  assert.equal(f.db.prepare('SELECT session_id FROM memories WHERE id=?').get(first).session_id, 'source-a');
  assert.deepEqual(f.r.search('简洁中文').map(row => row.id), [first]);
});

test('different source, kind, content and invalidated versions retain distinct originals and correction audit', t => {
  const f = fixture(t), input = { text: '用户偏好简洁中文', sessionId: 'source-a' };
  const first = f.r.add(input).id;
  const second = f.r.add({ ...input, sessionId: 'source-b' }).id;
  const third = f.r.add({ ...input, kind: 'fact' }).id;
  const fourth = f.r.add({ ...input, text: '用户偏好详细中文' }).id;
  f.r.change({ id: first, action: 'invalidate', expectedRevision: 0, reason: 'correction', mutationId: '00000000-0000-4000-8000-000000000015' }, { source: 'paired-client' });
  const fifth = f.r.add(input).id;
  assert.equal(new Set([first, second, third, fourth, fifth]).size, 5);
  assert.equal(f.r.add(input).id, fifth);
  assert.equal(f.r.describe(first).active, false); assert.equal(f.r.describe(first).revision, 1);
  assert.equal(f.r.history(first).length, 1);
  assert.equal(f.db.prepare('SELECT count(*) AS n FROM memories').get().n, 5);
  assert.equal(f.db.prepare('SELECT count(*) AS n FROM memories_fts').get().n, 4);
});

test('legacy unknown-source duplicates are preserved and unknown sources and repeated events never merge', t => {
  const f = fixture(t);
  f.db.prepare('INSERT INTO memories(text,kind,created_at,session_id) VALUES(?,?,?,?)').run('legacy 中文', 'note', 1, null);
  f.db.prepare('INSERT INTO memories(text,kind,created_at,session_id) VALUES(?,?,?,?)').run('legacy 中文', 'note', 2, null);
  f.db.prepare('INSERT INTO memories_fts(rowid,text) VALUES(?,?)').run(1, 'legacy 中 文');
  f.db.prepare('INSERT INTO memories_fts(rowid,text) VALUES(?,?)').run(2, 'legacy 中 文');
  assert.equal(f.r.add({ text: 'legacy 中文' }).id, 3);
  assert.equal(f.r.add({ text: 'legacy 中文' }).id, 4);
  const event = { text: 'legacy 中文', kind: 'event', sessionId: 'known' };
  assert.notEqual(f.r.add(event).id, f.r.add(event).id);
  assert.equal(f.db.prepare('SELECT count(*) AS n FROM memories').get().n, 6);
});

test('capacity is opt-in and counts all originals and UTF8 bytes at and above soft thresholds without denying writes', t => {
  const off = fixture(t);
  assert.equal(off.r.add({ text: '中' }).capacity, undefined);
  const f = fixture(t, { warnMemoryRows: 2, warnMemoryBytes: 6 });
  assert.deepEqual(f.r.add({ text: '中' }).capacity, { rows: 1, textBytes: 3, warnMemoryRows: 2, warnMemoryBytes: 6, warning: false });
  const hit = { rows: 2, textBytes: 6, warnMemoryRows: 2, warnMemoryBytes: 6, warning: true };
  assert.deepEqual(f.r.add({ text: '文', sessionId: 'source' }).capacity, hit);
  assert.deepEqual(f.r.add({ text: '文', sessionId: 'source' }).capacity, hit);
  assert.equal(f.r.add({ text: '字' }).capacity.rows, 3);
});

test('capacity config accepts zero and safe upper boundary and rejects nonintegers, negatives, nonfinite and strings', t => {
  const f = fixture(t);
  for (const key of ['warnMemoryRows', 'warnMemoryBytes']) {
    for (const value of [0, 1, Number.MAX_SAFE_INTEGER]) assert.doesNotThrow(() => new FtsRetriever(f.db, { [key]: value }));
    for (const value of [-1, .5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, '2', null])
      assert.throws(() => new FtsRetriever(f.db, { [key]: value }), /non-negative safe integer/);
  }
});

test('Loader capacity schema resolves compatible defaults, explicit bounds and rejects illegal values', () => {
  assert.equal(Config({}).warnMemoryRows, 0); assert.equal(Config({}).warnMemoryBytes, 0);
  for (const key of ['warnMemoryRows', 'warnMemoryBytes']) {
    for (const value of [0, 1, Number.MAX_SAFE_INTEGER]) assert.equal(Config({ [key]: value })[key], value);
    assert.equal(Config({ [key]: null })[key], 0, 'Schemastery treats null as omitted and resolves the declared default');
    for (const value of [-1, .5, Infinity, Number.MAX_SAFE_INTEGER + 1, '2']) assert.throws(() => Config({ [key]: value }), 'invalid '+key+'='+value);
  }
});

test('disabled observation issues no aggregate query and observation failure rolls the write back', t => {
  const f = fixture(t), original = f.db.prepare.bind(f.db);
  f.db.prepare = sql => { if (/sum\(/.test(sql)) assert.fail('Disabled capacity must not scan text'); return original(sql); };
  assert.equal(f.r.add({ text: '中文', sessionId: 'known' }).id, 1);
  f.db.prepare = original;
  const observed = new FtsRetriever(f.db, { warnMemoryBytes: 1 });
  f.db.prepare = sql => { if (/sum\(/.test(sql)) throw Error('PRIVATE capacity fixture'); return original(sql); };
  assert.throws(() => observed.add({ text: 'rollback capacity', sessionId: 'known' }), /PRIVATE capacity/);
  f.db.prepare = original;
  assert.equal(f.db.prepare('SELECT count(*) AS n FROM memories').get().n, 1);
  assert.equal(f.db.prepare('SELECT count(*) AS n FROM memories_fts').get().n, 1);
});

test('restored revision is reused without new version and duplicate add obeys caller rollback', t => {
  const f = fixture(t), input = { text: '版本保留', sessionId: 'known' }, first = f.r.add(input);
  f.r.change({id:first.id,action:'invalidate',expectedRevision:0,reason:'test',mutationId:'00000000-0000-4000-8000-000000000025'}, {source:'paired-client'});
  f.r.change({id:first.id,action:'restore',expectedRevision:1,reason:'test',mutationId:'00000000-0000-4000-8000-000000000026'}, {source:'paired-client'});
  const original = f.db.prepare('SELECT * FROM memories WHERE id=?').get(first.id);
  f.db.exec('BEGIN'); assert.equal(f.r.add(input).deduplicated, true); f.db.exec('ROLLBACK');
  assert.deepEqual(f.db.prepare('SELECT * FROM memories WHERE id=?').get(first.id), original);
  assert.equal(f.r.describe(first.id).revision, 2); assert.equal(f.r.history(first.id).length, 2);
});

test('capacity retains invalidated originals and disabling either threshold leaves the other independent', t => {
  const f=fixture(t,{warnMemoryRows:0,warnMemoryBytes:3});
  const first=f.r.add({text:'中',sessionId:'known'});assert.equal(first.capacity.warning,true);
  f.r.change({id:first.id,action:'invalidate',expectedRevision:0,reason:'count retained original',mutationId:'00000000-0000-4000-8000-000000000027'},{source:'paired-client'});
  assert.deepEqual(f.r.capacity(),first.capacity);
  const rowsOnly=new FtsRetriever(f.db,{warnMemoryRows:2,warnMemoryBytes:0});
  assert.equal(rowsOnly.capacity().warning,false);
  assert.equal(rowsOnly.add({text:'文',sessionId:'known'}).capacity.warning,true);
});
