import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, appendFile, rm, stat, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, delimiter } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { DatabaseSync } from 'node:sqlite';
import { createHash } from 'node:crypto';

const cliPath = fileURLToPath(new URL('../../../intel/task-runner/cli.mjs', import.meta.url));
const selfPath = fileURLToPath(import.meta.url);
const freshText = 'private connected fixture human prefers the color blue';
const historicalText = 'private connected fixture historical input must never enter the batch';
const factText = '用户喜欢蓝色，来自隔离连接测试的明确真人输入。';
const envKeys = ['DSH_HOME', 'INTEL_TASK_ID', 'INTEL_RUN_ID', 'INTEL_RUN_DATE'];
const shQuote = text => "'" + text.replaceAll("'", "'\\''") + "'";

async function officialRuntime() {
  const [cordis, session, jsonl, query, tools, systemPrompt, agents, llm] = await Promise.all([
    import('@deepseek-ai/cordis'), import('@deepseek-ai/dsh-session'),
    import('@deepseek-ai/dsh-session-persistence-jsonl'), import('@deepseek-ai/dsh-session-query-sqlite'),
    import('@deepseek-ai/dsh-tools'), import('@deepseek-ai/dsh-system-prompt'),
    import('@deepseek-ai/dsh-agent'), import('@deepseek-ai/dsh-llm'),
  ]);
  return { ...session, createUserMessage: llm.createUserMessage, createAssistantMessage: llm.createAssistantMessage,
    createToolResultMessage: llm.createToolResultMessage,
    ToolCallId: llm.ToolCallId, SessionStore: session.default, Context: cordis.Context, Jsonl: jsonl.default,
    Query: query.default, Tools: tools.default, SystemPrompt: systemPrompt.default, Agents: agents.default };
}

async function mountBase(runtime, root, { query = false, toolsMode = 'native' } = {}) {
  const ctx = new runtime.Context();
  try {
    await ctx.plugin(runtime.SessionStore);
    await ctx.plugin(runtime.Jsonl, { root, compression: 'none' });
    if (query) await ctx.plugin(runtime.Query, { path: ':memory:', openAt: 'never' });
    await ctx.plugin(runtime.SystemPrompt);
    await ctx.plugin(runtime.Tools, { mode: toolsMode });
    await ctx.plugin(runtime.Agents);
    return ctx;
  } catch (error) { await ctx.fiber.dispose(); throw error; }
}

