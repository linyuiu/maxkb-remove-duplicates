import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const source=readFileSync(new URL('../src/ingestion/controller.ts',import.meta.url),'utf8');
const eligibility=source.slice(source.indexOf('function canReparse('),source.indexOf('function selectionChanged()'));
const operation=source.slice(source.indexOf('async function reparseFiles('),source.indexOf('$("reparse-selected").addEventListener'));
const files=[{id:'a',name:'A.pdf',kind:'pdf',status:'review'},
  {id:'b',name:'B.pdf',kind:'pdf',status:'failed'}];
const selected=new Set(['a','b']);
let confirmed=true, calls=[], notices=[], cleared=0;
const run=new Function('kindOf','window','batch','selected','api','notice','clearPreview',`
  let reparsing=false, previewId='a';
  function renderBatch() {}
  function applyFileUpdate(file) { Object.assign(batch.files.find(f=>f.id===file.id),file); }
  function connectEvents() {}
  ${eligibility}
  ${operation}
  return {reparseFiles,canReparse};
`)(f=>f.kind,{confirm:()=>confirmed},{id:'batch',files},selected,async path=>{
  calls.push(path);
  if(path.includes('/b/'))throw new Error('读取原文失败');
  return {file:{...files[0],status:'queued'}};
},(...args)=>notices.push(args),()=>cleared++);
assert(!run.canReparse({...files[0],kind:'word'}));
assert(!run.canReparse({...files[0],status:'imported'}));
assert(!run.canReparse({...files[0],operation_active:1}));
confirmed=false;
await run.reparseFiles(files);
assert.equal(calls.length,0);
confirmed=true;
await run.reparseFiles(files);
assert.equal(calls.length,2);
assert.equal(selected.has('a'),false);
assert.equal(selected.has('b'),true);
assert.equal(cleared,1);
assert.match(notices[0][0],/已提交 1 个/);
assert.match(notices[0][0],/B.pdf/);
// After a partial failure, the failed file can be submitted again.
await run.reparseFiles([files[1]]);
assert.equal(calls.length,3);
console.log('Queue PDF reparse supports confirmation, partial failures and retry');
