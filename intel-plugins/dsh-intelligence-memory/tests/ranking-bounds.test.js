import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { openStore } from '../src/store.js';
import { FtsRetriever } from '../src/retriever.js';

function fixture(t, options = {}) {
  const directory = mkdtempSync(join(tmpdir(), 'memory-ranking-bounds-'));
  let db;
  t.after(() => {
    try { db?.close(); }
    finally { rmSync(directory, { recursive: true, force: true }); }
  });
  db = openStore(directory);
  return new FtsRetriever(db, options);
}

for (const key of ['maxCandidates', 'maxCandidateBytes']) {
  test(`${key} accepts positive safe integers and rejects invalid configured bounds`, t => {
    for (const value of [1, 2, Number.MAX_SAFE_INTEGER]) fixture(t, { [key]: value });
    for (const value of [0, -1, 1.5, Infinity, NaN, Number.MAX_SAFE_INTEGER + 1, '2']) {
      assert.throws(() => fixture(t, { [key]: value }), /positive safe integer/);
    }
  });
}

for (const key of ['minimumScoreRatio', 'duplicatePatternPenalty']) {
  test(`${key} accepts finite ratios including both endpoints and rejects invalid configuration`, t => {
    for (const value of [0, 0.5, 1]) fixture(t, { [key]: value });
    for (const value of [-0.01, 1.01, Infinity, -Infinity, NaN, '0.5', null, undefined]) {
      assert.throws(() => fixture(t, { [key]: value }), /finite ratio/);
    }
  });
}

test('a strict minimum score ratio removes weak topic matches while retaining the complete subject', t => {
  for (const minimumScoreRatio of [0, 1]) {
    const memory = fixture(t, { synonymGroups: [], minimumScoreRatio });
    const strongId = memory.add({ text: 'aurora compass bearing calibration manual', kind: 'note' }).id;
    const weakId = memory.add({ text: 'aurora brochure', kind: 'note' }).id;
    const ids = memory.search('aurora compass bearing calibration manual', 5).map(hit => hit.id);
    assert.equal(ids[0], strongId);
    if (minimumScoreRatio === 0) assert.ok(ids.includes(weakId), 'A zero cutoff preserves weak literal matches');
    else assert.deepEqual(ids, [strongId], 'A maximum cutoff preserves only equally strongest matches');
  }
});

test('a duplicate-pattern penalty promotes a distinct relevant result over a repeated complete match', t => {
  for (const duplicatePatternPenalty of [0, 1]) {
    const memory = fixture(t, { synonymGroups: [], minimumScoreRatio: 0, duplicatePatternPenalty });
    const repeatedIds = new Set([
      memory.add({ text: 'aurora compass bearing calibration', kind: 'note' }).id,
      memory.add({ text: 'aurora compass bearing calibration', kind: 'note' }).id,
    ]);
    const distinctId = memory.add({ text: 'aurora compass notes', kind: 'note' }).id;
    const ids = memory.search('aurora compass bearing calibration', 2).map(hit => hit.id);
    assert.ok(repeatedIds.has(ids[0]), 'The first complete match remains first');
    if (duplicatePatternPenalty === 0) assert.ok(repeatedIds.has(ids[1]), 'Without a penalty both complete matches retain priority');
    else assert.equal(ids[1], distinctId, 'The configured penalty gives a distinct relevant result the second slot');
  }
});

test('custom synonym expansion counts toward the query-term bound before either SQL candidate path', t => {
  const options = { synonymGroups: [['光学', '棱镜']] };
  const bounded = fixture(t, { ...options, maxQueryTerms: 2 });
  bounded.add({ text: '棱镜操作规范', kind: 'note' });
  const rejectSql = { all() { assert.fail('Expanded-term overflow must be rejected before SQL'); } };
  bounded.searchStmt = rejectSql;
  bounded.preferenceStmt = rejectSql;
  assert.throws(() => bounded.search('光学', 3), error => error.code === 'MEMORY_QUERY_TOO_LARGE');

  const boundary = fixture(t, { ...options, maxQueryTerms: 4 });
  const { id } = boundary.add({ text: '棱镜操作规范', kind: 'note' });
  assert.deepEqual(boundary.search('光学').map(hit => hit.id), [id]);
  assert.deepEqual(boundary.search('光学光学').map(hit => hit.id), [id], 'Repeated input and aliases must not consume distinct-term slots twice');
});

