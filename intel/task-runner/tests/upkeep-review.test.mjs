import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runUpkeep } from '../upkeep.mjs';

test('external source failures with an upkeep error code still expose only a content-free diagnostic', async () => {
  const privateMarker = 'private-source-session-and-prompt-fixture-marker';
  const privateError = Object.assign(new Error(privateMarker), { code: 'UPKEEP_SOURCE_MISSING', privateMarker });
  const initial = { version: 1, cursors: {}, backoffMs: 0, nextDueAtMs: 0, lastNowMs: 0 };
  let writes = 0, executions = 0;
  await assert.rejects(() => runUpkeep({
    config: { initialBackoffMs: 100, maxBackoffMs: 400, maxBatchMessages: 5, maxBatchChars: 1024 },
    nowMs: 1000,
    stateStore: { async read() { return structuredClone(initial); }, async write() { writes++; } },
    source: { async capture() { throw privateError; } },
    async execute() { executions++; return 0; },
    async inspect() { assert.fail('Source failure must stop run inspection'); },
    async record() { assert.fail('Source failure must not publish a success audit'); },
  }), error => {
    assert.equal(error.code, 'UPKEEP_SOURCE_MISSING');
    assert.equal(error.message, 'UPKEEP_SOURCE_MISSING', 'An allowed code must not whitelist the external error message');
    assert.equal(error.stack.includes(privateMarker), false);
    assert.equal(JSON.stringify(error).includes(privateMarker), false);
    return true;
  });
  assert.equal(writes, 0);
  assert.equal(executions, 0);
});

test('shared upkeep alias state recovers the pending original task identity without redispatching its effects', async () => {
  const pending = { taskId: 'memory_upkeep', runId: '11111111-1111-4111-8111-111111111111',
    batchHash: 'a'.repeat(64), proposedCursors: { 'anonymous-human-source': 12 } };
  let state = { version: 1, cursors: { 'anonymous-human-source': 10 },
    backoffMs: 0, nextDueAtMs: 0, lastNowMs: 0, pending };
  const inspections = [], writes = [], audit = [];
  const outcome = await runUpkeep({
    taskId: 'evolve_upkeep',
    config: { initialBackoffMs: 100, maxBackoffMs: 400, maxBatchMessages: 5, maxBatchChars: 1024 },
    nowMs: 1000,
    stateStore: { async read() { return structuredClone(state); }, async write(next) {
      writes.push(structuredClone(next)); state = structuredClone(next);
    } },
    source: { async capture() { assert.fail('Pending recovery must not recapture a new batch'); } },
    async execute() { assert.fail('Alias recovery must not repeat pending memory writes'); },
    async inspect(identity) {
      inspections.push(identity);
      return { complete: identity.taskId === pending.taskId && identity.runId === pending.runId,
        failedWrites: 0, writeIds: [17] };
    },
    async record(entry) { audit.push(entry); },
  });
  assert.deepEqual(inspections, [{ taskId: 'memory_upkeep', runId: pending.runId }]);
  assert.equal(outcome.kind, 'upkeep-recovered');
  assert.equal(writes.length, 1);
  assert.deepEqual(state.cursors, pending.proposedCursors);
  assert.equal(state.pending ?? null, null);
  assert.equal(JSON.stringify(audit).includes('anonymous-human-source'), false);
});

