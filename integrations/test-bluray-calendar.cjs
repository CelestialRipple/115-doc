const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('subscription entry follows navigation and never duplicates', () => {
  let current = null, observer;
  const main = { prepend(node) { current = node; } };
  const document = {
    readyState: 'complete', body: {},
    getElementById() { return current; },
    querySelector(selector) { assert.equal(selector, 'main.v-main'); return main; },
    createElement(tag) { return {tag, style: {}, children: [], append(node) {this.children.push(node);}, remove() {current=null;}}; }
  };
  const context = {document, location:{pathname:'/subscribe/movie'}, window:{addEventListener(){}}, requestAnimationFrame(fn){fn();}, MutationObserver:class {constructor(fn){observer=fn;} observe(){}}};
  vm.runInNewContext(fs.readFileSync(__dirname+'/moviepilot-bluray-calendar.js','utf8'), context);
  assert.equal(current.children[0].href,'/api/v1/plugin/BlurayReleaseCalendar/ui');
  assert.equal(current.children[0].target,'_blank');
  const first=current; observer(); assert.equal(current,first);
  context.location.pathname='/resource'; observer(); assert.equal(current,null);
  context.location.pathname='/subscribe/tv'; observer(); assert.ok(current);
});
