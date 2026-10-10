import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, readdir, rm, access } from 'node:fs/promises';
import { dirname, join, delimiter } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { spawn, spawnSync } from 'node:child_process';
import { DatabaseSync } from 'node:sqlite';
import { FeedStore } from '../../../intel-plugins/dsh-intelligence-feed/src/feed.js';
import { startUpkeepServer } from '../../../intel-plugins/dsh-intelligence-evolution/src/upkeep-socket.js';

const linux = { skip: process.platform !== 'linux' };
const cli = fileURLToPath(new URL('../cli.mjs', import.meta.url));
const feedPlugin = new URL('../../../intel-plugins/dsh-intelligence-feed/index.js', import.meta.url).href;
const memoryStore = new URL('../../../intel-plugins/dsh-intelligence-memory/src/store.js', import.meta.url).href;
const quoteShell = value => "'" + value.replaceAll("'", "'\\''") + "'";

async function fixture(t, mode, taskId = 'evolve_dreaming', visibleOutput = 'required') {
  const directory = await mkdtemp(join(tmpdir(), 'intel-cli-publication-'));
  const ownedChildren = new Set();
  const barrierReady = join(directory, 'first-execution-ready.json');
  const barrierRelease = join(directory, 'release');
  const lockAttemptsFile = join(directory, 'lock-attempts.txt');
  t.after(async () => {
    if (mode === 'publish-barrier') await writeFile(barrierRelease, 'release', { mode: 0o600 });
    await Promise.all(ownedChildren);
    await rm(directory, { recursive: true, force: true });
  });
  const home = join(directory, 'home'), bin = join(directory, 'bin');
  await mkdir(home, { mode: 0o700 });
  await mkdir(bin, { mode: 0o700 });
  const capture = join(directory, 'executions.jsonl');
  const childFixture = join(directory, 'dsh-fixture.mjs');
  await writeFile(childFixture, `
import assert from 'node:assert/strict';
import { readFileSync, appendFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { apply as applyFeed } from ${JSON.stringify(feedPlugin)};
import { openStore } from ${JSON.stringify(memoryStore)};

const profile = process.argv[process.argv.indexOf('--profile') + 1];
const patch = JSON.parse(readFileSync(process.argv[process.argv.indexOf('--patch') + 1], 'utf8'));
const prompt = readFileSync(0, 'utf8');
const identity = { taskId: process.env.INTEL_TASK_ID, runId: process.env.INTEL_RUN_ID,
  runDate: process.env.INTEL_RUN_DATE };
appendFileSync(process.env.TEST_EXECUTIONS, JSON.stringify({ profile, patch, prompt, ...identity }) + '\\n');
assert.equal(profile, 'evolve');
assert.deepEqual(patch.filter(row => !row.insert && row.id !== 'headless-runner'), [
  { id: 'agent-default-model', config: { provider: 'isolated-provider', model: 'isolated-model' } },
  { id: 'llm-pi-ai', config: { providers: { 'isolated-provider': { transport: 'sse' } } } },
]);
assert.equal(identity.taskId, process.env.TEST_TASK_ID);
assert.match(identity.runId, /^[0-9a-f-]{36}$/);
assert.match(identity.runDate, /^\\d{4}-\\d{2}-\\d{2}$/);
if (process.env.TEST_MODE === 'publish-barrier') {
  // Exclusive creation also rejects a second unlocked execution deterministically.
  writeFileSync(process.env.TEST_BARRIER_READY, JSON.stringify(identity), { flag: 'wx', mode: 0o600 });
  const deadline = Date.now() + 20000;
  while (!existsSync(process.env.TEST_BARRIER_RELEASE)) {
    if (Date.now() >= deadline) throw Error('Test did not release the first execution barrier');
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}

const memory = openStore(join(process.env.DSH_HOME, 'intel-memory'));
const tools = new Map(), services = new Map(), effects = [], disposers = [];
try {
  const reference = process.env.TEST_MODE === 'quiet' ? null :
    Number(memory.prepare('INSERT INTO memories(text,kind,created_at,session_id) VALUES (?,?,?,?)')
      .run('隔离来源：用户希望复盘结论可追溯', 'fact', Date.now(), 'isolated-cli-session').lastInsertRowid);
  if (process.env.TEST_MODE !== 'memory-only' && process.env.TEST_MODE !== 'quiet') {
    const ctx = {
      provide: (name, value) => services.set(name, value),
      get: name => services.get(name),
      tools: { register: tool => {
        assert.equal(typeof tool.execute, 'function');
        tools.set(tool.name, tool);
        const dispose = () => tools.delete(tool.name);
        disposers.push(dispose);
        return dispose;
      } },
      effect: factory => {
        effects.push((async () => {
          const iterator = factory();
          let value;
          for (;;) {
            const step = iterator.next(value);
            if (step.done) return;
            value = await step.value;
          }
        })());
        return () => {};
      },
    };
    applyFeed(ctx, {});
    await Promise.all(effects);
    assert.ok(tools.has('feed_post'), 'real Feed tool must register');
    const result = await tools.get('feed_post').execute({
      title: '隔离复盘结论', body: '来源已核对；下一步继续验证用户可见结果。', source: 'isolated-cli',
      references: [reference], taskId: 'forged-task', runId: 'forged-run', runDate: '1999-01-01',
    }, { agent: { id: 'isolated-cli-session' } });
    assert.match(result.text, /已发布/);
    if (process.env.TEST_MODE === 'missing-reference')
      memory.prepare('DELETE FROM memories WHERE id=?').run(reference);
  }
} finally {
  for (const dispose of disposers.reverse()) dispose();
  memory.close();
}
`, { mode: 0o600 });
  await writeFile(join(bin, 'dsh'), '#!/bin/sh\nexec ' + quoteShell(process.execPath) + ' ' +
    quoteShell(childFixture) + ' "$@"\n', { mode: 0o700 });
  if (mode === 'publish-barrier') await writeFile(join(bin, 'flock'),
    '#!/bin/sh\nprintf \'attempt\\n\' >> "$TEST_LOCK_ATTEMPTS"\nexec /usr/bin/flock "$@"\n',
    { mode: 0o700 });
  const configFile = join(directory, 'routes.json');
  await writeFile(configFile, JSON.stringify({
    timeoutMs: 15000, timeZone: 'Asia/Shanghai', defaultRoute: 'primary',
    routes: {
      primary: { provider: 'isolated-provider', model: 'isolated-model', transport: 'sse' },
      fallback: { provider: 'isolated-fallback', model: 'isolated-fallback-model' },
    },
    tasks: { [taskId]: { route: 'primary', fallback: 'fallback', retrySafe: true,
      visibleOutput, deduplicateDaily: true } },
  }), { mode: 0o600 });
  // Child environments omit inherited credentials and runner identity. The
  // real CLI owns the task identity passed to its detached dsh child.
  const environment = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    !/KEY|SECRET|TOKEN|PASSWORD/i.test(key) && !/^INTEL_(TASK_ID|RUN_ID|RUN_DATE|RUNNER_LOCK)$/.test(key)));
  const env = { ...environment, PATH: bin + delimiter + dirname(process.execPath) + delimiter + '/usr/bin:/bin',
    DSH_HOME: home, INTEL_ROUTER_CONFIG: configFile, TEST_EXECUTIONS: capture, TEST_MODE: mode,
    TEST_TASK_ID: taskId, TEST_BARRIER_READY: barrierReady, TEST_BARRIER_RELEASE: barrierRelease,
    TEST_LOCK_ATTEMPTS: lockAttemptsFile };
  const run = (nextMode = mode) => spawnSync(process.execPath, [cli, taskId], {
    env: { ...env, TEST_MODE: nextMode }, encoding: 'utf8', timeout: 25000, maxBuffer: 1024 * 1024,
  });
  const runAsync = () => {
    const completion = new Promise(resolve => {
      const child = spawn(process.execPath, [cli, taskId], {
        env, detached: true, stdio: ['ignore', 'pipe', 'pipe'],
      });
      let stdout = '', stderr = '', error;
      child.stdout.setEncoding('utf8');
      child.stderr.setEncoding('utf8');
      child.stdout.on('data', chunk => { stdout += chunk; });
      child.stderr.on('data', chunk => { stderr += chunk; });
      const timer = setTimeout(() => {
        error = Error('Concurrent CLI exceeded 25 seconds');
        try { process.kill(-child.pid, 'SIGKILL'); }
        catch (killError) { if (killError.code !== 'ESRCH') error = killError; }
      }, 25000);
      child.once('error', spawnError => { error = spawnError; clearTimeout(timer); });
      child.once('close', (status, signal) => {
        clearTimeout(timer);
        resolve({ status, signal, stdout, stderr, error });
      });
    });
    ownedChildren.add(completion);
    return completion;
  };
  const lines = async path => {
    try { return (await readFile(path, 'utf8')).trim().split('\n').filter(Boolean).map(line => JSON.parse(line)); }
    catch (error) { if (error.code === 'ENOENT') return []; throw error; }
  };
  const posts = () => new FeedStore(join(home, 'intel-feed')).listPosts(100);
  const executions = () => lines(capture);
  const audit = () => lines(join(home, 'intel-joblog', 'route-runs.jsonl'));
  const memories = () => {
    const db = new DatabaseSync(join(home, 'intel-memory', 'memory.db'), { readOnly: true });
    try { return db.prepare('SELECT id,text FROM memories ORDER BY id').all(); }
    finally { db.close(); }
  };
  const lockAttempts = async () => {
    try { return (await readFile(lockAttemptsFile, 'utf8')).trim().split('\n').filter(Boolean); }
    catch (error) { if (error.code === 'ENOENT') return []; throw error; }
  };
  const waitForContention = async () => {
    const deadline = Date.now() + 10000;
    while (Date.now() < deadline) {
      const ready = await access(barrierReady).then(() => true, error => {
        if (error.code === 'ENOENT') return false;
        throw error;
      });
      if (ready && (await lockAttempts()).length === 2) return;
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    assert.fail('Both CLI processes must attempt the real lock while the first execution awaits release');
  };
  const release = () => writeFile(barrierRelease, 'release', { mode: 0o600 });
  return { home, configFile, run, runAsync, posts, executions, audit, memories, lockAttempts, waitForContention, release };
}

