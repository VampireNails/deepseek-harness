import { DatabaseSync } from 'node:sqlite';
import { writeFileSync, renameSync, rmSync } from 'node:fs';
import { join, dirname, isAbsolute } from 'node:path';
import { randomUUID } from 'node:crypto';

/** SQLite owns cross-process exclusion; JSON remains the readable source of record. */
export function withStoreWriter(directory, action) {
  const lock = new DatabaseSync(join(directory, '.writer.sqlite'));
  try {
    lock.exec('PRAGMA busy_timeout=3000; BEGIN IMMEDIATE');
    const result = action(lock);
    lock.exec('COMMIT');
    return result;
  } finally { lock.close(); }
}

export function atomicJson(file, value) {
  const temporary = join(dirname(file), '.' + randomUUID() + '.tmp');
  try {
    writeFileSync(temporary, JSON.stringify(value, null, 2), { encoding: 'utf8', mode: 0o600 });
    renameSync(temporary, file);
  } finally { rmSync(temporary, { force: true }); }
}

export function publicationMetadata(value) {
  if (value === undefined) return {};
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('invalid publication metadata');
  const source = {};
  if (value.memoryDirectory !== undefined) {
    if (typeof value.memoryDirectory !== 'string' || value.memoryDirectory.length > 2048 || !isAbsolute(value.memoryDirectory)) throw Error('invalid memory source');
    source.memoryDirectory = value.memoryDirectory;
  }
  if (Object.keys(value).every(key => ['references','memoryDirectory'].includes(key))) {
    const references = value.references ?? [];
    if (!Array.isArray(references) || references.length > 100 || references.some(id => !Number.isSafeInteger(id) || id < 1)) throw Error('invalid memory references');
    return { references: [...new Set(references)], ...source };
  }
  for (const field of ['taskId', 'runId', 'runDate', 'idempotencyKey'])
    if (typeof value[field] !== 'string' || !value[field] || value[field].length > 256) throw Error('invalid publication ' + field);
  if (!/^[a-z][a-z0-9_-]{0,79}$/.test(value.taskId) || !/^\d{4}-\d{2}-\d{2}$/.test(value.runDate)) throw Error('invalid publication identity');
  const references = value.references ?? [];
  if (!Array.isArray(references) || references.length > 100 || references.some(id => !Number.isSafeInteger(id) || id < 1)) throw Error('invalid memory references');
  return { taskId: value.taskId, runId: value.runId, runDate: value.runDate, idempotencyKey: value.idempotencyKey, references: [...new Set(references)], ...source };
}