test('synonym configuration accepts empty groups and its maximum group, term and literal-length bounds', t => {
  fixture(t, { synonymGroups: [] });
  const maximum = Array.from({ length: 16 }, (_, group) =>
    Array.from({ length: 16 }, (_, term) => `alias${group}term${term}`));
  fixture(t, { synonymGroups: maximum });
  fixture(t, { synonymGroups: [['甲', '乙'.repeat(32)], ['star map 7', 'celestial atlas']] });
});

test('synonym configuration rejects oversized and malformed literal groups at construction', t => {
  const invalid = [
    '光学', {},
    Array.from({ length: 17 }, (_, index) => [`term${index}`, `alias${index}`]),
    ['光学', '棱镜'], [[]], [['光学']],
    [Array.from({ length: 17 }, (_, index) => `term${index}`)],
    [['光学', '']], [['光学', '   ']], [['光学', '棱'.repeat(33)]],
    [['光学', 7]], [['光学', null]], [['光学', 'café']],
  ];
  for (const synonymGroups of invalid) {
    assert.throws(() => fixture(t, { synonymGroups }), /synonymGroups/);
  }
});

test('synonym configuration rejects query-operator syntax while permitting literal operator words', t => {
  for (const injected of ['" OR "', 'prism*', 'NEAR(prism)', 'text:prism', 'prism -map', 'prism\nmap']) {
    assert.throws(() => fixture(t, { synonymGroups: [['光学', injected]] }), /synonymGroups/);
  }
  const memory = fixture(t, { synonymGroups: [['OR', '逻辑或']] });
  const { id } = memory.add({ text: '逻辑或门电路的输出表', kind: 'note' });
  assert.deepEqual(memory.search('OR').map(hit => hit.id), [id]);
});

test('custom English phrase aliases match case insensitively without matching a longer word', t => {
  const memory = fixture(t, { synonymGroups: [['star map', 'celestial atlas']] });
  const { id } = memory.add({ text: 'Celestial Atlas reference pages', kind: 'note' });
  assert.deepEqual(memory.search('please show STAR MAP').map(hit => hit.id), [id]);
  assert.deepEqual(memory.search('star mapper'), [], 'An alias phrase must not match the beginning of mapper');
  assert.deepEqual(memory.search('restart map'), [], 'An alias phrase must not match the end of restart');
});

test('configured named preference subjects retain personal statements before the candidate cap', t => {
  const memory = fixture(t, { preferenceSubjects: ['我', '用户', 'Rowan'], maxCandidates: 3 });
  for (let i = 0; i < 160; i++) memory.add({ text: `发布r${i}偏好设置校验完成，希望字段通过检查。` });
  const id = memory.add({ text: 'Rowan 偏好清晰解释实验的理由。' }).id;
  assert.deepEqual(memory.search('用户偏好').map(hit => hit.id), [id]);
  assert.deepEqual(memory.search('实验理由').map(hit => hit.id), [id]);
});

test('preference subject aliases reject malformed configuration without guessing identity', t => {
  for (const preferenceSubjects of [[], '用户', [''], ['  '], ['a'.repeat(33)], ['x*'], [2], Array.from({ length: 17 }, () => 'person')]) {
    assert.throws(() => fixture(t, { preferenceSubjects }), /preferenceSubjects/);
  }
});

test('all sixteen supported distinct preference subjects work before the candidate cap', t => {
  const preferenceSubjects = Array.from({ length: 16 }, (_, i) => 'Person' + i);
  const memory = fixture(t, { preferenceSubjects, maxCandidates: 3 });
  for (let i = 0; i < 160; i++) memory.add({ text: `发布r${i}偏好设置校验完成，希望字段通过检查。` });
  const id = memory.add({ text: 'Person15 偏好清晰解释实验的理由。' }).id;
  assert.deepEqual(memory.search('用户偏好').map(hit => hit.id), [id]);
});

