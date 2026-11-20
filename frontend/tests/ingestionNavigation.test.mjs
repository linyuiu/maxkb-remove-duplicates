import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../src/ingestion/controller.ts',import.meta.url),'utf8');
const start=source.indexOf('function setPage(page)');
const end=source.indexOf('function navigatePage(page)',start);
const nodes=new Map();
function node(key){
  if(!nodes.has(key))nodes.set(key,{hidden:false,textContent:'',classList:{toggle(){}}});
  return nodes.get(key);
}
const document={body:node('body'),querySelector:node,querySelectorAll:()=>[]};
const location={pathname:'/mineru'};
const navigation=new Function('document','$','window','location',`
  ${source.slice(start,end)}
  return {setPage,currentPage};
`)(document,node,{},location);
assert.equal(navigation.currentPage(),'mineru');
for(const page of ['mineru','workbench','settings','history','sources']){
  navigation.setPage(page);
  assert.equal(node('mineru-records').hidden,page!=='mineru');
  assert.equal(node('.workspace').hidden,page!=='workbench');
}
const markup=readFileSync(new URL('../src/ingestion/index.html',import.meta.url),'utf8');
assert(markup.indexOf('id="mineru-records"')<markup.indexOf('class="workspace"'));
const component=readFileSync(new URL('../src/components/KnowledgeIngestion.vue',import.meta.url),'utf8');
assert(component.includes("mineru:'/mineru'"));
assert(component.includes("'/mineru':'mineru'"));
console.log('Ingestion queue and MinerU records use separate pages');
