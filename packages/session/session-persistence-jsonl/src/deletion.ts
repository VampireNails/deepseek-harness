/** Exact JSONL generation removal with a durable, root-wide same-id retirement record. */
import { createHash, randomUUID } from 'node:crypto'
import { link, lstat, mkdir, open, readFile, readdir, realpath, unlink } from 'node:fs/promises'
import { dirname, join, relative } from 'node:path'
import type { SessionId } from '@deepseek-ai/dsh-session'
import { encodeSegment, parseGenerationLogFilename } from './format.ts'

const RECORD_DIRECTORY = '.session-deletions'
interface DeletionRecord {
  readonly version: 1
  readonly sessionId: SessionId
  readonly directory: string
  readonly files: readonly string[]
}

/** Provider-local physical retirement; the provider holds its writer claims while using this helper. */
export class JsonlSessionDeletion {
  /** @param root - absolute configured persistence root. */
  constructor(private readonly root: string) {}

  private recordPath(id: SessionId): string {
    return join(this.root, RECORD_DIRECTORY, createHash('sha256').update(id).digest('hex') + '.json')
  }

  /**
   * Read a validated deletion record, refusing links and malformed recovery targets.
   * @param id - complete Session identity.
   * @returns its retained target directory, or undefined before commitment.
   */
  async directory(id: SessionId): Promise<string | undefined> {
    const record = await this.record(id)
    return record === undefined ? undefined : join(this.root, record.directory)
  }

  private async record(id: SessionId): Promise<DeletionRecord | undefined> {
    const path = this.recordPath(id)
    try {
      await this.assertDirectory(this.root)
      await this.assertDirectory(dirname(path))
      await this.assertFile(path, false)
    } catch (error: unknown) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return undefined
      throw error
    }
    const data: unknown = JSON.parse(await readFile(path, 'utf8'))
    if (typeof data !== 'object' || data === null || Array.isArray(data)) throw new Error('invalid deletion record')
    const record = data as Record<string, unknown>
    const directory = record.directory
    const files = record.files
    if (record.version !== 1 || record.sessionId !== id || typeof directory !== 'string'
      || directory.split('/').length !== 2 || directory.split('/').some(part => !part || part === '.' || part === '..' || part.includes('\\'))
      || directory.split('/')[1] !== encodeSegment(id) || !Array.isArray(files)
      || !files.every((file: unknown) => typeof file === 'string' && this.isGeneration(file))) throw new Error('invalid deletion recovery target')
    return { version: 1, sessionId: id, directory, files: files as string[] }
  }

  private isGeneration(file: string): boolean {
    return parseGenerationLogFilename(file, 'none') !== undefined || parseGenerationLogFilename(file, 'zstd') !== undefined
  }

  /**
   * Check all components and files without following symlinks or removing unowned entries.
   * @param dir - exact selected Session directory.
   * @returns all canonical generation filenames owned by this directory.
   */
  async inspect(dir: string): Promise<readonly string[]> {
    const parts = relative(this.root, dir).split(/[\\/]/)
    if (parts.length !== 2 || parts.some(part => !part || part === '.' || part === '..')) throw new Error('unsafe deletion directory')
    await this.assertDirectory(this.root)
    if (await realpath(this.root) !== this.root) throw new Error('aliased deletion root')
    await this.assertDirectory(dirname(dir))
    await this.assertDirectory(dir)
    const entries = await readdir(dir)
    for (const name of entries) {
      if (name !== 'session.lock' && !this.isGeneration(name)) throw new Error('unowned deletion entry')
      await this.assertFile(join(dir, name))
    }
    return entries.filter(name => this.isGeneration(name))
  }

  private async assertDirectory(path: string): Promise<void> {
    const value = await lstat(path)
    if (!value.isDirectory() || value.isSymbolicLink()) throw new Error('link-shaped deletion directory')
  }

  private async assertFile(path: string, exclusive = true): Promise<void> {
    const value = await lstat(path)
    if (!value.isFile() || value.isSymbolicLink() || (exclusive && value.nlink !== 1)) throw new Error('nonexclusive deletion file')
  }

  private async syncDirectory(path: string): Promise<void> {
    const handle = await open(path, 'r')
    try { await handle.sync() } finally { await handle.close() }
  }

  /**
   * Publish the irreversible marker durably, then remove only registered generation files.
   * @param id - exact identity whose write ownership the caller holds.
   * @param dir - validated selected directory.
   * @param committed - records the irreversible durability point before unlinking.
   * @param signal - cancellation observed immediately before marker publication.
   * @returns after every registered file is absent and the directory is synced.
   */
  async commit(id: SessionId, dir: string, committed: () => void, signal?: AbortSignal): Promise<void> {
    let record = await this.record(id)
    if (record === undefined) {
      const files = await this.inspect(dir)
      const recordDir = dirname(this.recordPath(id))
      await mkdir(recordDir, { mode: 0o700 }).catch((error: unknown) => {
        if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error
      })
      await this.assertDirectory(recordDir)
      await this.syncDirectory(this.root)
      record = { version: 1, sessionId: id, directory: relative(this.root, dir).split(/[\\/]/).join('/'), files }
      const temp = join(recordDir, randomUUID() + '.tmp')
      const handle = await open(temp, 'wx', 0o600)
      try {
        await handle.writeFile(JSON.stringify(record) + '\n')
        await handle.sync()
      } finally { await handle.close() }
      try {
        signal?.throwIfAborted()
        await link(temp, this.recordPath(id))
      } finally { await unlink(temp) }
      await this.syncDirectory(recordDir)
    }
    if (join(this.root, record.directory) !== dir) throw new Error('deletion target changed')
    await this.syncDirectory(dirname(this.recordPath(id)))
    committed()
    const remaining = await this.inspect(dir)
    if (remaining.some(file => !record.files.includes(file))) throw new Error('unregistered generation after deletion')
    for (const name of record.files) {
      const path = join(dir, name)
      try { await this.assertFile(path) } catch (error: unknown) {
        if ((error as NodeJS.ErrnoException).code === 'ENOENT') continue
        throw error
      }
      await unlink(path)
    }
    await this.syncDirectory(dir)
  }
}
