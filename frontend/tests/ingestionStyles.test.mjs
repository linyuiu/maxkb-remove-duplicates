import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import postcss from 'postcss';

const component = readFileSync(new URL('../src/components/KnowledgeIngestion.vue', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../src/ingestion/style.css', import.meta.url), 'utf8');
// Exercise the transformation used when mounting the shadow-root workbench.
const expression = component.match(/style\.textContent=(styles[^`]+)\+`/)[1];
const transformed = new Function('styles', 'return ' + expression)(styles);
const rules = [];
postcss.parse(transformed).walkRules(rule => rules.push(rule));
assert(rules.some(rule => rule.selector === '.ingestion-root'));
assert(!rules.some(rule => rule.selector === 'body'));
for (const selector of ['.knowledge-dialog-body', '.chunk-body', '.glass-select-option-body']) {
  assert(rules.some(rule => rule.selector === selector), `${selector} must remain intact`);
}
const scrollRule = rules.find(rule => rule.selector === '.knowledge-dialog-body');
assert(scrollRule.nodes.some(node => node.prop === 'overflow-y' && node.value === 'auto'));
assert(scrollRule.nodes.some(node => node.prop === 'min-height' && node.value === '0'));
console.log('Ingestion style scoping preserves modal scrolling and content selectors');
