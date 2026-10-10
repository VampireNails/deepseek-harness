import {test,mock} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {syncBuiltinESMExports} from 'node:module';
import {spawnSync,spawn} from 'node:child_process';
import {once} from 'node:events';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {CronStore} from '../src/store.js';
import {GoalsStore} from '../../dsh-intelligence-goals/src/goals.js';
import {atomicText} from '../../shared/json-store.js';
import {CronScheduler} from '../src/scheduler.js';
import {DatabaseSync} from 'node:sqlite';

function fixture(t){const dir=fs.mkdtempSync(join(tmpdir(),'json-faults-'));t.after(()=>{mock.restoreAll();syncBuiltinESMExports();fs.rmSync(dir,{recursive:true,force:true});});return dir;}
function patch(name,fn){mock.method(fs,name,fn);syncBuiltinESMExports();}
function job(i=1){return {id:`c${i}`,name:'n',prompt:'p',sessionId:'s',schedule:{kind:'interval',seconds:300},scheduleId:`cron-c${i}`,nextFire:'2026-10-10T12:00:00.000Z'};}

test('goals partial write before publication retains JSON and Markdown without committing progress',t=>{
 const dir=fixture(t),store=new GoalsStore(dir);store.create('one');const json=fs.readFileSync(store.file),md=fs.readFileSync(store.mdFile),original=fs.writeFileSync;
 patch('writeFileSync',(fd,text,...args)=>{original(fd,text.slice(0,5),...args);throw Object.assign(Error('PRIVATE'),{code:'ENOSPC'});});
 assert.throws(()=>store.logProgress('g1','lost'),{code:'GOALS_WRITE_FAILED',errno:'ENOSPC',committed:false});assert.deepEqual(fs.readFileSync(store.file),json);assert.deepEqual(fs.readFileSync(store.mdFile),md);
});
test('schedule flush failure preserves committed record and safe cause without replaying create',async t=>{
 const dir=fixture(t),store=new CronStore(dir),events=[],session={id:'s',ownEvents:()=>events.slice(),append:(type,data)=>events.push({type,data})},agent={session};let fail=true,flushes=0;
 const sch=new CronScheduler({store,timeZone:'UTC',ctx:{sessions:{flush:async()=>{flushes++;if(fail)throw Object.assign(Error('/PRIVATE prompt'),{code:'EIO'});}}}});
 await assert.rejects(sch.create(agent,'n','p','每5分钟'),{code:'CRON_SCHEDULE_FAILED',errno:'EIO',committed:true});assert.equal(store.load().length,1);assert.equal(events.length,1);fail=false;await sch.reconcile(agent);assert.equal(events.length,1);assert.equal(flushes,2);
});
test('append and delete flush failures disclose partial JSON and reconciliation repairs empty-store orphans',async t=>{
 const dir=fixture(t),store=new CronStore(dir),events=[];let appendFail=true,flushFail=false,flushed=0;
 const session={id:'s',ownEvents:()=>events.slice(),append(type,data){if(appendFail)throw Object.assign(Error('/PRIVATE'),{code:'EIO'});events.push({type,data})}},agent={session};
 const sch=new CronScheduler({store,timeZone:'UTC',ctx:{sessions:{flush:async()=>{if(flushFail)throw Object.assign(Error('/PRIVATE'),{code:'EACCES'});flushed++;}},logger:{warn(){}}}});
 await assert.rejects(sch.create(agent,'n','p','每5分钟'),{code:'CRON_SCHEDULE_FAILED',errno:'EIO',committed:true});assert.equal(store.load().length,1);appendFail=false;await sch.reconcile(agent);assert.equal(events.length,1);
 appendFail=true;await assert.rejects(sch.remove(agent,'c1'),{code:'CRON_SCHEDULE_FAILED',committed:true});assert.equal(store.load().length,0);appendFail=false;await sch.reconcile(agent);assert.equal(events.at(-1).data.operation,'delete');
 await sch.create(agent,'next','p','每5分钟');flushFail=true;await assert.rejects(sch.remove(agent,'c2'),{code:'CRON_SCHEDULE_FAILED',errno:'EACCES',committed:true});flushFail=false;const count=events.length;await sch.reconcile(agent);assert.equal(events.length,count);assert.equal(flushed,4);
});
test('writer contention times out safely without blocking the same-process event loop or losing JSON',async t=>{
 const dir=fixture(t),store=new CronStore(dir);store.save([job()]);const bytes=fs.readFileSync(store.file),db=new DatabaseSync(join(dir,'.writer.sqlite'));db.exec('BEGIN IMMEDIATE');t.after(()=>db.close());let ticked=false;setImmediate(()=>ticked=true);
 await assert.rejects(store.transaction(()=>store.save([job(),job(2)])),{code:'CRON_WRITE_FAILED',errno:'ERR_SQLITE_ERROR',committed:false});assert.equal(ticked,true);assert.deepEqual(fs.readFileSync(store.file),bytes);
});

