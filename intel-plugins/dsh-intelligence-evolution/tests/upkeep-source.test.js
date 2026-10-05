import { test } from 'node:test';
import assert from 'node:assert/strict';
import { captureUpkeepSource, inspectUpkeepRun } from '../src/upkeep-source.js';

// Canonical envelopes follow dsh-session/src/types.ts and dsh-llm/src/message.ts.
// These observations represent raw cuts, not cold-read synthetic turn closers.
const user = (text, kind = 'user') => ({ type: 'user/message', surfaceOp: 'append',
  data: { id: 'message-' + text, role: 'user', source: { kind }, content: [{ type: 'text', text }] } });
const other = () => ({ type: 'session/end-seed', data: {} });
const start = (turn = 0) => ({ type: 'turn/start', data: { turn } });
const end = (kind = 'completed', turn = 0) => ({ type: 'turn/end', data: { turn, reason: { kind } } });
const call = (name = 'memory_write', callId = 'call-write', turn = 0) => ({ type: 'tool/call',
  data: { turn, step: 0, callId, name, arguments: '{"text":"anonymous fixture fact"}' } });
function result({ callId = 'call-write', isError = false, memoryWrite, text = 'anonymous result', turn = 0 } = {}) {
  return { type: 'tool/result', surfaceOp: 'append', data: { turn, step: 0,
    message: { id: 'result-' + callId, role: 'tool', source: { kind: 'tool', callId },
      toolCallId: callId, isError, content: [{ type: 'text', text }] },
    ...(memoryWrite === undefined ? {} : { meta: { memoryWrite } }) } };
}
function source(id, entries = [], metadata = {}) {
  const { inheritedEventCount = 0, cursor = entries.length - 1, observedId = id, failure, ...header } = metadata;
  return { header: { version: 4, id, createdAt: 1, cwd: '/same-fixture-cwd', isSeeded: false, ...header },
    observedId, inheritedEventCount, cursor, failure,
    events: entries.map((entry, seq) => ({ time: seq + 1, seq, ...entry })) };
}
function queryFixture(sources, { asyncDispose = false } = {}) {
  const byId = new Map(sources.map(item => [item.header.id, item]));
  const observed = [], released = [], active = new Set();
  return { observed, released, active,
    query: {
      async listSessions() { return sources.map(item => ({ header: { ...item.header } })); },
      async observeSession(id, options) {
        assert.deepEqual(options, { projectionMode: 'none' });
        observed.push(id);
        const item = byId.get(id);
        if (!item) return undefined;
        if (item.failure) throw item.failure;
        active.add(id);
        let disposed = false;
        const dispose = () => {
          assert.equal(disposed, false, 'Each observation lease must be released exactly once');
          disposed = true; active.delete(id); released.push(id);
        };
        const lease = { header: { ...item.header, id: item.observedId }, cursor: item.cursor,
          inheritedEventCount: item.inheritedEventCount, events: structuredClone(item.events) };
        if (asyncDispose) lease[Symbol.asyncDispose] = async () => { await Promise.resolve(); dispose(); };
        else lease[Symbol.dispose] = dispose;
        return lease;
      },
    },
  };
}
const request = (cursors = {}, extras = {}) => ({ cursors, bootstrap: false,
  maxBatchMessages: 50, maxBatchChars: 10000, ...extras });
const row = (sessionId, taskId = 'upkeep', runId = 'fixture-run') => ({ sessionId, taskId, runId, runDate: '2026-10-05' });
const identity = { taskId: 'upkeep', runId: 'fixture-run' };
const messageRecords = report => report.messages.map(({ sessionId, seq, messageId, text }) => ({ sessionId, seq, messageId, text }));
function releasedAll(fixture) {
  assert.equal(fixture.active.size, 0, 'Every retained lease must be released before returning or rejecting');
  assert.equal(fixture.released.length, fixture.observed.length);
}

test('bootstrap records every raw cut without exposing or consuming historical human text as new input', async () => {
  const privateOld = 'private historical fixture content';
  const f = queryFixture([source('z', [user(privateOld)]), source('a', []),
    source('child', [user('old child')], { origin: 'subagent' }), source('automatic', [user('old automatic')])]);
  const input = request({}, { bootstrap: true });
  const before = structuredClone(input);
  const captured = await captureUpkeepSource(f.query, [row('automatic')], input);
  assert.deepEqual(captured.messages, []);
  assert.deepEqual(captured.cursors, { a: -1, automatic: 0, child: 0, z: 0 });
  assert.equal(captured.counts.newMessages, 0);
  assert.equal(JSON.stringify(captured).includes(privateOld), false);
  assert.deepEqual(input, before, 'The caller owns cursor acknowledgement');
  releasedAll(f);
});

