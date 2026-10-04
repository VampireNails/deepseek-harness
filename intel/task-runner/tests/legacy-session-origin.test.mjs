import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,existsSync,readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {DatabaseSync} from 'node:sqlite';
import {AutomationSessionStore} from '../../../intel-plugins/dsh-intelligence-evolution/src/session-origin.js';
import {collectLegacyCandidates,applyLegacyCandidates,rollbackLegacyCandidates,readLegacyIndexIds} from '../legacy-session-origin.mjs';

const base=Date.parse('2026-10-04T16:00:00.000Z');
const templates=[{taskId:'heartbeat',prompt:'Known complete fixture task\n'}];
const interval=(start=0,finish=100)=>({taskId:'heartbeat',start:base+start,finish:base+finish,
  source:'fixture-route-runs.jsonl',sourceHash:'a'.repeat(64),startLine:1,finishLine:2});
const session=(id='fixture-secret-root-a',created=10,promptAt=20,origin)=>({generation:4,
  header:{id,createdAt:base+created,...(origin?{origin}:{})},
  firstUser:{type:'user/message',time:base+promptAt,data:{source:{kind:'user'},content:[{type:'text',text:templates[0].prompt}]}}});
const observations=(sessions=[session()],intervals=[interval()])=>({sessions,templates,intervals});
function fixture(t){
  const directory=mkdtempSync(join(tmpdir(),'legacy-origin-fixture-'));
  t.after(()=>rmSync(directory,{recursive:true,force:true}));
  return {directory,indexPath:join(directory,'automation-sessions.sqlite'),backupPath:join(directory,'before.sqlite')};
}
function initialize(directory,entries=[]){
  const store=new AutomationSessionStore(directory);
  try{for(const [id,identity]of entries)store.record(id,identity);}finally{store.close();}
}
function rows(indexPath){
  const db=new DatabaseSync(indexPath,{readOnly:true});
  try{return db.prepare('SELECT * FROM automation_sessions ORDER BY session_id').all().map(row=>({...row}));}
  finally{db.close();}
}
const native={taskId:'heartbeat',runId:'native-run-fixture',runDate:'2026-10-05'};

test('exact full first user text and a unique root interval produce an explicitly synthetic Shanghai identity',()=>{
  const result=collectLegacyCandidates(observations());
  assert.equal(result.candidates.length,1);
  assert.equal(result.candidates[0].runDate,'2026-10-05');
  assert.match(result.candidates[0].runId,/^legacy-evidence:[a-f0-9]{64}$/);
  assert.equal(result.receipt.counts.unknown,0);
  const serialized=JSON.stringify(result.receipt);
  assert.ok(!serialized.includes(session().header.id));
  assert.ok(!serialized.includes(templates[0].prompt));
  const appended={...interval(),sourceHash:'b'.repeat(64)};
  assert.equal(collectLegacyCandidates(observations([session()],[appended])).candidates[0].runId,result.candidates[0].runId);
});

test('public candidate receipts redact private audit source paths and session IDs embedded in them',()=>{
  const id=session().header.id,source='/private-fixture/'+id+'/audit.jsonl';
  const result=collectLegacyCandidates(observations([session()],[{...interval(),source}]));
  assert.equal(result.candidates[0].evidence.source,source);
  const publicReceipt=JSON.stringify(result.receipt);
  assert.ok(!publicReceipt.includes(id));assert.ok(!publicReceipt.includes(source));
  assert.equal(result.receipt.candidates[0].evidence.sourceHash,'a'.repeat(64));
  assert.equal(result.receipt.candidates[0].evidence.startLine,1);
});

test('a root matching two overlapping intervals still competes with another root matching only one',()=>{
  const result=collectLegacyCandidates(observations([session(),session('fixture-secret-root-b',60,70)],
    [interval(),{...interval(50,150),startLine:3,finishLine:4}]));
  assert.equal(result.candidates.length,0);
  assert.equal(result.receipt.counts.multipleIntervals,1);
  assert.equal(result.receipt.counts.multipleRoots,1);
  assert.equal(result.receipt.counts.unknown,2);
});

test('an already indexed matching root cannot make another root look uniquely assigned',()=>{
  const result=collectLegacyCandidates({...observations([session(),session('fixture-secret-root-b',30,40)]),
    indexedSessionIds:['fixture-secret-root-b']});
  assert.equal(result.candidates.length,0);
  assert.equal(result.receipt.counts.alreadyIndexed,1);
  assert.equal(result.receipt.counts.unknown,1);
});

