import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import ts from 'typescript';
import {ref, watch} from 'vue';
const code=ts.transpileModule(readFileSync(new URL('../src/navigation.ts',import.meta.url),'utf8'),{
  compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022},
}).outputText;
const {parseRoute,routeHash}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'));
for(const page of ['workbench','mineru','sources','history','settings']){
  const route={feature:'ingestion',page};
  assert.deepEqual(parseRoute(routeHash(route)),route);
}
for(const page of ['runs','tasks','sources','operations']){
  const route={feature:'duplicates',page};
  assert.deepEqual(parseRoute(routeHash(route)),route);
}
assert.deepEqual(parseRoute('#/duplicates/runs/abc-123'),{feature:'duplicates',page:'runs',runId:'abc-123'});
assert.deepEqual(parseRoute('#/ingestion/unknown'),{feature:'duplicates',page:'runs'});
const app=readFileSync(new URL('../src/App.vue',import.meta.url),'utf8');
const start=app.indexOf('let applyingRoute =');
const end=app.indexOf('let timer:',start);
const block=ts.transpileModule(app.slice(start,end),{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
const feature=ref('duplicates'),ingestionPage=ref('workbench'),tab=ref('runs'),selectedRun=ref(null);
const operations=ref([]),operationPage=ref(1),user=ref('admin'),ready=ref(true);
const browser={location:{hash:'#/ingestion/mineru'},history:{
  replaceState(_s,_t,hash){browser.location.hash=hash;},
  pushState(_s,_t,hash){browser.location.hash=hash;},
}};
const calls=[];
const api=async path=>{calls.push(path);return path.startsWith('/operations')?[{id:'op'}]:{run:{id:'abc-123'},items:[],total:0};};
const controller=new Function('window','parseRoute','routeHash','watch','feature','ingestionPage','tab','selectedRun','user','ready','api','openRun','operations','operationPage','ElMessage',block+';return {restoreRoute,browserNavigation};')(
  browser,parseRoute,routeHash,watch,feature,ingestionPage,tab,selectedRun,user,ready,api,
  async row=>{selectedRun.value=row;},operations,operationPage,{error:message=>{throw new Error(message);}},
);
await controller.restoreRoute();
assert.equal(feature.value,'ingestion');assert.equal(ingestionPage.value,'mineru');
ingestionPage.value='settings';
assert.equal(browser.location.hash,'#/ingestion/settings');
browser.location.hash='#/ingestion/mineru';await controller.restoreRoute();
assert.equal(ingestionPage.value,'mineru');
browser.location.hash='#/duplicates/operations';await controller.restoreRoute();
assert.equal(tab.value,'operations');assert.equal(operations.value[0].id,'op');
browser.location.hash='#/duplicates/runs/abc-123';await controller.restoreRoute();
assert.equal(selectedRun.value.id,'abc-123');
assert.equal(browser.location.hash,'#/duplicates/runs/abc-123');
assert(calls.some(path=>path.includes('abc-123/pairs')));
console.log('Browser routes restore ingestion pages, operations and scan details');