test('incremental input includes only original human messages and excludes indexed automation and canonical subagents', async () => {
  const f = queryFixture([source('human', [user('old'), user('new human'), user('plugin injected', 'plugin'),
    user('goal continued', 'goal'), result({ text: 'tool result text' })]),
  source('automatic', [user('cron stdin is still source user')]),
  source('child', [user('child user')], { origin: 'subagent', parentSession: 'human' })]);
  const captured = await captureUpkeepSource(f.query, [row('automatic')], request({ human: 0, automatic: -1, child: -1 }));
  assert.deepEqual(messageRecords(captured), [{ sessionId: 'human', seq: 1, messageId: 'message-new human', text: 'new human' }]);
  assert.equal(captured.cursors.human, 4);
  assert.equal(captured.counts.newMessages, 1);
  assert.ok(captured.counts.filteredSessions >= 2);
  assert.ok(captured.counts.filteredEvents >= 3);
  for (const text of ['new human', 'plugin injected', 'cron stdin is still source user', 'tool result text'])
    assert.equal(JSON.stringify(captured.counts).includes(text), false);
  releasedAll(f);
});

test('an ordinary fork includes its owned boundary event despite shared cwd and parent metadata', async () => {
  const f = queryFixture([source('fork', [user('inherited human'), user('inherited second'), user('owned boundary'), user('owned next')],
    { isSeeded: true, parentSession: 'parent', inheritedEventCount: 2 }),
  source('parent', [user('parent new')])]);
  const captured = await captureUpkeepSource(f.query, [], request({ fork: -1, parent: -1 }));
  assert.deepEqual(captured.messages.map(({ sessionId, seq, text }) => ({ sessionId, seq, text })),
    [{ sessionId: 'fork', seq: 2, text: 'owned boundary' }, { sessionId: 'fork', seq: 3, text: 'owned next' },
      { sessionId: 'parent', seq: 0, text: 'parent new' }]);
  releasedAll(f);
});

test('a newly discovered post-bootstrap session consumes owned user input without replaying its inherited prefix', async () => {
  const f = queryFixture([source('existing', [user('already acknowledged')]),
    source('new-fork', [user('inherited'), other(), user('fresh owned input')],
      { isSeeded: true, parentSession: 'existing', inheritedEventCount: 1 })]);
  const captured = await captureUpkeepSource(f.query, [], request({ existing: 0 }));
  assert.deepEqual(captured.messages.map(({ seq, text }) => ({ seq, text })), [{ seq: 2, text: 'fresh owned input' }]);
  assert.equal(captured.cursors['new-fork'], 2);
  releasedAll(f);
});

test('reusing acknowledged cursors produces no duplicate human messages', async () => {
  const f = queryFixture([source('a', [user('first'), user('second')])]);
  const first = await captureUpkeepSource(f.query, [], request({ a: -1 }));
  const second = await captureUpkeepSource(f.query, [], request(first.cursors));
  assert.deepEqual(first.messages.map(item => item.text), ['first', 'second']);
  assert.deepEqual(second.messages, []);
  assert.equal(second.counts.newMessages, 0);
  assert.equal(second.cursors.a, 1);
  releasedAll(f);
});

test('message backpressure stops before unacknowledged input and resumes every signal in stable ID order', async () => {
  const f = queryFixture([source('z', [user('third')]),
    source('a', [other(), user('first'), user('not a human signal', 'plugin'), user('second'), other()])]);
  const input = request({ a: -1, z: -1 }, { maxBatchMessages: 1 });
  const first = await captureUpkeepSource(f.query, [], input);
  assert.deepEqual(first.messages.map(item => item.text), ['first']);
  assert.equal(first.cursors.a, 2, 'Filtered events before the blocked human event may be acknowledged');
  assert.equal(first.cursors.z, -1, 'A later session must not leap past the stable batch boundary');
  const second = await captureUpkeepSource(f.query, [], request(first.cursors, { maxBatchMessages: 1 }));
  const third = await captureUpkeepSource(f.query, [], request(second.cursors, { maxBatchMessages: 1 }));
  assert.deepEqual([...first.messages, ...second.messages, ...third.messages].map(item => item.text), ['first', 'second', 'third']);
  assert.equal(second.cursors.a, 4);
  assert.equal(third.cursors.z, 0);
  assert.deepEqual(input.cursors, { a: -1, z: -1 });
  releasedAll(f);
});

