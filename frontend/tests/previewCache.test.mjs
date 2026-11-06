import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import ts from 'typescript';
const code = ts.transpileModule(readFileSync(new URL('../src/previewCache.ts', import.meta.url), 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022},
}).outputText;
const {PreviewCache} = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
const cache = new PreviewCache();
cache.set('a', {content: 'before'});
const first = cache.get('a'); first.content = 'edited';
assert.equal(cache.get('a').content, 'before');
cache.set('a', {content: 'confirmed'});
assert.equal(cache.get('a').content, 'confirmed');
const now = Date.now;
try { Date.now = () => now() + 300001; assert.equal(cache.get('a'), undefined); }
finally { Date.now = now; }
for (let i=0; i<51; i++) cache.set(String(i), i);
assert.equal(cache.get('0'), undefined);
assert.equal(cache.get('50'), 50);
cache.clear(); assert.equal(cache.get('50'), undefined);
console.log('Preview cache isolation, update, expiry, capacity and invalidation passed');
