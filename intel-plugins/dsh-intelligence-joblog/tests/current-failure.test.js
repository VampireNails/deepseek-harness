import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,readFile,rm,mkdir,symlink} from 'node:fs/promises';
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

test('registered alerts render an empty view for an absent or empty alert file',async()=>{
  const previous=process.env.DSH_HOME,home=await mkdtemp(join(tmpdir(),'joblog-empty-alerts-'));
  const tools=new Map(),services=new Map(),disposers=[];
  try{
    process.env.DSH_HOME=home;
    apply({provide:(name,value)=>services.set(name,value),tools:{register:tool=>{
      tools.set(tool.name,tool);return ()=>tools.delete(tool.name);
    }},effect:factory=>{for(const dispose of factory())disposers.push(dispose);}},{});
    const alerts=tools.get('joblog_alerts'),file=services.get('joblog').alertsFile;
    for(const empty of [undefined,'']){
      if(empty!==undefined)await writeFile(file,empty);
      const value=await alerts.execute({});
      assert.equal(value.text,'(无告警)');
      assert.deepEqual(alerts.output.render({},value),[{type:'text',text:'(无告警)'}]);
    }
  }finally{
    for(const dispose of disposers.reverse())dispose();
    if(previous===undefined)delete process.env.DSH_HOME;else process.env.DSH_HOME=previous;
    await rm(home,{recursive:true,force:true});
  }
});

test('registered alerts reject unreadable storage without rendering no-alert success and recover after repair',async()=>{
  const previous=process.env.DSH_HOME,home=await mkdtemp(join(tmpdir(),'joblog-private-alert-source-'));
  const tools=new Map(),services=new Map(),disposers=[];
  try{
    process.env.DSH_HOME=home;
    apply({provide:(name,value)=>services.set(name,value),tools:{register:tool=>{
      tools.set(tool.name,tool);return ()=>tools.delete(tool.name);
    }},effect:factory=>{for(const dispose of factory())disposers.push(dispose);}},{});
    const alerts=tools.get('joblog_alerts'),file=services.get('joblog').alertsFile;
    await mkdir(file);
    async function rejectedView(){
      let rendered,error;
      try{const value=await alerts.execute({});rendered=alerts.output.render({},value);}
      catch(failure){error=failure;}
      assert.equal(error?.code,'JOBLOG_READ_FAILED');
      assert.equal(error.message,'JOBLOG_READ_FAILED');
      assert.equal(rendered,undefined,'Read failure must not reach the successful no-alert renderer');
      assert.equal(String(error).includes(home),false);
      assert.equal(error.cause,undefined);
    }
    await rejectedView();
    await rm(file,{recursive:true});
    if(process.platform==='linux'){
      await symlink(file,file);
      await rejectedView();
      await rm(file);
    }
    const text='\n## 2026-10-09 10:00:00 [owned_failure] 运行失败\nRECOVERED_FAILURE\n';
    await writeFile(file,text);
    const value=await alerts.execute({});
    assert.equal(value.text,text.trim());
    assert.deepEqual(alerts.output.render({},value),[{type:'text',text:text.trim()}]);
    assert.equal(await readFile(file,'utf8'),text);
  }finally{
    for(const dispose of disposers.reverse())dispose();
    if(previous===undefined)delete process.env.DSH_HOME;else process.env.DSH_HOME=previous;
    await rm(home,{recursive:true,force:true});
  }
});
