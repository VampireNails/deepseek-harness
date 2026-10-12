import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {formatSummary} from '../src/aggregator.js';

test('formatted task names retain native, audited, inferred and unknown identity labels',()=>{
 const byTask={};for(const [classification,id,input] of [['native','evolve_studying',200],['legacy-evidence','evolve_dreaming',100],['heuristic','studying',300],['native','future_task',400],['unknown','unknown',10]])byTask[classification+':'+id]={classification,id,input,output:2,sessions:1};
 const text=formatSummary({days:7,sessions:5,skipped:0,byTask,byDay:{'2026-10-12':{input:1010,output:10}},byModel:{}},7);
 assert.equal(text,readFileSync(new URL('./expected/task-summary.txt',import.meta.url),'utf8').trimEnd());
});
