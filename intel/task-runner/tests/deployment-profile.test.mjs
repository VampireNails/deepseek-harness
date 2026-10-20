import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,readFile,writeFile,rm,symlink} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {fileURLToPath} from 'node:url';
import {checkProfile,checkDeployment,initializeSources} from '../deployment.mjs';

const linux={skip:process.platform!=='linux'};
async function fixture(t){
  const home=await mkdtemp(join(tmpdir(),'deployment-profile-'));t.after(()=>rm(home,{recursive:true,force:true}));
  const profile=join(home,'profiles','evolve');await mkdir(join(profile,'node_modules'),{recursive:true});
  const bootstrap=await readFile(new URL('../../bootstrap.sh',import.meta.url),'utf8');
  const bundles=/PROFILE_BUNDLES\[evolve\]='([^']+)'/.exec(bootstrap)[1].split(' ');
  for(const name of bundles.filter(name=>!name.startsWith('@'))){
    await symlink(fileURLToPath(new URL('../../../intel-plugins/'+name,import.meta.url)),join(profile,'node_modules',name));
  }
  await writeFile(join(profile,'package.json'),JSON.stringify({private:true,name:'owned-readiness-test',dsh:{profile:{bundles}}}));
  await writeFile(join(profile,'cordis.yml'),'# original installation bytes must remain\n[]\n');
  await writeFile(join(profile,'cordis.patch.yml'),'[]\n');
  const config=JSON.parse(await readFile(new URL('../routes.json',import.meta.url),'utf8'));
  return {home,profile,config};
}

test('installed CLI composes all nine task capabilities without rewriting installation files',linux,async t=>{
  const f=await fixture(t),before=await readFile(join(f.profile,'cordis.yml'));
  assert.equal((await checkProfile(f.home,f.config)).length,9);
  assert.deepEqual(await readFile(join(f.profile,'cordis.yml')),before);
  await initializeSources(f.home);await writeFile(join(f.home,'intel-evolution','HEARTBEAT.md'),'');
  assert.equal((await checkDeployment(f.home,f.config)).state,'inactive');
});

test('composition rejects disabled capabilities, missing dependencies and corrupt personal patches',linux,async t=>{
  const f=await fixture(t),patch=join(f.profile,'cordis.patch.yml');
  await writeFile(patch,JSON.stringify([{id:'dsh-intelligence-goalact',disabled:true}]));
  await assert.rejects(()=>checkProfile(f.home,f.config),{code:'SCHEDULE_PROFILE_CAPABILITY_INACTIVE'});
  await writeFile(patch,'[broken');
  await assert.rejects(()=>checkProfile(f.home,f.config),{code:'SCHEDULE_PROFILE_COMPOSITION_FAILED'});
  await writeFile(patch,'[]\n');await rm(join(f.profile,'node_modules','dsh-intelligence-evolution'));
  await assert.rejects(()=>checkProfile(f.home,f.config),{code:'SCHEDULE_PROFILE_CAPABILITY_INACTIVE'});
});
