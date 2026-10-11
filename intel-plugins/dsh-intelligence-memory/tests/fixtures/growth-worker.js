import { openStore } from '../../src/store.js';
import { FtsRetriever } from '../../src/retriever.js';
const db=openStore(process.argv[2]),r=new FtsRetriever(db,{warnMemoryRows:1});
process.on('message',command=>{
  try {
    if(command==='hold') {db.exec('BEGIN');const result=r.add({text:'并发同来源',sessionId:'shared-source'});process.send({state:'holding',result});}
    if(command==='add') {const result=r.add({text:'并发同来源',sessionId:'shared-source'});process.send({state:'added',result});}
    if(command==='commit') {db.exec('COMMIT');process.send({state:'committed'});}
    if(command==='close') {db.close();process.disconnect();}
  } catch(error) {process.send({state:'failed',code:error.code,errcode:error.errcode});}
});
process.send({state:'ready'});