test('a socket initialization failure after listen releases the real listener and its owned pathname',
  { skip: process.platform !== 'linux' }, async () => {
    const { spawnSync } = await import('node:child_process');
    const moduleUrl = new URL('../../../intel-plugins/dsh-intelligence-evolution/src/upkeep-socket.js', import.meta.url).href;
    // A private subprocess isolates the one syscall fault injection and bounds
    // cleanup even when the rejected initializer leaks its otherwise-unreachable server.
    const program = `
      import fs from 'node:fs/promises';
      import { syncBuiltinESMExports } from 'node:module';
      import { createConnection } from 'node:net';
      import { tmpdir } from 'node:os';
      import { join } from 'node:path';
      const { startUpkeepServer } = await import(process.argv[1]);
      const directory = await fs.mkdtemp(join(tmpdir(), 'upkeep-init-review-'));
      const socketPath = join(directory, 'source.sock');
      const chmod = fs.chmod;
      let failed = false, injected = false;
      try {
        fs.chmod = async (path, ...args) => {
          if (path === socketPath && !injected) {
            injected = true;
            throw Object.assign(Error('anonymous fixture chmod I/O failure'), { code: 'EIO' });
          }
          return chmod(path, ...args);
        };
        syncBuiltinESMExports();
        try {
          await startUpkeepServer({ socketPath, timeoutMs: 1000,
            source: { async capture() { return {}; } }, inspect: async () => ({}) });
        } catch { failed = true; }
        finally { fs.chmod = chmod; syncBuiltinESMExports(); }
        const acceptsConnection = await new Promise(resolve => {
          const socket = createConnection(socketPath);
          const timer = setTimeout(() => { socket.destroy(); resolve(false); }, 1000);
          socket.once('connect', () => { clearTimeout(timer); socket.destroy(); resolve(true); });
          socket.once('error', () => { clearTimeout(timer); socket.destroy(); resolve(false); });
        });
        let pathExists;
        try { await fs.lstat(socketPath); pathExists = true; } catch (error) {
          if (error.code !== 'ENOENT') throw error;
          pathExists = false;
        }
        console.log(JSON.stringify({ failed, injected, acceptsConnection, pathExists }));
      } finally {
        fs.chmod = chmod; syncBuiltinESMExports();
        await fs.rm(directory, { recursive: true, force: true });
      }
      process.exit(0);
    `;
    const child = spawnSync(process.execPath, ['--input-type=module', '-e', program, moduleUrl],
      { encoding: 'utf8', timeout: 10000, maxBuffer: 65536 });
    assert.equal(child.error, undefined);
    assert.equal(child.signal, null);
    assert.equal(child.status, 0, child.stderr);
    const report = JSON.parse(child.stdout);
    assert.equal(report.injected, true, 'The real listener must reach its post-listen permission step');
    assert.equal(report.failed, true);
    assert.equal(report.acceptsConnection, false, 'A rejected initializer must not leave an unowned listening server');
    assert.equal(report.pathExists, false, 'Initialization failure must remove only its newly owned socket path');
  });

test('socket teardown preserves another real listener that replaced its original pathname',
  { skip: process.platform !== 'linux' }, async () => {
    const { spawnSync } = await import('node:child_process');
    const moduleUrl = new URL('../../../intel-plugins/dsh-intelligence-evolution/src/upkeep-socket.js', import.meta.url).href;
    const program = `
      import fs from 'node:fs/promises';
      import { createConnection, createServer } from 'node:net';
      import { tmpdir } from 'node:os';
      import { join } from 'node:path';
      const { startUpkeepServer } = await import(process.argv[1]);
      const directory = await fs.mkdtemp(join(tmpdir(), 'upkeep-owner-review-'));
      const socketPath = join(directory, 'source.sock');
      let owned, replacement;
      try {
        owned = await startUpkeepServer({ socketPath, timeoutMs: 1000,
          source: { async capture() { return {}; } }, inspect: async () => ({}) });
        await fs.rename(socketPath, join(directory, 'old-source.sock'));
        replacement = createServer(socket => socket.destroy());
        await new Promise((resolve, reject) => {
          replacement.once('error', reject); replacement.listen(socketPath, resolve);
        });
        await fs.chmod(socketPath, 0o600);
        const originalReplacement = await fs.lstat(socketPath);
        await owned.close();
        let preservedIdentity = false;
        try {
          const current = await fs.lstat(socketPath);
          preservedIdentity = current.ino === originalReplacement.ino && current.dev === originalReplacement.dev;
        } catch (error) { if (error.code !== 'ENOENT') throw error; }
        const acceptsConnection = await new Promise(resolve => {
          const socket = createConnection(socketPath);
          const timer = setTimeout(() => { socket.destroy(); resolve(false); }, 1000);
          socket.once('connect', () => { clearTimeout(timer); socket.destroy(); resolve(true); });
          socket.once('error', () => { clearTimeout(timer); socket.destroy(); resolve(false); });
        });
        console.log(JSON.stringify({ preservedIdentity, acceptsConnection }));
      } finally {
        if (owned) await owned.close();
        if (replacement?.listening) await new Promise((resolve, reject) => replacement.close(error => error ? reject(error) : resolve()));
        await fs.rm(directory, { recursive: true, force: true });
      }
    `;
    const child = spawnSync(process.execPath, ['--input-type=module', '-e', program, moduleUrl],
      { encoding: 'utf8', timeout: 10000, maxBuffer: 65536 });
    assert.equal(child.error, undefined);
    assert.equal(child.signal, null);
    assert.equal(child.status, 0, child.stderr);
    const report = JSON.parse(child.stdout);
    assert.equal(report.preservedIdentity, true, 'Closing an old owner must preserve a replacement socket inode');
    assert.equal(report.acceptsConnection, true, 'The replacement real server must remain reachable after old-owner teardown');
  });
