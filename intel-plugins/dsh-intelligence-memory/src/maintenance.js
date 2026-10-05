import { createHash } from "node:crypto";
import { closeSync, existsSync, lstatSync, openSync, readFileSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { DatabaseSync, backup } from "node:sqlite";
import { spaceCjk } from "./retriever.js";
import { createValiditySchema, hasValiditySchema } from './validity.js';

/** Inspect logical index consistency without exposing the underlying memory text. */
export function inspectMemoryIndex(db) {
  const count = sql => db.prepare(sql).get().count;
  const prepared=hasValiditySchema(db),active=prepared?'NOT EXISTS (SELECT 1 FROM memory_state s WHERE s.memory_id=m.id AND s.active=0)':'1';
  let mismatchedFtsRows = 0;
  for (const row of db.prepare("SELECT m.text AS original, f.text AS indexed FROM memories m JOIN memories_fts f ON f.rowid=m.id WHERE "+active).iterate())
    if (spaceCjk(row.original) !== row.indexed) mismatchedFtsRows++;
  return {
    memoryRows: count("SELECT count(*) AS count FROM memories"),
    ftsRows: count("SELECT count(*) AS count FROM memories_fts"),
    orphanFtsRows: count("SELECT count(*) AS count FROM memories_fts f LEFT JOIN memories m ON m.id=f.rowid WHERE m.id IS NULL"),
    missingFtsRows: count("SELECT count(*) AS count FROM memories m LEFT JOIN memories_fts f ON f.rowid=m.id WHERE f.rowid IS NULL AND "+active),
    mismatchedFtsRows,
    inactiveMemoryRows: prepared?count('SELECT count(*) AS count FROM memories m JOIN memory_state s ON s.memory_id=m.id WHERE s.active=0'):0,
    inactiveFtsRows: prepared?count('SELECT count(*) AS count FROM memories_fts f JOIN memory_state s ON s.memory_id=f.rowid WHERE s.active=0'):0,
  };
}

function memoryHash(db) {
  const digest = createHash("sha256");
  const rows = db.prepare("SELECT * FROM memories ORDER BY id,rowid");
  rows.setReadBigInts(true);
  for (const row of rows.iterate())
    digest.update(JSON.stringify(row, (_, value) => typeof value === "bigint" ? { integer: value.toString() } : value) + "\n");
  return digest.digest("hex");
}

/**
 * Explicit offline maintenance: back up SQLite, then rebuild only the derived FTS rows.
 * Writers should be quiescent for the backup/repair window. Rollback of a running system
 * must rebuild from its current memories, never restore this snapshot over later writes.
 */
async function backupMemoryStore({databasePath,backupPath},assertSource=()=>{}) {
  if (typeof databasePath !== "string" || typeof backupPath !== "string" ||
      !isAbsolute(databasePath) || !isAbsolute(backupPath) || resolve(databasePath) === resolve(backupPath) ||
      !existsSync(databasePath) || !lstatSync(databasePath).isFile() || existsSync(backupPath))
    throw new Error("existing database and distinct unused absolute backup path required");
  const source = new DatabaseSync(databasePath, { readOnly: true });
  try {
    assertSource(source);
    inspectMemoryIndex(source);
    // Reserve the destination with private permissions; never overwrite another backup.
    const descriptor = openSync(backupPath, "wx", 0o600);
    closeSync(descriptor);
    await backup(source, backupPath);
  } finally { source.close(); }
  const backupSha256 = createHash("sha256").update(readFileSync(backupPath)).digest("hex");
  const snapshot=new DatabaseSync(backupPath,{readOnly:true});let memoryRowsSha256;
  try{memoryRowsSha256=memoryHash(snapshot);}finally{snapshot.close();}
  return {backupSha256,backupPathSha256:createHash('sha256').update(backupPath).digest('hex'),memoryRowsSha256};
}

/** Back up before rebuilding only active derived FTS rows; preserve state and audit. */
export async function repairMemoryIndex({ databasePath, backupPath }) {
  const saved=await backupMemoryStore({databasePath,backupPath});
  const db = new DatabaseSync(databasePath);
  try {
    db.exec("PRAGMA busy_timeout=3000; BEGIN IMMEDIATE");
    try {
      const before = inspectMemoryIndex(db), memoryRowsSha256 = memoryHash(db);
      if(memoryRowsSha256!==saved.memoryRowsSha256)throw new Error('memory changed after maintenance backup');
      if (before.orphanFtsRows || before.missingFtsRows || before.mismatchedFtsRows || before.inactiveFtsRows) {
        db.exec("DELETE FROM memories_fts");
        const insert = db.prepare("INSERT INTO memories_fts(rowid,text) VALUES (?,?)");
        const active=hasValiditySchema(db)?' WHERE NOT EXISTS (SELECT 1 FROM memory_state s WHERE s.memory_id=m.id AND s.active=0)':'';
        for (const row of db.prepare("SELECT m.id,m.text FROM memories m"+active+" ORDER BY m.id,m.rowid").iterate())
          insert.run(row.id, spaceCjk(row.text));
      }
      const after = inspectMemoryIndex(db);
      if (memoryRowsSha256 !== memoryHash(db) || after.orphanFtsRows || after.missingFtsRows || after.mismatchedFtsRows || after.inactiveFtsRows)
        throw new Error("memory index repair invariant failed");
      db.exec("COMMIT");
      return { before, after, ...saved, memoryRowsSha256 };
    } catch (error) { db.exec("ROLLBACK"); throw error; }
  } finally { db.close(); }
}

/** Explicit backed-up preparation; never an implicit migration while opening legacy data. */
export async function prepareMemoryValidity({databasePath,backupPath}) {
  const saved=await backupMemoryStore({databasePath,backupPath},db=>{if(hasValiditySchema(db))throw new Error('memory validity already prepared');});
  const db=new DatabaseSync(databasePath);
  try{
    db.exec('PRAGMA busy_timeout=3000; BEGIN IMMEDIATE');
    try{
      if(memoryHash(db)!==saved.memoryRowsSha256)throw new Error('memory changed after preparation backup');
      createValiditySchema(db);
      if(!hasValiditySchema(db)||memoryHash(db)!==saved.memoryRowsSha256)throw new Error('memory validity preparation invariant failed');
      const after=inspectMemoryIndex(db);db.exec('COMMIT');return {prepared:true,...saved,after};
    }catch(error){db.exec('ROLLBACK');throw error;}
  }finally{db.close();}
}

/** Operator-only CLI; no implicit production path and no memory text in its output. */
export async function memoryMaintenanceCli(argv) {
  const [mode, databasePath, backupPath, ...extra] = argv;
  if (extra.length || !isAbsolute(databasePath ?? "") || !existsSync(databasePath))
    throw new Error("explicit existing absolute memory database required");
  if (mode === "repair" && backupPath) return repairMemoryIndex({ databasePath, backupPath });
  if (mode === 'prepare-validity' && backupPath) return prepareMemoryValidity({databasePath,backupPath});
  if (mode !== "inspect" || backupPath) throw new Error("use inspect database, repair database unused-backup or prepare-validity database unused-backup");
  const db = new DatabaseSync(databasePath, { readOnly: true });
  try { return inspectMemoryIndex(db); } finally { db.close(); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try { console.log(JSON.stringify(await memoryMaintenanceCli(process.argv.slice(2)), null, 2)); }
  catch { console.error("Memory index maintenance failed; inspect the private database and backup state."); process.exitCode = 1; }
}