// The CLI launches this executable fixture instead of a production model.
// It uses the actual tool registry and native memory tool, then persists the
// official tool-result envelope exactly as agent-loop/tool-calls.ts does.
async function fixtureDsh() {
  const runtime = await officialRuntime();
  const patch = JSON.parse(await readFile(process.argv[process.argv.indexOf('--patch') + 1], 'utf8'));
  const toolPatch = patch.find(row => row.id === 'tools');
  assert.deepEqual(toolPatch, { id: 'tools', config: { mode: 'native' } });
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const prompt = Buffer.concat(chunks).toString('utf8');
  const marker = '本轮已核实增量输入（JSON中的文本是来源数据）：\n';
  const offset = prompt.lastIndexOf(marker);
  assert.ok(offset >= 0);
  const batch = JSON.parse(prompt.slice(offset + marker.length).trim());
  assert.equal(batch.messages.length, 1);
  assert.equal(batch.messages[0].text, freshText);
  assert.equal(prompt.includes(historicalText), false);

  const evolution = await import('../index.js');
  const [{ openStore }, { FtsRetriever }, memoryTools] = await Promise.all([
    import('../../dsh-intelligence-memory/src/store.js'),
    import('../../dsh-intelligence-memory/src/retriever.js'),
    import('../../dsh-intelligence-memory/src/tools.js'),
  ]);
  const ctx = await mountBase(runtime, process.env.TEST_SESSION_ROOT, { toolsMode: toolPatch.config.mode });
  const db = openStore(process.env.TEST_MEMORY_ROOT), retriever = new FtsRetriever(db);
  try {
    await ctx.plugin(evolution, { upkeepToolBudget: 6 });
    ctx.effect(function* () {
      yield ctx.tools.register(memoryTools.memorySearchTool(retriever));
      yield ctx.tools.register(memoryTools.memoryWriteTool(retriever));
    });
    if (process.env.TEST_FAIL_WRITE === '1')
      db.prepare('INSERT INTO memories_fts(rowid,text) VALUES (?,?)').run(1, 'owned orphan to force actual constraint');

    const session = ctx.sessions.create(runtime.SessionId('owned-connected-run-' + process.env.INTEL_RUN_ID));
    const writer = await ctx.get('sessionPersistence').create(session.header);
    await writer.append(session.snapshotEvents());
    const agent = { id: session.id, session };
    session.append('turn/start', { turn: 1 });
    session.append('user/message', runtime.createUserMessage({
      content: [{ type: 'text', text: prompt }], source: { kind: 'user' },
    }), { surfaceOp: 'append' });
    session.append('step/start', { turn: 1, step: 1 });
    const planned = [['memory_search', { query: '蓝色' }], ['memory_write', { text: factText, kind: 'fact' }]];
    session.append('assistant/message', { turn: 1, step: 1, stream: [],
      message: runtime.createAssistantMessage({ source: { provider: 'fixture-no-network', model: 'fixture-no-model' },
        content: planned.map(([name, args]) => ({ type: 'tool-call', id: runtime.ToolCallId('owned-' + name), name, arguments: JSON.stringify(args) })),
      }),
    }, { surfaceOp: 'append' });
    let writeResult;
    for (const [name, args] of planned) {
      const callId = runtime.ToolCallId('owned-' + name);
      const call = session.append('tool/call', { turn: 1, step: 1, callId, name, arguments: JSON.stringify(args) });
      const result = await ctx.tools.execute({ callId, name, arguments: args, agent, signal: new AbortController().signal });
      session.append('tool/result', { turn: 1, step: 1,
        message: runtime.createToolResultMessage({ callId, content: result.content, isError: result.isError }),
        ...(result.error?.info ? { error: result.error.info } : {}),
        ...(result.meta !== undefined ? { meta: result.meta } : {}),
      }, { surfaceOp: 'append', sourceEventSeqs: [call.seq] });
      if (name === 'memory_write') writeResult = result;
    }
    session.append('step/end', { turn: 1, step: 1 });
    session.append('turn/end', { turn: 1, reason: { kind: 'completed' } });
    assert.equal(await ctx.sessions.flush(session), true);
    await appendFile(process.env.TEST_DISPATCH_FILE, JSON.stringify({
      taskId: process.env.INTEL_TASK_ID, runId: process.env.INTEL_RUN_ID,
      sessionId: session.id, messages: batch.messages, writeMeta: writeResult.meta,
      genericIsError: writeResult.isError,
    }) + '\n', { mode: 0o600 });
  } finally { try { await ctx.fiber.dispose(); } finally { retriever.close(); } }
}

