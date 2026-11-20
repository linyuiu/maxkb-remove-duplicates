import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../src/ingestion/controller.ts',import.meta.url),'utf8');
const markup=readFileSync(new URL('../src/ingestion/index.html',import.meta.url),'utf8');
const kind=source.slice(source.indexOf('function kindOf('),source.indexOf('function filePath('));
const add=source.slice(source.indexOf('function addFiles('),source.indexOf('function showPending('));
const notices=[];
const controller=new Function('notice',`
  let pending=[], uploading=false, resumeRequested=false;
  const localStorage={removeItem(){}};
  function showPending(){}
  ${kind}
  ${add}
  return {addFiles,files:()=>pending};
`)(text=>notices.push(text));
const files=['manual.pdf','report.docx','table.xlsx','legacy.doc','legacy.xls','notes.txt'].map(name=>({name,size:12,lastModified:1}));
controller.addFiles(files);
assert.equal(controller.files().length,5);
controller.addFiles(files);
assert.equal(controller.files().length,5);
assert(notices.some(text=>text.includes('1 个不支持')));
const accept=markup.match(/id="file-input"[^>]+accept="([^"]+)"/)[1];
for(const ext of ['.pdf','.doc','.docx','.xls','.xlsx'])assert(accept.split(',').includes(ext));
for(const removed of ['choose-word','choose-excel','word-input','excel-input']){
  assert(!markup.includes(`id="${removed}"`));
}
assert(markup.includes('id="folder-input"'));
console.log('Unified upload accepts mixed document formats and preserves deduplication');
