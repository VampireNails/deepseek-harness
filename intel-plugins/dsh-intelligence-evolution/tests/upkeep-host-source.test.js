import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { captureUpkeepSource } from '../src/upkeep-source.js';

// The baseline runs the existing public query against the same real fixture.
// Missing adapter/dependency imports are setup failures, never the RED evidence.
const createUpkeepQuery = process.env.UPKEEP_QUERY_BASELINE === '1'
  ? ctx => ctx.get('sessionQuery')
  : (await import('../src/upkeep-host-source.js')).createUpkeepQuery;

async function dispose(lease) {
  if (typeof lease[Symbol.asyncDispose] === 'function') await lease[Symbol.asyncDispose]();
  else await lease[Symbol.dispose]();
}

async function ownedPersistence(t) {
  const [{ Context }, session, { default: Jsonl }, { default: Query }, { createUserMessage }] = await Promise.all([
    import('@deepseek-ai/cordis'),
    import('@deepseek-ai/dsh-session'),
    import('@deepseek-ai/dsh-session-persistence-jsonl'),
    import('@deepseek-ai/dsh-session-query-sqlite'),
    import('@deepseek-ai/dsh-llm'),
  ]);
  const root = await mkdtemp(join(tmpdir(), 'upkeep-host-source-'));
  const ctx = new Context();
  t.after(async () => {
    try { await ctx.fiber.dispose(); }
    finally { await rm(root, { recursive: true, force: true }); }
  });
  await ctx.plugin(session.default);
  await ctx.plugin(Jsonl, { root, compression: 'none' });
  await ctx.plugin(Query, { path: ':memory:', openAt: 'never' });
  return { ctx, ...session, createUserMessage };
}

const request = (cursors, bootstrap = false) => ({
  cursors, bootstrap, maxBatchMessages: 8, maxBatchChars: 1024,
});

test('cold raw cut excludes synthetic closers and retains the next real human event', async t => {
  const f = await ownedPersistence(t);
  const id = f.SessionId('owned-cold-root');
  const header = { id, version: f.SESSION_FORMAT_VERSION, createdAt: 1000, isSeeded: false };
  const writer = await f.ctx.get('sessionPersistence').create(header);
  t.after(() => writer.close());
  await writer.append([
    { type: 'turn/start', seq: f.SessionSeq(0), time: 1, data: { turn: 1 } },
    { type: 'user/message', seq: f.SessionSeq(1), time: 2, surfaceOp: 'append',
      data: f.createUserMessage({ content: [{ type: 'text', text: 'fixture initial human' }], source: { kind: 'user' } }) },
    { type: 'step/start', seq: f.SessionSeq(2), time: 3, data: { turn: 1, step: 1 } },
  ]);
  await writer.flush();
  assert.equal(f.ctx.sessions.get(id), undefined);
  const prepared = await f.ctx.get('sessionQuery').observeSession(id, { projectionMode: 'none' });
  try {
    assert.equal(prepared.cursor, 4, 'existing cold query adds step/end and turn/end');
    assert.deepEqual(prepared.events.slice(-2).map(e => e.type), ['step/end', 'turn/end']);
  } finally { await dispose(prepared); }

  const query = createUpkeepQuery(f.ctx);
  const bootstrap = await captureUpkeepSource(query, [], request({}, true));
  assert.equal(bootstrap.cursors[id], 2, 'bootstrap must acknowledge only actual persisted events');
  assert.deepEqual(bootstrap.messages, []);
  const next = f.createUserMessage({ content: [{ type: 'text', text: 'fixture next human' }], source: { kind: 'user' } });
  await writer.append([{ type: 'user/message', seq: f.SessionSeq(3), time: 4, data: next, surfaceOp: 'append' }]);
  await writer.flush();
  const captured = await captureUpkeepSource(query, [], request(bootstrap.cursors));
  assert.deepEqual(captured.messages.map(m => ({ seq: m.seq, messageId: m.messageId })), [{ seq: 3, messageId: next.id }]);
  assert.equal(captured.cursors[id], 3);
  assert.equal(f.ctx.sessions.get(id), undefined, 'reading never attaches a cold Session');
});

test('live cut stays exact when the real Session appends before lazy events are accessed', async t => {
  const f = await ownedPersistence(t);
  const live = f.ctx.sessions.create(f.SessionId('owned-live-root'));
  const expected = live.snapshotEvents();
  const lease = await createUpkeepQuery(f.ctx).observeSession(live.id, { projectionMode: 'none' });
  try {
    live.append('user/message', f.createUserMessage({ content: [{ type: 'text', text: 'fixture future human' }], source: { kind: 'user' } }), { surfaceOp: 'append' });
    assert.equal(lease.cursor, expected.at(-1)?.seq ?? -1);
    assert.deepEqual(lease.events, expected);
    assert.equal(lease.header, live.header);
    assert.equal(lease.inheritedEventCount, live.inheritedEventCount);
    assert.ok(Object.isFrozen(lease.events));
  } finally { await dispose(lease); }
});

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

function coldBoundary(overrides = {}) {
  const header = { id: 'boundary-root', version: 4, createdAt: 1000, isSeeded: true };
  const events = [0, 1, 2].map(seq => ({ type: 'session/end-seed', seq, time: seq + 1, data: {} }));
  const state = { closes: 0, opens: [], reads: [] };
  const handle = {
    header, inheritedEventCount: 2,
    async read(...args) { state.reads.push(args); return { eventState: 'detached', events }; },
    async close() { state.closes++; },
    ...overrides,
  };
  const query = {
    async listSessions() { return [{ header }]; },
    async observeSession() { throw new Error('cold prepared query must not be used'); },
  };
  const persistence = { async open(...args) { state.opens.push(args); return handle; } };
  const sessions = { get() { return undefined; } };
  const services = { sessionQuery: query, sessionPersistence: persistence, sessions };
  const ctx = { sessions, get(name) { return services[name]; } };
  return { ctx, query, persistence, handle, header, events, state };
}