function exited(result, code) {
  assert.equal(result.error, undefined, result.error?.message);
  assert.equal(result.signal, null, result.stderr);
  assert.equal(result.status, code, result.stderr);
}

test('Linux CLI required output publishes real Feed content with trusted runner identity and memory reference', linux, async t => {
  const f = await fixture(t, 'publish');
  exited(f.run(), 0);
  const executions = await f.executions();
  assert.equal(executions.length, 1);
  const [execution] = executions, [post] = f.posts();
  assert.equal(f.posts().length, 1);
  assert.equal(execution.profile, 'evolve');
  assert.ok(execution.prompt.includes('日期 ' + execution.runDate));
  assert.ok(execution.prompt.includes('仅回复聊天或写记忆不算完成'));
  assert.match(execution.prompt,/不额外调用 artifact_save/);
  assert.doesNotMatch(execution.prompt,/长报告可用 artifact_save/);
  for (const key of ['taskId', 'runId', 'runDate']) assert.equal(post[key], execution[key]);
  assert.equal(post.idempotencyKey, 'feed:' + execution.taskId + ':' + execution.runDate);
  assert.equal(post.body, '来源已核对；下一步继续验证用户可见结果。');
  assert.deepEqual(post.references, f.memories().map(row => row.id));
  const audit = await f.audit();
  assert.equal(audit.at(-1).exit, 0);
  assert.equal(audit.at(-1).runId, post.runId);
  assert.equal(audit.some(entry => entry.kind === 'no-visible-output'), false);
});