for(const [name,replacement] of [
 ['write',original=>(fd,text,...args)=>{original(fd,text.slice(0,5),...args);throw Object.assign(Error('PRIVATE'),{code:'ENOSPC'});}],
 ['sync',()=>()=>{throw Object.assign(Error('PRIVATE'),{code:'EIO'});}],
 ['rename',original=>(source,target)=>original(source,join(target,'impossible'))],
])test(`cron ${name} failure preserves authoritative bytes and cleans only its temporary`,t=>{
 const dir=fixture(t),store=new CronStore(dir);store.save([job()]);const before=fs.readFileSync(store.file);
 const fn=name==='write'?'writeFileSync':name==='sync'?'fsyncSync':'renameSync';patch(fn,replacement(fs[fn]));
 assert.throws(()=>store.save([job(),job(2)]),e=>e.code==='CRON_WRITE_FAILED'&&e.committed===false&&!e.message.includes('PRIVATE'));
 assert.deepEqual(fs.readFileSync(store.file),before);assert.equal(fs.readdirSync(dir).filter(n=>n.endsWith('.tmp')).length,0);
});
test('exclusive temporary collision never deletes the foreign temporary',t=>{
 const dir=fixture(t),target=join(dir,'json'),original=fs.openSync;let foreign;
 patch('openSync',(file,flags,...rest)=>{if(flags==='wx'){foreign=file;const fd=original(file,'w');fs.writeFileSync(fd,'foreign');fs.closeSync(fd);}return original(file,flags,...rest);});
 assert.throws(()=>atomicText(target,'new','CRON'),{code:'CRON_WRITE_FAILED',errno:'EEXIST',committed:false});
 assert.equal(fs.readFileSync(foreign,'utf8'),'foreign');
});
test('directory sync failure reports already committed JSON and allows markdown rebuild',t=>{
 const dir=fixture(t),store=new GoalsStore(dir);store.create('one');const original=fs.fsyncSync;let n=0;
 patch('fsyncSync',fd=>{if(++n===2)throw Object.assign(Error('PRIVATE'),{code:'EIO'});return original(fd);});
 assert.throws(()=>store.logProgress('g1','committed'),{code:'GOALS_WRITE_FAILED',errno:'EIO',committed:true});
 assert.equal(store.load().goals[0].progress.length,1);mock.restoreAll();syncBuiltinESMExports();store.rebuildMarkdown();
 assert.match(fs.readFileSync(store.mdFile,'utf8'),/committed/);
});
test('markdown rename failure leaves JSON committed and list repairs projection without replay',t=>{
 const dir=fixture(t),store=new GoalsStore(dir);store.create('one');const old=fs.readFileSync(store.mdFile,'utf8'),original=fs.renameSync;
 patch('renameSync',(from,to)=>original(from,to.endsWith('.md')?join(to,'impossible'):to));
 assert.throws(()=>store.logProgress('g1','only once'),{code:'GOALS_PROJECTION_FAILED',errno:'ENOTDIR',committed:true});
 assert.equal(store.load().goals[0].progress.length,1);assert.equal(fs.readFileSync(store.mdFile,'utf8'),old);
 assert.equal(new GoalsStore(dir).listGoals().length,1);mock.restoreAll();syncBuiltinESMExports();new GoalsStore(dir).rebuildMarkdown();
 assert.match(fs.readFileSync(store.mdFile,'utf8'),/only once/);assert.equal(store.load().goals[0].progress.length,1);
});
test('durable schema and invalid UTF8 failures never become empty stores',t=>{
 const dir=fixture(t),cron=new CronStore(dir),goals=new GoalsStore(dir);
 for(const [store,values,prefix] of [[cron,[{},[{}],[job(),job()],{version:1,seq:0,jobs:[job()]}],'CRON'],[goals,[[],{goals:[{}],seq:1},{goals:[],seq:-1},{goals:[],seq:1.5}],'GOALS']]){
  for(const value of values){const bytes=JSON.stringify(value);fs.writeFileSync(store.file,bytes);assert.throws(()=>store.load(),{code:prefix+'_INVALID_DATA'});assert.equal(fs.readFileSync(store.file,'utf8'),bytes);}
  fs.writeFileSync(store.file,Buffer.from([0x22,0xff,0x22]));assert.throws(()=>store.load(),{code:prefix+'_INVALID_DATA'});
 }
});
test('missing leaf is empty but missing or non-directory parent rejects',t=>{
 const dir=fixture(t),store=new CronStore(join(dir,'data'));assert.deepEqual(store.load(),[]);fs.rmdirSync(join(dir,'data'));
 assert.throws(()=>store.load(),{code:'CRON_READ_FAILED'});fs.writeFileSync(join(dir,'data'),'file');assert.throws(()=>store.load(),{code:'CRON_READ_FAILED'});
});
test('mutations do not recreate a vanished parent and misreport it as first-time empty storage',async t=>{
 const dir=fixture(t),cron=new CronStore(join(dir,'cron')),goals=new GoalsStore(join(dir,'goals'));fs.rmdirSync(join(dir,'cron'));fs.rmdirSync(join(dir,'goals'));
 await assert.rejects(cron.transaction(()=>cron.save([job()])),{code:'CRON_WRITE_FAILED',errno:'ENOENT',committed:false});assert.throws(()=>goals.create('n'),{code:'GOALS_WRITE_FAILED',errno:'ENOENT',committed:false});assert.equal(fs.existsSync(join(dir,'cron')),false);assert.equal(fs.existsSync(join(dir,'goals')),false);
});
test('legacy arrays and goals preserve every record and extension field through updates',t=>{
 const dir=fixture(t),cron=new CronStore(dir),goals=new GoalsStore(dir),legacy=[{...job(7),extension:{kept:true}}];
 fs.writeFileSync(cron.file,JSON.stringify(legacy));cron.save(cron.load());const envelope=JSON.parse(fs.readFileSync(cron.file));assert.equal(envelope.seq,7);assert.deepEqual(envelope.jobs,legacy);
 goals.create('one');const data=goals.load();data.extension='kept';data.goals[0].extension=true;fs.writeFileSync(goals.file,JSON.stringify(data));goals.logProgress('g1','p');assert.equal(goals.load().extension,'kept');assert.equal(goals.load().goals[0].extension,true);
});
test('owning shell integration readers accept both legacy array and current envelope',t=>{
 const dir=fixture(t);fs.mkdirSync(join(dir,'intel-cron'));const script=fs.readFileSync(new URL('./integration.sh',import.meta.url),'utf8');
 const readers=script.match(/data = json\.load\(open\(os\.path\.join\(os\.environ\['DSH_HOME'\], 'intel-cron\/cron\.json'\)\)\)\)\n jobs =[^\n]+/g)??script.match(/data = json\.load[^\n]+\njobs =[^\n]+/g);assert.equal(readers.length,2);
 for(const data of [[job()],{version:1,seq:1,jobs:[job()]}]){fs.writeFileSync(join(dir,'intel-cron','cron.json'),JSON.stringify(data));for(const reader of readers){const result=spawnSync('python3',['-c','import json,os\n'+reader+'\nassert len(jobs)==1 and jobs[0]["id"]=="c1"'],{env:{...process.env,DSH_HOME:dir},encoding:'utf8',timeout:5000});assert.equal(result.error,undefined);assert.equal(result.signal,null);assert.equal(result.status,0,result.stderr);}}
});
test('real Linux EACCES reads and writes retain original JSON', {skip:process.platform!=='linux'||process.getuid()!==0},t=>{
 const dir=fixture(t),store=new CronStore(dir);store.save([job()]);const before=fs.readFileSync(store.file);fs.chmodSync(dir,0o755);fs.chmodSync(store.file,0);
 const code=`import {CronStore} from ${JSON.stringify(new URL('../src/store.js',import.meta.url).href)};import {atomicText} from ${JSON.stringify(new URL('../../shared/json-store.js',import.meta.url).href)};const s=new CronStore(${JSON.stringify(dir)});let r,w;try{s.load()}catch(e){r={code:e.code,errno:e.errno}}try{atomicText(s.file,'[]','CRON')}catch(e){w={code:e.code,errno:e.errno,committed:e.committed}}console.log(JSON.stringify({r,w}));`;
 const result=spawnSync(process.execPath,['--input-type=module','-e',code],{uid:65534,gid:65534,encoding:'utf8',timeout:10000});assert.equal(result.error,undefined);assert.equal(result.signal,null);assert.equal(result.status,0,result.stderr);
 assert.deepEqual(JSON.parse(result.stdout),{r:{code:'CRON_READ_FAILED',errno:'EACCES'},w:{code:'CRON_WRITE_FAILED',errno:'EACCES',committed:false}});assert.deepEqual(fs.readFileSync(store.file),before);
});
test('independent Linux writers retain all goals and cron records',async t=>{
 const dir=fixture(t),goalsDir=join(dir,'goals'),cronDir=join(dir,'cron');fs.mkdirSync(goalsDir);fs.mkdirSync(cronDir);fs.chmodSync(dir,0o755);
 const code=`import {GoalsStore} from ${JSON.stringify(new URL('../../dsh-intelligence-goals/src/goals.js',import.meta.url).href)};import {CronScheduler} from ${JSON.stringify(new URL('../src/scheduler.js',import.meta.url).href)};import {CronStore} from ${JSON.stringify(new URL('../src/store.js',import.meta.url).href)};const g=new GoalsStore(${JSON.stringify(goalsDir)}),s={id:process.argv[1],ownEvents:()=>[],append(){}};const cron=new CronScheduler({store:new CronStore(${JSON.stringify(cronDir)}),timeZone:'UTC',ctx:{sessions:{flush:async()=>new Promise(r=>setTimeout(r,20))}}});process.stdout.write('READY\\n');await new Promise(r=>process.stdin.once('data',r));process.stdin.destroy();for(let n=0;n<5;n++){g.create(s.id+n);await cron.create({session:s},s.id+n,'p','每5分钟')}`;
 const children=['one','two'].map(id=>spawn(process.execPath,['--input-type=module','-e',code,id],{stdio:['pipe','pipe','pipe']}));
 const outcomes=children.map(child=>{let log='';child.stderr.on('data',b=>log+=b);const done=once(child,'close');t.after(async()=>{if(child.exitCode===null)child.kill();await done;});return done.then(([status,signal])=>{assert.equal(signal,null);assert.equal(status,0,log);});});
 await Promise.all(children.map(child=>once(child.stdout,'data')));for(const child of children)child.stdin.end('go');await Promise.all(outcomes);
 const gs=new GoalsStore(goalsDir).load();assert.equal(gs.goals.length,10);assert.equal(gs.seq,10);const jobs=new CronStore(cronDir).load();assert.equal(jobs.length,10);assert.equal(new Set(jobs.map(j=>j.id)).size,10);
});
