import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Context } from '@deepseek-ai/cordis'
import { cp, link, mkdtemp, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import SessionStore, { SESSION_FORMAT_VERSION, SessionId } from '@deepseek-ai/dsh-session'
import JsonlSessionPersistence from '../src/index.ts'
import { logPath, sessionDir } from '../src/format.ts'

const fault = vi.hoisted(() => ({
  target: '', fail: false, entered: undefined as (() => void) | undefined,
  resume: undefined as Promise<void> | undefined, beforeMarker: undefined as (() => void) | undefined,
}))
vi.mock('node:fs/promises', async (original) => {
  const fs = await original<typeof import('node:fs/promises')>()
  return { ...fs, open: async (...args: Parameters<typeof fs.open>) => {
    const handle = await fs.open(...args)
    if (String(args[0]).includes('.session-deletions/') && String(args[0]).endsWith('.tmp')) {
      const sync = handle.sync.bind(handle)
      handle.sync = async () => { await sync(); fault.beforeMarker?.() }
    }
    return handle
  }, unlink: async (path: Parameters<typeof fs.unlink>[0]) => {
    if (String(path) === fault.target) {
      fault.entered?.()
      await fault.resume
      if (fault.fail) throw Object.assign(new Error('owned unlink fault'), { code: 'EIO' })
    }
    return fs.unlink(path)
  } }
})

describe.runIf(process.platform === 'linux')('persistent deletion of exact stored Sessions', () => {
  let root: string
  let ctx: Context
  const id = SessionId('session-owned-delete-exact')
  const other = SessionId('session-owned-delete-exact-other')
  const cwd = '/owned/project'
  beforeEach(async () => {
    root = await mkdtemp(join(tmpdir(), 'dsh-delete-owned-'))
    ctx = new Context()
    await ctx.plugin(JsonlSessionPersistence, { root, compression: 'none' })
  })
  afterEach(async () => {
    fault.fail = false
    fault.target = ''
    fault.entered = undefined
    fault.resume = undefined
    fault.beforeMarker = undefined
    await ctx.fiber.dispose()
    await rm(root, { recursive: true, force: true })
  })
  async function store(target = id): Promise<void> {
    const handle = await ctx.sessionPersistence.create({ version: SESSION_FORMAT_VERSION, id: target, createdAt: 1, cwd, isSeeded: false })
    await handle.flush()
    await handle.close()
  }
  it('removes physical generations without changing a prefix neighbor and refuses same-id recreation after restart', async () => {
    await store()
    await store(other)
    const preserved = await readFile(logPath(root, cwd, other, 'none'))
    expect(await ctx.sessionPersistence.deleteSession(id)).toEqual({ status: 'deleted', sessionId: id })
    expect(await readdir(sessionDir(root, cwd, id))).toEqual(['session.lock'])
    expect(await readFile(logPath(root, cwd, other, 'none'))).toEqual(preserved)
    expect(await ctx.sessionPersistence.stat(id)).toBeUndefined()
    expect((await ctx.sessionPersistence.list()).map(row => row.header.id)).toEqual([other])
    await ctx.fiber.dispose()
    ctx = new Context()
    await ctx.plugin(JsonlSessionPersistence, { root, compression: 'none' })
    expect(await ctx.sessionPersistence.stat(id)).toBeUndefined()
    await expect(ctx.sessionPersistence.open(id, 'write')).rejects.toThrow()
    await expect(store()).rejects.toThrow()
    expect(await ctx.sessionPersistence.deleteSession(id)).toEqual({ status: 'deleted', sessionId: id })
  })
  it('refuses an owned writer and succeeds only after it actually closes', async () => {
    await store()
    const writer = await ctx.sessionPersistence.open(id, 'write')
    expect(await ctx.sessionPersistence.deleteSession(id)).toEqual({ status: 'refused', sessionId: id, reason: 'writer-held' })
    expect(await ctx.sessionPersistence.stat(id)).toBeDefined()
    await writer.close()
    expect((await ctx.sessionPersistence.deleteSession(id)).status).toBe('deleted')
  })
  it('reports missing without creating a deletion marker and rejects ambiguous or link-shaped storage', async () => {
    expect(await ctx.sessionPersistence.deleteSession(id)).toEqual({ status: 'not-found', sessionId: id })
    await store()
    const original = await readFile(logPath(root, cwd, id, 'none'))
    const link = join(sessionDir(root, cwd, id), 'session.v3.jsonl')
    await symlink(logPath(root, cwd, other, 'none'), link)
    expect((await ctx.sessionPersistence.deleteSession(id)).status).toBe('refused')
    expect(await readFile(logPath(root, cwd, id, 'none'))).toEqual(original)
  })
  it('never follows an unregistered filename into shared data', async () => {
    await store()
    const shared = join(sessionDir(root, cwd, id), 'shared-memory.json')
    await writeFile(shared, 'preserve')
    expect((await ctx.sessionPersistence.deleteSession(id)).status).toBe('refused')
    expect(await readFile(shared, 'utf8')).toBe('preserve')
  })
  it('keeps a failed physical cleanup hidden across restart and retries the same target', async () => {
    await store()
    await store(other)
    fault.target = logPath(root, cwd, id, 'none')
    fault.fail = true
    expect(await ctx.sessionPersistence.deleteSession(id)).toEqual({ status: 'incomplete', sessionId: id })
    expect(await readFile(fault.target)).toBeDefined()
    expect(await ctx.sessionPersistence.stat(id)).toBeUndefined()
    expect((await ctx.sessionPersistence.list()).map(row => row.header.id)).toEqual([other])
    await ctx.fiber.dispose()
    ctx = new Context()
    await ctx.plugin(JsonlSessionPersistence, { root, compression: 'none' })
    await expect(ctx.sessionPersistence.open(id, 'read')).rejects.toThrow()
    await expect(store()).rejects.toThrow()
    fault.fail = false
    expect((await ctx.sessionPersistence.deleteSession(id)).status).toBe('deleted')
    expect(await ctx.sessionPersistence.stat(other)).toBeDefined()
  })
  it('excludes concurrent deletion, resume and same-id create while physical cleanup is suspended', async () => {
    await store()
    const entered = Promise.withResolvers<undefined>()
    const resume = Promise.withResolvers<undefined>()
    fault.target = logPath(root, cwd, id, 'none')
    fault.entered = () => { entered.resolve(undefined) }
    fault.resume = resume.promise
    const deletion = ctx.sessionPersistence.deleteSession(id)
    try {
      await entered.promise
      expect((await ctx.sessionPersistence.deleteSession(id)).status).toBe('refused')
      await expect(ctx.sessionPersistence.open(id, 'write')).rejects.toThrow()
      await expect(store()).rejects.toThrow()
    } finally { resume.resolve(undefined) }
    expect((await deletion).status).toBe('deleted')
  })
  it('refuses duplicate stores, hard links and path-like IDs without any removal', async () => {
    await store()
    const bytes = await readFile(logPath(root, cwd, id, 'none'))
    for (const invalid of ['../escape', '.', '..', '/absolute', 'session owned', 'session\u0000owned']) {
      expect((await ctx.sessionPersistence.deleteSession(SessionId(invalid))).status).toBe('refused')
    }
    await cp(sessionDir(root, cwd, id), sessionDir(root, '/owned/duplicate', id), { recursive: true })
    expect((await ctx.sessionPersistence.deleteSession(id)).status).toBe('refused')
    await rm(sessionDir(root, '/owned/duplicate', id), { recursive: true })
    await store(other)
    await link(logPath(root, cwd, other, 'none'), join(sessionDir(root, cwd, id), 'session.v3.jsonl'))
    expect((await ctx.sessionPersistence.deleteSession(id)).status).toBe('refused')
    expect(await readFile(logPath(root, cwd, id, 'none'))).toEqual(bytes)
  })
  it('cancels before commitment without hiding or removing the target', async () => {
    await store()
    const controller = new AbortController()
    controller.abort(new Error('owned cancellation'))
    await expect(ctx.sessionPersistence.deleteSession(id, controller.signal)).rejects.toThrow('owned cancellation')
    expect(await ctx.sessionPersistence.stat(id)).toBeDefined()
  })
  it('honors cancellation after preparing a synced marker but before publishing it', async () => {
    await store()
    const abort = new AbortController()
    fault.beforeMarker = () => { abort.abort(new Error('before publication')) }
    await expect(ctx.sessionPersistence.deleteSession(id, abort.signal)).rejects.toThrow('before publication')
    expect(await ctx.sessionPersistence.stat(id)).toBeDefined()
    expect(await readdir(join(root, '.session-deletions'))).toEqual([])
    fault.beforeMarker = undefined
    expect((await ctx.sessionPersistence.deleteSession(id)).status).toBe('deleted')
  })
  it('refuses a predecessor header with another identity without removing either generation', async () => {
    await store()
    const path = logPath(root, cwd, id, 'none')
    const original = await readFile(path)
    const predecessor = join(sessionDir(root, cwd, id), 'session.v3.jsonl')
    await writeFile(predecessor, JSON.stringify({ version: 3, id: other, createdAt: 1, cwd, isSeeded: false }) + '\n')
    expect((await ctx.sessionPersistence.deleteSession(id)).status).toBe('refused')
    expect(await readFile(path)).toEqual(original)
    expect(await readFile(predecessor, 'utf8')).toContain(other)
  })
  it('finishes committed cleanup despite caller cancellation and retires a primed read handle', async () => {
    await store()
    const reader = await ctx.sessionPersistence.open(id, 'read')
    await reader.read()
    const abort = new AbortController()
    fault.target = logPath(root, cwd, id, 'none')
    fault.entered = () => { abort.abort(new Error('after commitment')) }
    expect((await ctx.sessionPersistence.deleteSession(id, abort.signal)).status).toBe('deleted')
    await expect(reader.read()).rejects.toThrow()
    await reader.close()
  })
  it('fails closed on a corrupted retained marker rather than treating it as a missing session', async () => {
    await store()
    expect((await ctx.sessionPersistence.deleteSession(id)).status).toBe('deleted')
    const recordDir = join(root, '.session-deletions')
    const [record] = await readdir(recordDir)
    if (record === undefined) throw new Error('missing test marker')
    await writeFile(join(recordDir, record), '{"version":1}')
    expect((await ctx.sessionPersistence.deleteSession(id)).status).toBe('unknown')
    await expect(store()).rejects.toThrow()
  })
  it('removes compressed generations under the same retirement contract', async () => {
    await ctx.fiber.dispose()
    ctx = new Context()
    await ctx.plugin(JsonlSessionPersistence, { root, compression: 'zstd' })
    await store()
    expect((await ctx.sessionPersistence.deleteSession(id)).status).toBe('deleted')
    expect(await readdir(sessionDir(root, cwd, id))).toEqual(['session.lock'])
  })
  it('refuses a cold parent while a real subagent descendant remains attached', async () => {
    await store()
    await ctx.plugin(SessionStore)
    const child = ctx.sessions.prepare(SessionId('owned-attached-child'), { meta: { cwd, origin: 'subagent', parentSession: id } })
    const detach = ctx.sessions.enter(child)
    try {
      expect(await ctx.sessionPersistence.deleteSession(id)).toEqual({ status: 'refused', sessionId: id, reason: 'live-owner' })
      expect(await ctx.sessionPersistence.stat(id)).toBeDefined()
    } finally { detach() }
    expect((await ctx.sessionPersistence.deleteSession(id)).status).toBe('deleted')
  })
})