test('cold lease uses handle header and inherited count and exposes an immutable raw snapshot', async () => {
  const f = coldBoundary();
  const lease = await createUpkeepQuery(f.ctx).observeSession(f.header.id, { projectionMode: 'none' });
  try {
    assert.equal(f.state.opens[0][0], f.header.id);
    assert.equal(f.state.opens[0][1], 'read');
    assert.equal(lease.inheritedEventCount, 2);
    assert.deepEqual(lease.header, f.header);
    assert.equal(lease.cursor, 2);
    assert.equal(f.state.closes, 0, 'caller owns the handle until lease disposal');
    assert.ok(Object.isFrozen(lease.header));
    assert.ok(Object.isFrozen(lease.events));
    assert.ok(Object.isFrozen(lease.events[0].data));
    assert.throws(() => { lease.cursor = 99; }, TypeError);
    f.events[0].data.changed = true;
    f.events.push({ seq: 3 });
    assert.equal(lease.events.length, 3);
    assert.deepEqual(lease.events[0].data, {});
  } finally { await dispose(lease); }
  assert.equal(f.state.closes, 1);
});

test('cold empty stored snapshot has cursor minus one', async () => {
  const f = coldBoundary({ inheritedEventCount: 0, async read() { return { eventState: 'detached', events: [] }; } });
  const lease = await createUpkeepQuery(f.ctx).observeSession(f.header.id);
  try { assert.equal(lease.cursor, -1); assert.deepEqual(lease.events, []); }
  finally { await dispose(lease); }
});

test('async lease disposal awaits close and repeated disposal closes once', async () => {
  const closeStarted = deferred(), closeDone = deferred();
  let closes = 0;
  const f = coldBoundary({ async close() { closes++; closeStarted.resolve(); await closeDone.promise; } });
  const lease = await createUpkeepQuery(f.ctx).observeSession(f.header.id);
  assert.equal(typeof lease[Symbol.asyncDispose], 'function');
  let disposed = false;
  const disposing = lease[Symbol.asyncDispose]().then(() => { disposed = true; });
  await closeStarted.promise;
  assert.equal(disposed, false);
  closeDone.resolve();
  await disposing;
  await dispose(lease);
  if (typeof lease[Symbol.dispose] === 'function') await lease[Symbol.dispose]();
  assert.equal(closes, 1);
});

test('failed read awaits close before propagating the original failure', async () => {
  const readError = new Error('fixture read failure');
  const closeStarted = deferred(), closeDone = deferred();
  const f = coldBoundary({ async read() { throw readError; }, async close() { closeStarted.resolve(); await closeDone.promise; } });
  let settled = false;
  const observing = createUpkeepQuery(f.ctx).observeSession(f.header.id);
  const rejected = assert.rejects(observing, error => error === readError).then(() => { settled = true; });
  await closeStarted.promise;
  assert.equal(settled, false);
  closeDone.resolve();
  await rejected;
});

test('close failure cannot replace the initiating read failure', async () => {
  const readError = new Error('fixture initiating read failure'), closeError = new Error('fixture secondary close failure');
  let closes = 0;
  const f = coldBoundary({ async read() { throw readError; }, async close() { closes++; throw closeError; } });
  await assert.rejects(createUpkeepQuery(f.ctx).observeSession(f.header.id), error =>
    error === readError || error.cause === readError || (error instanceof AggregateError && error.errors.includes(readError)));
  assert.equal(closes, 1);
});

test('open and listing failures remain observable instead of becoming empty success', async () => {
  const f = coldBoundary();
  const openError = new Error('fixture open failure'), listError = new Error('fixture list failure');
  f.persistence.open = async () => { throw openError; };
  const query = createUpkeepQuery(f.ctx);
  await assert.rejects(query.observeSession(f.header.id), error => error === openError);
  f.query.listSessions = async () => { throw listError; };
  await assert.rejects(query.listSessions(), error => error === listError);
  assert.equal(f.state.closes, 0);
});

test('read-only handle receives cancellation while opening and reading', async () => {
  const f = coldBoundary();
  const controller = new AbortController();
  const lease = await createUpkeepQuery(f.ctx).observeSession(f.header.id, { projectionMode: 'none', signal: controller.signal });
  try {
    assert.equal(f.state.opens[0][2]?.signal, controller.signal);
    assert.equal(f.state.reads[0][2]?.signal, controller.signal);
  } finally { await dispose(lease); }
});

test('configured artifact limit rejects before decode and admits the exact byte boundary', async () => {
  const f = coldBoundary();
  let sizeBytes = 129;
  f.persistence.stat = async () => ({ header: f.header, sizeBytes });
  const query = createUpkeepQuery(f.ctx, { maxSessionBytes: 128 });
  await assert.rejects(query.observeSession(f.header.id), error => error.code === 'UPKEEP_SOURCE_TOO_LARGE');
  assert.equal(f.state.opens.length, 0, 'oversize artifacts must not enter whole-log materialization');
  sizeBytes = 128;
  const lease = await query.observeSession(f.header.id);
  try { assert.equal(lease.cursor, 2); }
  finally { await dispose(lease); }
  assert.equal(f.state.closes, 1);
});