test('Linux CLI memory-only exit zero becomes exit 65 without retrying completed effects', linux, async t => {
  const f = await fixture(t, 'memory-only');
  exited(f.run(), 65);
  assert.equal((await f.executions()).length, 1, 'fallback must not execute memory writes again');
  assert.equal(f.memories().length, 1);
  assert.deepEqual(f.posts(), []);
  const audit = await f.audit();
  assert.equal(audit.filter(entry => entry.kind === 'no-visible-output').length, 1);
  assert.equal(audit.some(entry => entry.kind === 'fallback'), false);
  assert.equal(audit.at(-1).exit, 65);
  assert.equal(audit.at(-1).processExit, 0);
  const jobRuns = JSON.parse(await readFile(join(f.home, 'intel-joblog', 'job_runs.json'), 'utf8'));
  assert.equal(jobRuns.evolve_dreaming.status, 'fail');
});

test('Linux CLI optional upkeep stays successful without manufacturing Feed or Artifact', linux, async t => {
  const f = await fixture(t, 'quiet', 'evolve_upkeep', 'optional');
  const socketPath=join(f.home,'upkeep-source','source.sock');
  const server=await startUpkeepServer({socketPath,timeoutMs:1000,
    source:{capture:async()=>({cursors:{},messages:[],counts:{newMessages:0}})},
    inspect:async()=>{assert.fail('Empty bootstrap must not inspect a model run');},
  });t.after(()=>server.close());
  const config=JSON.parse(await readFile(f.configFile,'utf8'));
  config.upkeep={socketPath,sourceTimeoutMs:1000,initialBackoffMs:100,maxBackoffMs:400,maxBatchMessages:3,maxBatchChars:1024};
  await writeFile(f.configFile,JSON.stringify(config),{mode:0o600});
  exited(await f.runAsync(), 0);
  const executions = await f.executions();
  assert.equal(executions.length, 0,'Empty bootstrap never launches dsh or a model');
  await assert.rejects(()=>access(join(f.home,'intel-feed')),{code:'ENOENT'});
  await assert.rejects(()=>access(join(f.home,'intel-memory')),{code:'ENOENT'});
  const artifactEntries = await readdir(join(f.home, 'intel-artifacts')).catch(error => {
    if (error.code === 'ENOENT') return [];
    throw error;
  });
  assert.deepEqual(artifactEntries, []);
  assert.equal((await f.audit()).some(entry => entry.kind === 'no-visible-output'), false);
});