test('duplicate physical observations cannot assign their logical session or erase a competing root',()=>{
  const result=collectLegacyCandidates(observations([session(),session('fixture-secret-root-b',30,40),session('fixture-secret-root-b',30,40)]));
  assert.equal(result.candidates.length,0);
  assert.equal(result.receipt.counts.duplicateIds,2);
  assert.equal(result.receipt.counts.multipleRoots,1);
});

test('overlapping cron intervals with different exact task templates remain separable',()=>{
  const other=session('fixture-other',30,40);
  other.firstUser.data.content[0].text='Different fixture task\n';
  const result=collectLegacyCandidates({sessions:[session(),other],templates:[...templates,{taskId:'morning',prompt:'Different fixture task\n'}],
    intervals:[interval(),{...interval(),taskId:'morning',startLine:3,finishLine:4}]});
  assert.equal(result.candidates.length,2);
  assert.deepEqual(result.candidates.map(row=>row.taskId),['heartbeat','morning']);
});

test('a shared exact template assigned to two task identities is ambiguous when both audits overlap',()=>{
  const result=collectLegacyCandidates({...observations([session()],[interval(),{...interval(),taskId:'morning',startLine:3,finishLine:4}]),
    templates:[...templates,{taskId:'morning',prompt:templates[0].prompt}]});
  assert.equal(result.candidates.length,0);
  assert.equal(result.receipt.counts.multipleIntervals,1);
});

test('subagents are excluded, and normal/partial/wrong-source/outside-time observations stay unknown',()=>{
  const ordinary=session('fixture-ordinary');ordinary.firstUser.data.content[0].text='Private-looking fixture conversation';
  const partial=session('fixture-partial');partial.firstUser.data.content[0].text=templates[0].prompt.trim();
  const injected=session('fixture-hook');injected.firstUser.data.source.kind='hook';
  const result=collectLegacyCandidates(observations([session('fixture-child',10,20,'subagent'),ordinary,partial,injected,session('fixture-outside',10,200)]));
  assert.equal(result.candidates.length,0);
  assert.equal(result.receipt.counts.subagents,1);
  assert.equal(result.receipt.counts.unknown,4);
});

test('read-only missing index lookup creates nothing',t=>{
  const paths=fixture(t);
  assert.deepEqual(readLegacyIndexIds(paths.indexPath),[]);
  assert.equal(existsSync(paths.indexPath),false);
});

test('an empty apply batch creates neither index nor backup',async t=>{
  const paths=fixture(t);
  const receipt=await applyLegacyCandidates({...paths,candidates:[]});
  assert.equal(receipt.inserted,0);assert.equal(receipt.backup.status,'no-change');
  assert.equal(existsSync(paths.indexPath),false);assert.equal(existsSync(paths.backupPath),false);
});

test('apply uses a real SQLite snapshot backup and preserves pre-existing native records',async t=>{
  const paths=fixture(t);initialize(paths.directory,[['fixture-native',native]]);
  const receipt=await applyLegacyCandidates({...paths,candidates:collectLegacyCandidates(observations()).candidates});
  assert.equal(receipt.inserted,1);
  assert.equal(receipt.backup.status,'created');
  assert.equal(rows(paths.backupPath).length,1);
  assert.equal(rows(paths.backupPath)[0].run_id,native.runId);
  assert.equal(rows(paths.indexPath).length,2);
  assert.match(receipt.backup.sha256,/^[a-f0-9]{64}$/);
  assert.ok(!JSON.stringify(receipt).includes(session().header.id));
});

test('public apply receipt does not echo a private backup path containing an opaque session ID',async t=>{
  const paths=fixture(t);initialize(paths.directory);
  const backupPath=join(paths.directory,session().header.id+'.sqlite');
  const receipt=await applyLegacyCandidates({...paths,backupPath,candidates:collectLegacyCandidates(observations()).candidates});
  assert.equal(existsSync(backupPath),true);
  assert.ok(!JSON.stringify(receipt).includes(session().header.id));
  assert.ok(!JSON.stringify(receipt).includes(backupPath));
});

test('a conflicting final candidate rolls back every earlier insert and retains the backup',async t=>{
  const paths=fixture(t);initialize(paths.directory,[[session('fixture-secret-root-b').header.id,native]]);
  const candidates=collectLegacyCandidates(observations([session(),session('fixture-secret-root-b',110,120)],
    [interval(),{...interval(100,200),startLine:3,finishLine:4}])).candidates;
  assert.equal(candidates.length,2);
  await assert.rejects(applyLegacyCandidates({...paths,candidates}),/identity conflict/);
  assert.equal(rows(paths.indexPath).length,1);
  assert.equal(rows(paths.indexPath)[0].run_id,native.runId);
  assert.deepEqual(rows(paths.backupPath),rows(paths.indexPath));
});

