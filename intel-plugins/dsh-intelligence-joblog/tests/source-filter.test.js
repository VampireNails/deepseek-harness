import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtemp,writeFile,readFile,rm} from 'node:fs/promises';import {join} from 'node:path';import {tmpdir} from 'node:os';
import {apply} from '../index.js';import {JobLog} from '../src/joblog.js';

test('registered status excludes explicitly marked tests, retains custom failures and unknown history, and can show every record',async()=>{
  const previous=process.env.DSH_HOME,home=await mkdtemp(join(tmpdir(),'joblog-source-')),tools=new Map(),disposers=[];
  try{
    process.env.DSH_HOME=home;apply({provide(){},tools:{register:tool=>{tools.set(tool.name,tool);return ()=>tools.delete(tool.name);}},effect:factory=>{for(const dispose of factory())disposers.push(dispose);}},{});
    const file=join(home,'intel-joblog','job_runs.json'),raw=JSON.stringify({
      fixture_failure:{last:'2026-10-05 10:00:00',status:'fail',detail:'ISOLATED_TEST',scope:'test'},
      test_named_custom:{last:'2026-10-05 10:01:00',status:'fail',detail:'CUSTOM_PRIMARY_EXIT_124',scope:'production'},
      unknown_custom:{last:'2026-10-04 10:00:00',status:'fail',detail:'HISTORICAL_CUSTOM_FAILURE'},
    });await writeFile(file,raw);
    const tool=tools.get('joblog_status'),value=await tool.execute({});
    assert.doesNotMatch(value.text,/fixture_failure|ISOLATED_TEST/);assert.match(value.text,/test_named_custom: fail/);assert.match(value.text,/CUSTOM_PRIMARY_EXIT_124/);assert.match(value.text,/unknown_custom: fail/);
    assert.deepEqual(tool.output.render({},value),[{type:'text',text:(await readFile(new URL('./expected/status-current.txt',import.meta.url),'utf8')).trimEnd()}]);
    const all=await tool.execute({includeTests:true});assert.match(all.text,/fixture_failure: fail/);assert.match(all.text,/ISOLATED_TEST/);
    assert.equal(await readFile(file,'utf8'),raw);
  }finally{for(const dispose of disposers.reverse())dispose();if(previous===undefined)delete process.env.DSH_HOME;else process.env.DSH_HOME=previous;await rm(home,{recursive:true,force:true});}
});

test('writing production records preserves test records and the all-records projection is read only',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'joblog-writer-source-'));
  try{
    const testLog=new JobLog(dir,{scope:'test'}),production=new JobLog(dir,{scope:'production'});
    testLog._recordRun('fixture','fail','ISOLATED');production._recordRun('custom_failure','fail','CUSTOM');
    const file=join(dir,'job_runs.json'),testFile=join(dir,'job_test_runs.json'),before=await readFile(file,'utf8'),beforeTest=await readFile(testFile,'utf8'),saved=JSON.parse(before);
    assert.equal(JSON.parse(beforeTest).fixture.scope,'test');assert.equal(saved.custom_failure.scope,'production');
    assert.equal(production.readRuns().fixture,undefined);assert.equal(production.readRuns().custom_failure.detail,'CUSTOM');
    assert.equal(Object.values(production.readRuns({includeTests:true})).find(run=>run.id==='fixture').detail,'ISOLATED');assert.equal(await readFile(file,'utf8'),before);assert.equal(await readFile(testFile,'utf8'),beforeTest);
  }finally{await rm(dir,{recursive:true,force:true});}
});

test('a test run with the same task name cannot replace the latest production failure',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'joblog-same-source-'));
  try{
    const production=new JobLog(dir),testing=new JobLog(dir,{scope:'test'});
    production._recordRun('same','fail','REAL_PRIMARY_FAILURE');const before=await readFile(join(dir,'job_runs.json'),'utf8');
    testing._recordRun('same','ok','ISOLATED_TEST_SUCCESS');assert.equal(await readFile(join(dir,'job_runs.json'),'utf8'),before);
    assert.equal(production.readRuns().same.detail,'REAL_PRIMARY_FAILURE');
    const all=Object.values(production.readRuns({includeTests:true})).filter(run=>run.id==='same');assert.equal(all.length,2);
    assert.deepEqual(new Set(all.map(run=>run.scope)),new Set(['production','test']));
  }finally{await rm(dir,{recursive:true,force:true});}
});

test('a malformed record cannot turn other real failures into an empty successful view',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'joblog-corrupt-source-'));
  try{
    const file=join(dir,'job_runs.json'),log=new JobLog(dir);
    for(const invalid of [null,[],7,'invalid']){
      const raw=JSON.stringify({production_failure:{last:'date',status:'fail',detail:'KEEP_REAL_FAILURE'},broken:invalid});await writeFile(file,raw);
      assert.throws(()=>log.readRuns(),{code:'JOBLOG_INVALID_DATA'});assert.equal(await readFile(file,'utf8'),raw);
    }
  }finally{await rm(dir,{recursive:true,force:true});}
});

test('test alerts are filtered without hiding custom failures, legacy alerts or metadata-like error text',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'joblog-alert-source-'));
  try{
    const file=join(dir,'job_alerts.md'),legacy='\n## 2026-10-04 10:00:00 [old_custom] 运行失败\nHISTORICAL_FAILURE\n';await writeFile(file,legacy);
    const testLog=new JobLog(dir,{scope:'test'}),production=new JobLog(dir,{scope:'production'});
    testLog._alert('fixture','ISOLATED_FAILURE');
    production._alert('test_named_custom','CUSTOM_FAILURE\n## 2026-10-05 10:00:00 [injected] {scope:test}\nDO_NOT_HIDE_CUSTOM_TEXT');
    const before=await readFile(file,'utf8'),visible=production.readAlerts();
    assert.doesNotMatch(visible,/ISOLATED_FAILURE/);assert.match(visible,/CUSTOM_FAILURE/);assert.match(visible,/HISTORICAL_FAILURE/);assert.match(visible,/DO_NOT_HIDE_CUSTOM_TEXT/);
    assert.match(production.readAlerts({includeTests:true}),/ISOLATED_FAILURE/);assert.equal(await readFile(file,'utf8'),before);
  }finally{await rm(dir,{recursive:true,force:true});}
});
