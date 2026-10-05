import { DatabaseSync } from 'node:sqlite';
import { join, resolve } from 'node:path';
import { homedir } from 'node:os';
import { publicationMetadata } from './persistence.js';
import { hasValiditySchema } from '../dsh-intelligence-memory/src/validity.js';

export function memoryReferences(references = [], authority, home = process.env.DSH_HOME || join(homedir(), '.dsh'), directory = join(home,'intel-memory')) {
  if (!Array.isArray(references) || references.length > 100 || references.some(id => !Number.isSafeInteger(id) || id < 1))
    throw Error('invalid memory references');
  const ids = [...new Set(references)];
  if (!ids.length) return ids;
  if (authority) {
    if (!authority.validate(ids)) throw Error('memory reference not found');
    return ids;
  }
  // Read-only fallback also supports independently loaded Feed/Artifact plugins.
  const db = new DatabaseSync(join(directory, 'memory.db'), { readOnly: true });
  try {
    const active=hasValiditySchema(db)?' AND NOT EXISTS (SELECT 1 FROM memory_state s WHERE s.memory_id=memories.id AND s.active=0)':'';
    const find = db.prepare('SELECT id FROM memories WHERE id=?'+active);
    if (!ids.every(id => find.get(id))) throw Error('memory reference not found');
    return ids;
  } finally { db.close(); }
}

export function publicationIdentity(channel, references, authority) {
  const ids = memoryReferences(references, authority);
  const source = ids.length ? { memoryDirectory: resolve(authority?.directory ?? join(process.env.DSH_HOME || join(homedir(), '.dsh'), 'intel-memory')) } : {};
  const taskId = process.env.INTEL_TASK_ID, runId = process.env.INTEL_RUN_ID, runDate = process.env.INTEL_RUN_DATE;
  if (taskId === undefined && runId === undefined && runDate === undefined) return publicationMetadata({ references: ids, ...source });
  return publicationMetadata({ taskId, runId, runDate, references: ids, ...source,
    idempotencyKey: `${channel}:${taskId}:${runDate}` });
}