test('repeat apply reuses identical rows and its empty rollback receipt cannot remove them',async t=>{
  const paths=fixture(t);initialize(paths.directory);
  const candidates=collectLegacyCandidates(observations()).candidates;
  const first=await applyLegacyCandidates({...paths,candidates});
  const second=await applyLegacyCandidates({...paths,backupPath:join(paths.directory,'second.sqlite'),candidates});
  assert.equal(first.inserted,1);assert.equal(second.inserted,0);assert.equal(second.reused,1);
  assert.equal(rollbackLegacyCandidates({indexPath:paths.indexPath,receipt:second}).deleted,0);
  assert.equal(rows(paths.indexPath).length,1);
});

test('rollback deletes only inserted matching identities, retaining changed and new native rows',async t=>{
  const paths=fixture(t);initialize(paths.directory,[['fixture-native',native]]);
  const candidates=collectLegacyCandidates(observations([session(),session('fixture-secret-root-b',110,120)],
    [interval(),{...interval(100,200),startLine:3,finishLine:4}])).candidates;
  const receipt=await applyLegacyCandidates({...paths,candidates});
  const db=new DatabaseSync(paths.indexPath);
  try{
    db.prepare('UPDATE automation_sessions SET run_id=? WHERE session_id=?').run('native-replacement-fixture','fixture-secret-root-b');
    db.prepare('INSERT INTO automation_sessions VALUES (?,?,?,?)').run('fixture-new-native',native.taskId,'new-native-fixture',native.runDate);
  }finally{db.close();}
  const result=rollbackLegacyCandidates({indexPath:paths.indexPath,receipt});
  assert.equal(result.deleted,1);assert.equal(result.changed,1);assert.equal(result.missing,0);
  assert.deepEqual(rows(paths.indexPath).map(row=>row.session_id),['fixture-native','fixture-new-native','fixture-secret-root-b']);
  assert.equal(rollbackLegacyCandidates({indexPath:paths.indexPath,receipt}).deleted,0);
});

test('an existing backup path or invalid synthetic identity rejects before any index write',async t=>{
  const paths=fixture(t);initialize(paths.directory,[['fixture-native',native]]);
  const candidates=collectLegacyCandidates(observations()).candidates;
  await assert.rejects(applyLegacyCandidates({...paths,backupPath:paths.indexPath,candidates}),/unused backup/);
  await assert.rejects(applyLegacyCandidates({...paths,candidates:[{...candidates[0],runId:'pretend-original-run'}]}),/legacy evidence/);
  assert.equal(rows(paths.indexPath).length,1);assert.equal(existsSync(paths.backupPath),false);
});

test('CLI dry-run receives private observations on stdin and emits only anonymous receipt without creating an index',t=>{
  const paths=fixture(t),cli=fileURLToPath(new URL('../legacy-session-origin.mjs',import.meta.url));
  const result=spawnSync(process.execPath,[cli,'dry-run','--index',paths.indexPath],{input:JSON.stringify(observations()),encoding:'utf8'});
  assert.equal(result.status,0,result.stderr);
  assert.equal(JSON.parse(result.stdout).candidates.length,1);
  assert.ok(!result.stdout.includes(session().header.id));assert.ok(!result.stdout.includes(templates[0].prompt));
  assert.equal(existsSync(paths.indexPath),false);
});

test('CLI apply and rollback operate only on the explicitly supplied temporary fixture index',t=>{
  const paths=fixture(t),cli=fileURLToPath(new URL('../legacy-session-origin.mjs',import.meta.url));
  initialize(paths.directory,[['fixture-native',native]]);
  const applied=spawnSync(process.execPath,[cli,'apply','--index',paths.indexPath,'--backup',paths.backupPath],
    {input:JSON.stringify(observations()),encoding:'utf8'});
  assert.equal(applied.status,0,applied.stderr);
  assert.equal(rows(paths.indexPath).length,2);assert.equal(rows(paths.backupPath).length,1);
  const rolled=spawnSync(process.execPath,[cli,'rollback','--index',paths.indexPath],{input:applied.stdout,encoding:'utf8'});
  assert.equal(rolled.status,0,rolled.stderr);assert.equal(JSON.parse(rolled.stdout).deleted,1);
  assert.equal(rows(paths.indexPath).length,1);
  assert.equal(rows(paths.indexPath)[0].run_id,native.runId);
  assert.ok(!readFileSync(paths.backupPath).equals(readFileSync(paths.indexPath)));
});
