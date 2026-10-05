import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,readFile,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {apply} from '../index.js';

test('registered JobLog tools report current runner failure even when legacy jobs.log is unchanged',async()=>{
  const previous=process.env.DSH_HOME,home=await mkdtemp(join(tmpdir(),'joblog-current-failure-'));
  const tools=new Map(),services=new Map(),disposers=[];
  try{
    process.env.DSH_HOME=home;
    apply({provide:(name,value)=>services.set(name,value),tools:{register:tool=>{
      tools.set(tool.name,tool);return ()=>tools.delete(tool.name);
    }},effect:factory=>{for(const dispose of factory())disposers.push(dispose);}},{});
    const log=services.get('joblog'),legacy=join(log.dir,'jobs.log');
    await writeFile(legacy,'old legacy log\n');
    // The actual CLI calls these methods for its primary exit; it does not append jobs.log.
    log._recordRun('controlled_failure','fail','FIXTURE_PRIMARY_EXIT_124');
    log._alert('controlled_failure','FIXTURE_PRIMARY_EXIT_124');
    const status=tools.get('joblog_status'),alerts=tools.get('joblog_alerts');
    const statusValue=await status.execute({}),alertValue=await alerts.execute({});
    assert.match(status.output.render({},statusValue)[0].text,/controlled_failure: fail/);
    assert.match(statusValue.text,/FIXTURE_PRIMARY_EXIT_124/);
    assert.match(alerts.output.render({},alertValue)[0].text,/controlled_failure/);
    assert.match(alertValue.text,/FIXTURE_PRIMARY_EXIT_124/);
    assert.equal(await readFile(legacy,'utf8'),'old legacy log\n');
  }finally{
    for(const dispose of disposers.reverse())dispose();
    if(previous===undefined)delete process.env.DSH_HOME;else process.env.DSH_HOME=previous;
    await rm(home,{recursive:true,force:true});
  }
});