const byteBudgetTexts = ['玉衡 coffee 校准表', '玉衡 coffee 记录簿'];
const byteBudget = byteBudgetTexts.reduce((sum, text) => sum + Buffer.byteLength(text, 'utf8'), 0);

test('candidate byte budget accepts the exact total UTF-8 boundary for multiple originals', t => {
  const memory = fixture(t, { synonymGroups: [], maxCandidates: 2, maxCandidateBytes: byteBudget });
  const ids = byteBudgetTexts.map(text => memory.add({ text, kind: 'note' }).id);
  const hits = memory.search('玉衡 coffee', 5);
  assert.equal(hits.length, 2);
  assert.deepEqual(new Set(hits.map(hit => hit.id)), new Set(ids));
  for (const hit of hits) assert.equal(hit.text, byteBudgetTexts[ids.indexOf(hit.id)]);
});

test('total candidate UTF-8 byte overflow rejects before fetching any original text', t => {
  const memory = fixture(t, { synonymGroups: [], maxCandidates: 2, maxCandidateBytes: byteBudget - 1 });
  for (const text of byteBudgetTexts) memory.add({ text, kind: 'note' });
  memory.getStmt = { get() { assert.fail('An excessive candidate set must be rejected before text fetch'); } };
  assert.throws(() => memory.search('玉衡 coffee', 1), error => error.code === 'MEMORY_RECALL_TOO_LARGE');
});

for (const personal of [false, true]) {
  test(`maxCandidates bounds one total SQL window and text-fetch count (${personal ? 'preference' : 'factual'} query)`, t => {
    const memory = fixture(t, { synonymGroups: [], maxCandidates: 2 });
    const inserted = new Set();
    for (const text of ['我偏好opal笔记', '我喜欢opal手册', '我希望opal目录', '我偏好opal文档', '我喜欢opal索引']) {
      inserted.add(memory.add({ text, kind: 'note' }).id);
    }
    let windows = 0, fetched = 0;
    for (const key of ['searchStmt', 'preferenceStmt']) {
      const original = memory[key];
      memory[key] = { all(...args) {
        windows++;
        assert.equal(args.at(-1), 2, 'The SQL window must use the configured candidate bound');
        const candidates = original.all(...args);
        assert.ok(candidates.length <= 2);
        return candidates;
      } };
    }
    const originalGet = memory.getStmt;
    memory.getStmt = { get(...args) { fetched++; return originalGet.get(...args); } };
    const hits = memory.search(personal ? '我的个人偏好opal' : 'opal', 5);
    assert.equal(windows, 1, 'Search must not issue extra windows to evade the total candidate cap');
    assert.equal(fetched, 2);
    assert.equal(hits.length, 2);
    assert.ok(hits.every(hit => inserted.has(hit.id)));
  });
}

test('bounded factual recall excludes soft-invalidated originals without deleting them or spending their byte budget', t => {
  const activeText = '榆林展馆桂花拿铁咖啡柜台周末下午营业。';
  const memory = fixture(t, { maxCandidates: 1, maxCandidateBytes: Buffer.byteLength(activeText, 'utf8') });
  const oldText = `榆林展馆桂花拿铁咖啡柜台旧营业时间。${'旧档案说明。'.repeat(40)}`;
  const oldId = memory.add({ text: oldText, kind: 'note' }).id;
  const activeId = memory.add({ text: activeText, kind: 'note' }).id;
  memory.change({ id: oldId, action: 'invalidate', expectedRevision: 0, reason: 'Synthetic schedule correction', mutationId: randomUUID() }, { source: 'paired-client' });
  assert.deepEqual(memory.search('我希望了解桂花拿铁咖啡柜台的营业时间', 3).map(hit => hit.id), [activeId]);
  assert.equal(memory.get(oldId), null);
  assert.equal(memory.describe(oldId).text, oldText);
  assert.equal(memory.describe(oldId).active, false);
});