async function fixture(t, failWrite = false) {
  const runtime = await officialRuntime();
  const previous = new Map(envKeys.map(key => [key, process.env[key]]));
  const directory = await mkdtemp(join(tmpdir(), 'upkeep-connected-'));
  const home = join(directory, 'home'), root = join(directory, 'sessions'), memory = join(directory, 'memory');
  const socketPath = join(directory, 'source.sock'), bin = join(directory, 'bin'), dispatch = join(directory, 'dispatch.jsonl');
  const children = new Set();
  let ctx, writer, sourceMounted = false;
  t.after(async () => {
    for (const child of children) { try { process.kill(-child.process.pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; } }
    await Promise.allSettled([...children].map(child => child.closed));
    try {
      if (ctx) await ctx.fiber.dispose();
      if (sourceMounted) await assert.rejects(() => stat(socketPath), { code: 'ENOENT' });
      assert.deepEqual((await readdir(directory)).filter(name => name.startsWith('.u-')), [], 'Disposal must await random socket bind-path cleanup');
      if (writer) await assert.rejects(() => writer.read(), { name: 'SessionHandleClosedError' }, 'Provider teardown must await owned writer close');
    }
    finally {
      for (const [key, value] of previous) { if (value === undefined) delete process.env[key]; else process.env[key] = value; }
      await rm(directory, { recursive: true, force: true });
    }
  });
  for (const key of envKeys) delete process.env[key];
  process.env.DSH_HOME = home;
  await mkdir(home); await mkdir(bin);
  const evolution = await import('../index.js');
  ctx = await mountBase(runtime, root, { query: true });
  // This is the actual evolution plugin effect, not a manually started server.
  await ctx.plugin(evolution, { upkeepSourceSocket: socketPath, upkeepSourceTimeoutMs: 5000, upkeepMaxSessionBytes: 1048576 });
  sourceMounted = true;
  assert.equal((await stat(socketPath)).mode & 0o777, 0o600);

  const id = runtime.SessionId('owned-connected-human');
  writer = await ctx.get('sessionPersistence').create({ id,
    version: runtime.SESSION_FORMAT_VERSION, createdAt: 1, isSeeded: false });
  await writer.append([
    { type: 'turn/start', seq: runtime.SessionSeq(0), time: 1, data: { turn: 1 } },
    { type: 'user/message', seq: runtime.SessionSeq(1), time: 2, surfaceOp: 'append',
      data: runtime.createUserMessage({ content: [{ type: 'text', text: historicalText }], source: { kind: 'user' } }) },
    { type: 'step/start', seq: runtime.SessionSeq(2), time: 3, data: { turn: 1, step: 1 } },
  ]);
  await writer.flush();
  await writeFile(join(bin, 'dsh'), '#!/bin/sh\nexec ' + shQuote(process.execPath) + ' ' + shQuote(selfPath) + ' --fixture-dsh "$@"\n', { mode: 0o700 });
  const router = join(directory, 'routes.json');
  await writeFile(router, JSON.stringify({ timeoutMs: 10000, defaultRoute: 'fixture',
    routes: { fixture: { provider: 'fixture-no-network', model: 'fixture-no-model' } },
    upkeep: { socketPath, sourceTimeoutMs: 5000, initialBackoffMs: 100, maxBackoffMs: 400,
      maxBatchMessages: 3, maxBatchChars: 1024 } }), { mode: 0o600 });
  const env = { PATH: bin + delimiter + process.env.PATH, DSH_HOME: home, INTEL_ROUTER_CONFIG: router,
    DSH_TOOLS_MODE: 'ptc', TEST_SESSION_ROOT: root, TEST_MEMORY_ROOT: memory,
    TEST_DISPATCH_FILE: dispatch, TEST_FAIL_WRITE: failWrite ? '1' : '0' };
  const invoke = (taskId = 'memory_upkeep') => new Promise((resolve, reject) => {
    const childProcess = spawn(process.execPath, [cliPath, taskId], { env, detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '', stderr = '', timer;
    const child = { process: childProcess, closed: null };
    child.closed = new Promise(done => childProcess.once('close', done));
    children.add(child);
    childProcess.stdout.on('data', chunk => { stdout += chunk; });
    childProcess.stderr.on('data', chunk => { stderr += chunk; });
    timer = setTimeout(() => { try { process.kill(-childProcess.pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') reject(error); } }, 20000);
    childProcess.once('error', reject);
    childProcess.once('close', (code, signal) => { clearTimeout(timer); children.delete(child); resolve({ code, signal, stdout, stderr }); });
  });
  const statePath = join(home, 'intel-joblog', 'upkeep', 'state.json');
  return { runtime, ctx, home, memory, socketPath, dispatch, id, writer, invoke,
    readState: async () => JSON.parse(await readFile(statePath, 'utf8')),
    async addFreshInput() {
      const state = JSON.parse(await readFile(statePath, 'utf8'));
      await writeFile(statePath, JSON.stringify({ ...state, nextDueAtMs: 0 }), { mode: 0o600 });
      const message = runtime.createUserMessage({ content: [{ type: 'text', text: freshText }], source: { kind: 'user' } });
      await writer.append([{ type: 'user/message', seq: runtime.SessionSeq(3), time: 4, data: message, surfaceOp: 'append' }]);
      await writer.flush(); return message;
    },
  };
}

async function bootstrap(f) {
  const first = await f.invoke();
  assert.equal(first.code, 0, first.stderr);
  assert.equal(first.signal, null);
  assert.equal((await f.readState()).cursors[f.id], 2, 'Checkpoint must use raw persisted seq, never synthetic interrupted closers');
  await assert.rejects(() => stat(f.dispatch), { code: 'ENOENT' }, 'Bootstrap must not invoke the executable fixture');
  assert.equal(f.ctx.sessions.get(f.id), undefined, 'Cold sources must never become live agents');
}

function inspectDatabase(path, read) {
  const db = new DatabaseSync(path, { readOnly: true });
  try { return read(db); } finally { db.close(); }
}

async function assertPersistedTaskInput(f, dispatch) {
  const handle = await f.ctx.get('sessionPersistence').open(dispatch.sessionId, 'read');
  try {
    const { events } = await handle.read();
    const inputs = events.filter(event => event.type === 'user/message' && event.data.source?.kind === 'user');
    assert.equal(inputs.length, 1, 'The actual persisted runner log must reconstruct its one model-visible input');
    const prefix = await readFile(new URL('./expected/upkeep-task-input.txt', import.meta.url), 'utf8');
    const batchHash = createHash('sha256').update(JSON.stringify(dispatch.messages)).digest('hex');
    const expected = prefix + JSON.stringify({ batchHash, messages: dispatch.messages }) + '\n';
    assert.deepEqual(inputs[0].data.content, [{ type: 'text', text: expected }]);
    assert.deepEqual(events.filter(event => event.type === 'tool/call').map(event => event.data.name), ['memory_search', 'memory_write']);
    const result = events.find(event => event.type === 'tool/result' && event.data.message.toolCallId === 'owned-memory_write');
    assert.deepEqual(result.data.meta, dispatch.writeMeta, 'Canonical metadata must survive the real persistence envelope');
  } finally { await handle.close(); }
}

if (process.argv[2] === '--fixture-dsh') {
  await fixtureDsh();
} else {
  test('actual plugin mount rejects source setup failure and preserves the occupied canonical path',
    { skip: process.platform !== 'linux', timeout: 20000 }, async t => {
      const runtime = await officialRuntime();
      const previous = new Map(envKeys.map(key => [key, process.env[key]]));
      const directory = await mkdtemp(join(tmpdir(), 'upkeep-mount-'));
      const socketPath = join(directory, 'source.sock');
      let ctx;
      t.after(async () => {
        try { if (ctx) await ctx.fiber.dispose(); }
        finally {
          for (const [key, value] of previous) { if (value === undefined) delete process.env[key]; else process.env[key] = value; }
          await rm(directory, { recursive: true, force: true });
        }
      });
      for (const key of envKeys) delete process.env[key];
      process.env.DSH_HOME = join(directory, 'home');
      await writeFile(socketPath, 'owned occupied source path', { mode: 0o600 });
      const original = await stat(socketPath);
      ctx = await mountBase(runtime, join(directory, 'sessions'), { query: true });
      const evolution = await import('../index.js');
      await assert.rejects(async () => { await ctx.plugin(evolution, { upkeepSourceSocket: socketPath, upkeepSourceTimeoutMs: 1000 }); },
        error => error.code === 'UPKEEP_SOCKET_UNAVAILABLE' && error.message === 'UPKEEP_SOCKET_UNAVAILABLE');
      assert.equal(ctx.get('evolution'), undefined, 'Failed startup cannot publish a healthy evolution provider');
      assert.equal(await readFile(socketPath, 'utf8'), 'owned occupied source path');
      const current = await stat(socketPath);
      assert.equal(current.ino, original.ino);
      assert.equal(current.dev, original.dev);
      assert.deepEqual((await readdir(directory)).filter(name => name.startsWith('.u-')), []);
    });

  test('real plugin socket and CLI consume raw human input then verify actual persisted native memory metadata',
    { skip: process.platform !== 'linux', timeout: 45000 }, async t => {
      const f = await fixture(t);
      await bootstrap(f);
      const message = await f.addFreshInput();
      const processed = await f.invoke();
      assert.equal(processed.code, 0, processed.stderr);
      assert.equal(processed.signal, null);
      const dispatches = (await readFile(f.dispatch, 'utf8')).trim().split('\n').map(line => JSON.parse(line));
      assert.equal(dispatches.length, 1);
      const dispatch = dispatches[0];
      await assertPersistedTaskInput(f, dispatch);
      assert.deepEqual(dispatch.messages, [{ sessionId: f.id, seq: 3, messageId: message.id, text: freshText }]);
      assert.equal(dispatch.genericIsError, false);
      assert.equal(dispatch.writeMeta.memoryWrite.ok, true);
      const writeId = dispatch.writeMeta.memoryWrite.id;
      const memory = inspectDatabase(join(f.memory, 'memory.db'), db => db.prepare('SELECT * FROM memories WHERE id=?').get(writeId));
      assert.equal(memory.text, factText);
      assert.equal(memory.kind, 'fact');
      const state = await f.readState();
      assert.equal(state.cursors[f.id], 3);
      assert.equal(state.pending ?? null, null);
      const { requestUpkeep } = await import('../src/upkeep-socket.js');
      const report = await requestUpkeep(f.socketPath, { version: 1, operation: 'inspect',
        identity: { taskId: dispatch.taskId, runId: dispatch.runId } }, 5000);
      assert.deepEqual(report, { complete: true, failedWrites: 0, writeIds: [writeId] });
      assert.equal(f.ctx.sessions.get(dispatch.sessionId), undefined, 'Inspection must stay on raw persisted runner events');
      const quiet = await f.invoke('evolve_upkeep');
      assert.equal(quiet.code, 0, quiet.stderr);
      assert.equal((await readFile(f.dispatch, 'utf8')).trim().split('\n').length, 1, 'Authoritative runner stdin must not reenter the human batch');
      const audit = await readFile(join(f.home, 'intel-joblog', 'route-runs.jsonl'), 'utf8');
      for (const marker of [historicalText, freshText, f.id, message.id, dispatch.sessionId]) assert.equal(audit.includes(marker), false);
    });

  test('real failed SQLite memory write persists failure metadata and cannot acknowledge its source batch or replay effects',
    { skip: process.platform !== 'linux', timeout: 45000 }, async t => {
      const f = await fixture(t, true);
      await bootstrap(f);
      await f.addFreshInput();
      const processed = await f.invoke();
      assert.equal(processed.code, 1);
      assert.match(processed.stderr, /UPKEEP_RUN_UNVERIFIED/);
      const dispatch = JSON.parse((await readFile(f.dispatch, 'utf8')).trim());
      await assertPersistedTaskInput(f, dispatch);
      assert.equal(dispatch.genericIsError, false, 'The actual tool body returns an ok:false canonical value');
      assert.deepEqual(dispatch.writeMeta, { memoryWrite: { ok: false } });
      assert.equal(inspectDatabase(join(f.memory, 'memory.db'), db => db.prepare('SELECT count(*) AS n FROM memories').get().n), 0);
      const state = await f.readState();
      assert.equal(state.cursors[f.id], 2);
      assert.equal(state.pending.taskId, 'memory_upkeep');
      assert.equal(state.pending.runId, dispatch.runId);
      assert.equal(state.pending.proposedCursors[f.id], 3);
      const { requestUpkeep } = await import('../src/upkeep-socket.js');
      assert.deepEqual(await requestUpkeep(f.socketPath, { version: 1, operation: 'inspect',
        identity: { taskId: dispatch.taskId, runId: dispatch.runId } }, 5000), { complete: false, failedWrites: 1, writeIds: [] });
      const retry = await f.invoke('evolve_upkeep');
      assert.equal(retry.code, 1);
      assert.match(retry.stderr, /UPKEEP_PREVIOUS_RUN_UNRESOLVED/);
      assert.deepEqual(await f.readState(), state);
      assert.equal((await readFile(f.dispatch, 'utf8')).trim().split('\n').length, 1);
    });
}
