import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../src/ingestion/controller.ts',import.meta.url),'utf8');
const start=source.indexOf('  let selectedBatch =');
const end=source.indexOf('  async function update(',start);
const select={value:'',disabled:false,options:[],replaceChildren(...options){this.options=options;}};
const state={batch:{id:'b2',queue_id:'q1'},folderQueue:{id:'q1',batches:[
  {id:'b1',ordinal:0,total:100},{id:'b2',ordinal:1,total:25},
]}};
const factory=new Function('$','el','state',`
  ${source.slice(start,end).replaceAll('batch?.','state.batch?.').replaceAll('folderQueue?.','state.folderQueue?.').replaceAll('folderQueue.batches','state.folderQueue.batches').replace('batch?[batch]:[]','state.batch?[state.batch]:[]')}
  return {scope,choose(id){selectedBatch=id;}};
`);
const picker=factory(()=>select,(_tag,_class,text)=>({textContent:text}),state);
assert.deepEqual(picker.scope(),{batch_id:'b2'});
assert.deepEqual(select.options.map(o=>o.value),['','b1','b2']);
picker.choose('b1');
assert.deepEqual(picker.scope(),{batch_id:'b1'});
assert.equal(state.batch.id,'b2');
picker.choose('');
assert.deepEqual(picker.scope(),{queue_id:'q1'});
state.batch={id:'b3',queue_id:'q2'};
state.folderQueue={id:'q2',batches:[{id:'b3',ordinal:0}]};
assert.deepEqual(picker.scope(),{batch_id:'b3'});
assert.deepEqual(select.options.map(o=>o.value),['','b3']);
state.batch=null;state.folderQueue=null;
assert.equal(picker.scope(),null);
assert.equal(select.disabled,true);
console.log('MinerU batch picker switches query scope without changing workbench batch');