test('Linux CLI reuses a valid daily publication without spawning dsh or writing another Feed post', linux, async t => {
  const f = await fixture(t, 'publish');
  exited(f.run(), 0);
  const before = f.posts(), beforeMemory = f.memories();
  exited(f.run(), 0);
  assert.equal((await f.executions()).length, 1, 'second CLI call must not spawn dsh');
  assert.deepEqual(f.posts(), before);
  assert.deepEqual(f.memories(), beforeMemory);
  const audit = await f.audit(), reused = audit.find(entry => entry.kind === 'already-published');
  assert.ok(reused);
  assert.equal(reused.publishedRunId, before[0].runId);
  assert.equal(reused.runDate, before[0].runDate);
  assert.equal(audit.at(-1).reused, true);
  assert.equal(audit.at(-1).exit, 0);
});

test('two simultaneous Linux CLI processes execute and publish a daily task only once', linux, async t => {
  const f = await fixture(t, 'publish-barrier');
  const first = f.runAsync(), second = f.runAsync();
  await f.waitForContention();
  assert.equal((await f.executions()).length, 1, 'only the lock owner may reach the execution barrier');
  assert.deepEqual(await f.lockAttempts(), ['attempt', 'attempt']);
  await f.release();
  const results = await Promise.all([first, second]);
  for (const result of results) exited(result, 0);
  const executions = await f.executions(), posts = f.posts(), memories = f.memories();
  assert.equal(executions.length, 1, 'flock must cover daily preflight and the real child execution');
  assert.equal(posts.length, 1, 'concurrent invocations must not publish duplicate Feed content');
  assert.equal(memories.length, 1, 'concurrent invocations must not repeat the source-memory write');
  assert.deepEqual(posts[0].references, [memories[0].id]);
  assert.equal(posts[0].runId, executions[0].runId);
  const audit = await f.audit();
  assert.equal(audit.filter(entry => entry.kind === 'start').length, 2);
  const reuse = audit.filter(entry => entry.kind === 'already-published');
  assert.equal(reuse.length, 1);
  assert.equal(reuse[0].publishedRunId, posts[0].runId);
  assert.equal(reuse[0].runDate, posts[0].runDate);
  assert.notEqual(reuse[0].runId, posts[0].runId);
  const finishes = audit.filter(entry => entry.kind === 'finish');
  assert.equal(finishes.length, 2);
  assert.ok(finishes.every(entry => entry.exit === 0));
  assert.equal(finishes.filter(entry => entry.reused === true).length, 1);
});

test('Linux CLI refuses success and daily reuse when real published Feed references a missing memory', linux, async t => {
  const f = await fixture(t, 'missing-reference');
  exited(f.run(), 65);
  assert.equal((await f.executions()).length, 1);
  const [post] = f.posts();
  assert.ok(post.body.trim(), 'real output exists on disk');
  assert.equal(post.references.length, 1);
  assert.deepEqual(f.memories(), [], 'referenced memory was removed only in the isolated fixture');
  exited(f.run('memory-only'), 65);
  assert.equal((await f.executions()).length, 2, 'invalid prior output must not suppress another CLI execution');
  assert.deepEqual(f.posts(), [post], 'failed validation must not manufacture a replacement Feed');
  assert.equal(f.memories().length, 1, 'second invocation writes its isolated memory exactly once');
  assert.notEqual(f.memories()[0].id, post.references[0], 'a new memory cannot repair the missing reference');
  const audit = await f.audit();
  assert.equal(audit.some(entry => entry.kind === 'already-published'), false);
  assert.equal(audit.some(entry => entry.kind === 'no-visible-output'), true);
  assert.equal(audit.some(entry => entry.kind === 'fallback'), false);
  assert.equal(audit.at(-1).exit, 65);
  assert.equal(audit.at(-1).processExit, 0);
});
