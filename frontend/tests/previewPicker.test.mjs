import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const source = readFileSync(new URL('../src/ingestion/controller.ts', import.meta.url), 'utf8');
const start = source.indexOf('function renderPreviewPicker(');
const end = source.indexOf('function setPreviewPickerOpen(', start);
function element() {
  return {value: '', dataset: {}, children: [], classList: {toggle() {}},
    setAttribute() {}, addEventListener() {},
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children = nodes; }};
}
const nodes = new Map();
const $ = id => {
  if (!nodes.has(id)) nodes.set(id, element());
  return nodes.get(id);
};
const files = [
  {id: 'a', name: 'Manual-A.PDF', kind: 'pdf', paragraph_count: 3},
  {id: 'b', name: '使用说明.pdf', kind: 'pdf', paragraph_count: 4},
];
const render = new Function('$', 'el', 'kindOf', 'formatNames', `
  let previewId = 'b', previewFilesSignature = '';
  ${source.slice(start, end)}
  return renderPreviewPicker;
`)($, element, file => file.kind, {pdf: 'PDF'});
render(files);
assert.equal($('preview-file-list').children.length, 2);
$('preview-file-search').value = 'manual';
render(files);
assert.deepEqual($('preview-file-list').children.map(n => n.dataset.fileId), ['a']);
assert.equal($('preview-selected-name').textContent, '使用说明.pdf');
$('preview-file-search').value = '说明';
render(files);
assert.deepEqual($('preview-file-list').children.map(n => n.dataset.fileId), ['b']);
$('preview-file-search').value = 'missing';
render(files);
assert.equal($('preview-file-list').children.length, 0);
assert.equal($('preview-file-empty').hidden, false);
$('preview-file-search').value = '';
render(files);
assert.equal($('preview-file-list').children.length, 2);
console.log('Preview filename search handles case, Chinese, empty results and selection');