test('character backpressure preserves complete text and leaves the first excluded human event unacknowledged', async () => {
  const f = queryFixture([source('a', [user('12345'), other(), user('67890'), other()])]);
  const first = await captureUpkeepSource(f.query, [], request({ a: -1 }, { maxBatchChars: 8 }));
  assert.deepEqual(first.messages.map(item => item.text), ['12345']);
  assert.equal(first.cursors.a, 1);
  const second = await captureUpkeepSource(f.query, [], request(first.cursors, { maxBatchChars: 8 }));
  assert.deepEqual(second.messages.map(item => item.text), ['67890']);
  assert.equal(second.cursors.a, 3);
  releasedAll(f);
});

test('one oversized original human message rejects without truncating or consuming its cursor', async () => {
  const f = queryFixture([source('a', [other(), user('complete oversized fixture message')])]);
  const input = request({ a: -1 }, { maxBatchChars: 4 });
  await assert.rejects(() => captureUpkeepSource(f.query, [], input));
  assert.deepEqual(input.cursors, { a: -1 });
  releasedAll(f);
});

test('a later source read failure rejects the whole batch and releases earlier observations', async () => {
  const failure = Error('anonymous fixture source unavailable');
  const f = queryFixture([source('a', [user('would otherwise be captured')]), source('z', [], { failure })]);
  const input = request({ a: -1, z: -1 });
  await assert.rejects(() => captureUpkeepSource(f.query, [], input), error => error === failure);
  assert.deepEqual(input.cursors, { a: -1, z: -1 });
  assert.equal(f.active.size, 0);
  assert.deepEqual(f.released, ['a']);
});

test('a listed source that disappears must fail loudly rather than becoming an acknowledged empty source', async () => {
  const f = queryFixture([source('a', [user('first')])]);
  const originalList = f.query.listSessions;
  f.query.listSessions = async () => [...await originalList(), { header: source('z').header }];
  await assert.rejects(() => captureUpkeepSource(f.query, [], request({ a: -1, z: -1 })));
  assert.equal(f.active.size, 0);
  assert.deepEqual(f.released, ['a']);
});

for (const [name, metadata, previous] of [
  ['identity mismatch', { observedId: 'another-source' }, -1],
  ['cursor rollback', {}, 2],
]) test('capture rejects ' + name + ' and releases the offending lease', async () => {
  const f = queryFixture([source('a', [user('anonymous')], metadata)]);
  await assert.rejects(() => captureUpkeepSource(f.query, [], request({ a: previous })));
  releasedAll(f);
});

test('capture awaits asynchronous lease disposal on both success and oversized-message failure', async () => {
  const f = queryFixture([source('a', [user('anonymous')])], { asyncDispose: true });
  await captureUpkeepSource(f.query, [], request({ a: -1 }));
  releasedAll(f);
  await assert.rejects(() => captureUpkeepSource(f.query, [], request({ a: -1 }, { maxBatchChars: 1 })));
  releasedAll(f);
});

const successfulRun = () => [start(), call(), result({ memoryWrite: { ok: true, id: 17 } }), end()];
test('run inspection resolves only the exact indexed root and canonical successful memory-write metadata', async () => {
  const f = queryFixture([source('root', successfulRun()), source('unrelated', [start(), end('error')]),
    source('child', [start(), end('error')], { origin: 'subagent', parentSession: 'root' })]);
  const report = await inspectUpkeepRun(f.query, [row('unrelated', 'upkeep', 'different-run'), row('child'), row('root')], identity);
  assert.equal(report.complete, true);
  assert.equal(report.failedWrites, 0);
  assert.deepEqual(report.writeIds, [17]);
  assert.equal(JSON.stringify(report).includes('anonymous fixture fact'), false);
  assert.equal(JSON.stringify(report).includes('anonymous result'), false);
  assert.deepEqual(f.observed, ['root']);
  releasedAll(f);
});

test('a completed upkeep turn with no durable facts may succeed without inventing a memory write', async () => {
  const f = queryFixture([source('root', [start(), call('memory_search', 'search'),
    result({ callId: 'search', text: 'no new facts' }), end()])]);
  const report = await inspectUpkeepRun(f.query, [row('root')], identity);
  assert.equal(report.complete, true);
  assert.equal(report.failedWrites, 0);
  assert.deepEqual(report.writeIds, []);
  releasedAll(f);
});

test('two distinct exact-run roots cannot be reported as one completed upkeep run', async () => {
  const f = queryFixture([source('a', successfulRun()), source('b', successfulRun())]);
  const report = await inspectUpkeepRun(f.query, [row('a'), row('b')], identity);
  assert.equal(report.complete, false);
  assert.deepEqual(report.writeIds, []);
  assert.equal(f.active.size, 0);
});

