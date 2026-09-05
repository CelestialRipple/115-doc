const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync(__dirname + '/bt4g-results.user.js', 'utf8');

function run({ challenge = false, opener = true, currentKeyword = 'Big Buck Bunny', origin = 'http://moviepilot.test', context = true, detail = false } = {}) {
  const sent = [], children = [];
  const nonce = '01234567-abcd-abcd-abcd-012345678901';
  const callback = { origin, nonce, keyword: 'Big Buck Bunny' };
  const location = new URL('https://bt4gprx.com/search?q=' + encodeURIComponent(currentKeyword));
  if (context) location.hash = 'mp-pansou=' + encodeURIComponent(JSON.stringify(callback));
  const link = { textContent: 'Big Buck Bunny both discs', getAttribute: () => '/magnet/kzb8HRBQ62h4eEUGBY6bKvf5ZIzBIqezD', closest: () => ({ querySelector: selector => ({ textContent: selector === '.red-pill' ? '13.69GB' : '2' }) }) };
  function element(tag) {
    const e = { tag, style: {}, children: [], textContent: '', append(...items) { this.children.push(...items); }, querySelector(selector) { return this.children.find(c => c.tag === selector); }, focus() {}, select() {} };
    return e;
  }
  const document = {
    title: challenge ? '请稍候…' : 'Download Big Buck Bunny Torrents - BT4G',
    querySelector: selector => detail && selector === 'h1.notion-detail-title' ? {textContent:'Big Buck Bunny'} : null,
    querySelectorAll: selector => detail ? (selector.includes('.notion-btn-group') ? [{getAttribute:()=> '//downloadtorrentfile.com/hash/6d2d195d2e79fb1719d55ffc1982ff09bc0eaed7?name=Big-Buck-Bunny'}] : []) : selector.includes('.notion-list-item-title') ? [link] : [],
    createElement: element,
    body: { append: e => children.push(e) },
  };
  vm.runInNewContext(code, { URL, URLSearchParams, Date, Set, JSON, Number, Math, decodeURIComponent, location, document, history: { replaceState() {} }, sessionStorage: { setItem() {}, getItem() { return null; } }, window: { opener: opener ? { postMessage: (data, target) => sent.push({ data, target }) } : null } });
  return { sent, box: children[0], callback };
}

test('verified live-layout results return only after user click and to exact MP origin', () => {
  const { sent, box } = run();
  assert.equal(sent.length, 0);
  box.children[1].onclick();
  assert.equal(sent.length, 1);
  assert.equal(sent[0].target, 'http://moviepilot.test');
  assert.equal(sent[0].data.items[0].title, 'Big Buck Bunny both discs');
  assert.equal(sent[0].data.items[0].seeders, 2);
  assert.match(sent[0].data.items[0].url, /^https:\/\/bt4gprx.com\/magnet\//);
});

test('challenge never triggers CAPTCHA click or exports fake results', () => {
  const { sent, box } = run({ challenge: true });
  box.children[1].onclick();
  assert.equal(sent.length, 0);
  assert.match(box.children[0].textContent, /完成真人验证/);
});

test('cross-origin isolation provides copy fallback without remote requests', () => {
  const { sent, box } = run({ opener: false });
  box.children[1].onclick();
  assert.equal(sent.length, 0);
  assert.match(box.children[0].textContent, /复制 JSON/);
  box.children[2].onclick();
  assert.equal(JSON.parse(box.querySelector('textarea').value).items.length, 1);
});

test('changed keyword cannot contaminate original search cache', () => {
  const { sent, box } = run({ currentKeyword: 'Different Movie' });
  box.children[1].onclick();
  assert.equal(sent.length, 0);
  assert.match(box.children[0].textContent, /关键词已改变/);
});

test('no launch context means no helper, and invalid callback schemes are rejected', () => {
  assert.equal(run({ context: false }).box, undefined);
  assert.equal(run({ origin: 'javascript:alert(1)' }).box, undefined);
});

test('real detail-page button reveals hash without fetching external download host', () => {
  const {sent,box}=run({detail:true});
  box.children[1].onclick();
  assert.equal(sent.length,1);
  assert.equal(new URL(sent[0].data.items[0].url).searchParams.get('xt'),'urn:btih:6d2d195d2e79fb1719d55ffc1982ff09bc0eaed7');
});

test('MoviePilot page validates source, origin and nonce before authenticated import', async () => {
  const ui=fs.readFileSync(__dirname+'/../plugins.v2/pansouaggregate/ui.py','utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
  const elements={},requests=[],listeners={},child={};
  const element=()=>({value:'',textContent:'',disabled:false,children:[],append(...items){this.children.push(...items)},replaceChildren(){this.children=[]}});
  const document={getElementById:id=>elements[id] ||= element(),createElement:element};
  vm.runInNewContext(ui,{URL,URLSearchParams,document,location:new URL('http://moviepilot.test/api/v1/plugin/PanSouAggregate/ui?key=private-key'),crypto:{randomUUID:()=> 'test-nonce'},navigator:{},window:{open:()=>child,addEventListener:(type,fn)=>listeners[type]=fn},fetch:async(url,options)=>{requests.push({url,options});return{ok:true,json:async()=>url.includes('browser-import')?{count:1}:{keyword:'Movie',items:[],errors:{},bt4g_url:'https://bt4gprx.com/search?q=Movie'}}}});
  document.getElementById('keyword').value='Movie';elements['search-form'].onsubmit({preventDefault(){}});
  await new Promise(setImmediate);elements.verify.onclick();
  const payload={type:'mp-pansou-results',nonce:'test-nonce',keyword:'Movie',items:[]};
  await listeners.message({source:{},origin:'https://bt4gprx.com',data:payload});
  await listeners.message({source:child,origin:'https://evil.test',data:payload});
  await listeners.message({source:child,origin:'https://bt4gprx.com',data:{...payload,nonce:'wrong'}});
  assert.equal(requests.filter(r=>r.url.includes('browser-import')).length,0);
  await listeners.message({source:child,origin:'https://bt4gprx.com',data:payload});
  const imported=requests.filter(r=>r.url.includes('browser-import'));
  assert.equal(imported.length,1);
  assert.equal(imported[0].options.headers['x-pansou-key'],'private-key');
});