test('absence of exact-run attribution cannot borrow a different task or run completion', async () => {
  const f = queryFixture([source('root', successfulRun())]);
  const report = await inspectUpkeepRun(f.query, [row('root', 'studying'), row('other', 'upkeep', 'another-run')], identity);
  assert.equal(report.complete, false);
  assert.deepEqual(report.writeIds, []);
  assert.deepEqual(f.observed, []);
});

for (const [name, entries] of [
  ['unfinished turn', [start(), call(), result({ memoryWrite: { ok: true, id: 17 } })]],
  ['interrupted synthetic closer', [start(), end('interrupted')]],
  ['last turn still running', [...successfulRun(), start(1)]],
  ['tool failure despite completed turn', [start(), call('memory_search', 'search'), result({ callId: 'search', isError: true }), end()]],
  ['missing tool result', [start(), call(), end()]],
  ['prohibited tool', [start(), call('bash', 'shell'), result({ callId: 'shell' }), end()]],
]) test('run inspection refuses ' + name, async () => {
  const f = queryFixture([source('root', entries)]);
  const report = await inspectUpkeepRun(f.query, [row('root')], identity);
  assert.equal(report.complete, false);
  releasedAll(f);
});

for (const [name, outcome] of [
  ['canonical failed write metadata', { memoryWrite: { ok: false }, text: 'looks successful to a string matcher' }],
  ['missing metadata despite isError false', { text: '已记住 #17: misleading display text' }],
  ['isError true despite optimistic metadata', { isError: true, memoryWrite: { ok: true, id: 17 } }],
]) test('run inspection refuses ' + name + ' and counts the failed write without fabricating IDs', async () => {
  const f = queryFixture([source('root', [start(), call(), result(outcome), end()])]);
  const report = await inspectUpkeepRun(f.query, [row('root')], identity);
  assert.equal(report.complete, false);
  assert.equal(report.failedWrites, 1);
  assert.deepEqual(report.writeIds, []);
  assert.equal(JSON.stringify(report).includes(outcome.text ?? 'anonymous result'), false);
  releasedAll(f);
});

test('write metadata must pair with its memory_write call rather than another tool result', async () => {
  const f = queryFixture([source('root', [start(), call(), call('memory_search', 'search'),
    result({ callId: 'search', memoryWrite: { ok: true, id: 999 } }), result(), end()])]);
  const report = await inspectUpkeepRun(f.query, [row('root')], identity);
  assert.equal(report.complete, false);
  assert.equal(report.failedWrites, 1);
  assert.deepEqual(report.writeIds, []);
  releasedAll(f);
});

test('run inspection propagates a missing indexed source and releases an identity-mismatched source', async () => {
  const missing = queryFixture([]);
  await assert.rejects(() => inspectUpkeepRun(missing.query, [row('missing')], identity));
  assert.equal(missing.active.size, 0);
  const mismatch = queryFixture([source('root', successfulRun(), { observedId: 'different-root' })]);
  await assert.rejects(() => inspectUpkeepRun(mismatch.query, [row('root')], identity));
  releasedAll(mismatch);
});

test('run inspection awaits asynchronous lease release before publishing a completed receipt', async () => {
  const f = queryFixture([source('root', successfulRun())], { asyncDispose: true });
  const report = await inspectUpkeepRun(f.query, [row('root')], identity);
  assert.equal(report.complete, true);
  releasedAll(f);
});

test('capture and inspection await a promise-returning dispose compatibility method and reject its cleanup failure', async () => {
  for (const mode of ['capture', 'inspect']) {
    const f = queryFixture([source('root', successfulRun())]);
    const observe = f.query.observeSession;
    const failure = Error('anonymous fixture asynchronous lease cleanup failed');
    // Mark the rejection as observed independently so the assertion detects the
    // missing await, rather than an unrelated test-runner unhandled rejection.
    const rejectedCleanup = Promise.reject(failure);
    rejectedCleanup.catch(() => {});
    f.query.observeSession = async (...args) => {
      const lease = await observe(...args), release = lease[Symbol.dispose];
      lease[Symbol.dispose] = () => { release(); return rejectedCleanup; };
      return lease;
    };
    const invoke = mode === 'capture'
      ? () => captureUpkeepSource(f.query, [], request({ root: -1 }))
      : () => inspectUpkeepRun(f.query, [row('root')], identity);
    await assert.rejects(invoke, error => error === failure,
      'A successful receipt cannot escape before the retained resource finishes cleanup');
    releasedAll(f);
  }
});
