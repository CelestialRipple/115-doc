const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');

const script = readFileSync(__dirname + '/moviepilot-browser-download.user.js', 'utf8');
const origin = 'https://moviepilot.example';
function torrent(id = 'resource-1', overrides = {}) {
  const token = `${Math.floor(Date.now() / 1000) + 3600}.${'a'.repeat(64)}`;
  const browser = `/api/v1/plugin/TencentDoc115Library/resources/browser/${id}?token=${token}`;
  return {
    title: 'Test movie (2024)', description: 'Movies · 待保存到媒体库',
    site_name: '腾讯文档115媒体库', enclosure: `magnet:?xt=urn:btih:abc&x.td115=${id}`,
    page_url: `/api/v1/plugin/TencentDoc115Library/resources/save/${id}?token=save#mp115-browser=${new URLSearchParams({url: browser})}`,
    ...overrides,
  };
}
function environment() {
  const opened = [], listeners = {}, dialogs = [];
  class Element {
    constructor(item, interactive = false) { this.item = item; this.interactive = interactive; }
    get textContent() { return this.item.compact ? this.item.site_name[0] : this.item.site_name; }
    closest(selector) { return selector.startsWith('a,') ? (this.interactive ? this : null) : this; }
    querySelector(selector) {
      if (selector.startsWith('[title=')) return selector === `[title="${this.item.site_name}"]` ? this : null;
      return { getAttribute: () => selector.includes('subtitle') ? this.item.title : this.item.description };
    }
  }
  class XHR { open() {} addEventListener() {} }
  class EventSource {
    constructor() { this.listeners = {}; }
    addEventListener(type, listener) { this.listeners[type] = listener; }
    emit(value) { this.listeners.message?.({ data: JSON.stringify(value) }); }
  }
  const document = {
    addEventListener: (type, listener) => { listeners[type] = listener; },
    createElement: type => ({type, children: [], style: {}, append(...nodes) { this.children.push(...nodes); },
      addEventListener() {}, showModal() { dialogs.push(this); }}),
    body: {append() {}},
  };
  const window = { EventSource, open: (...args) => opened.push(args) };
  vm.runInNewContext(script, {window, document, Element, XMLHttpRequest: XHR, URL, URLSearchParams,
    location: {origin, href: origin + '/resource'}, Map, Date});
  const source = new window.EventSource(origin + '/api/v1/search/title/stream');
  function click(item, interactive = false) {
    const event = {type: 'click', target: new Element(item, interactive),
      preventDefault() {this.prevented = true;}, stopImmediatePropagation() {this.stopped = true;}};
    listeners.click(event);
    return event;
  }
  return {source, opened, dialogs, click};
}
test('search result click opens only the signed same-origin browser route', () => {
  const env = environment(), item = torrent();
  env.source.emit({type: 'result', data: [{torrent_info: item}]});
  const event = env.click(item);
  assert.equal(event.stopped, true);
  assert.equal(env.opened.length, 1);
  assert.ok(env.opened[0][0].startsWith(origin + '/api/v1/plugin/TencentDoc115Library/resources/browser/resource-1?'));
  assert.equal(env.opened[0][2], 'noopener,noreferrer');
});
test('PT results and detail/action buttons retain their normal behavior', () => {
  const env = environment(), item = torrent();
  env.source.emit({torrent_info: item});
  assert.equal(env.click({...item, site_name: 'Other PT site'}).stopped, undefined);
  assert.equal(env.click(item, true).stopped, undefined);
  assert.equal(env.opened.length, 0);
});
test('compact list rows identify the plugin through the site title attribute', () => {
  const env = environment(), item = torrent();
  env.source.emit({torrent_info: item});
  assert.equal(env.click({...item, compact: true}).stopped, true);
  assert.equal(env.opened.length, 1);
});
test('different shares with identical titles require explicit selection', () => {
  const env = environment();
  env.source.emit([torrent('one'), torrent('two')]);
  assert.equal(env.click(torrent('one')).stopped, true);
  assert.equal(env.opened.length, 0);
  assert.equal(env.dialogs.length, 1);
  assert.equal(env.dialogs[0].children.filter(node => node.type === 'a').length, 2);
});
test('external redirects and resource mismatches are not accepted', () => {
  for (const fragment of ['https://evil.example/video', '/api/v1/plugin/TencentDoc115Library/resources/browser/other?token=1.abc']) {
    const env = environment(), item = torrent('one');
    item.page_url = item.page_url.split('#')[0] + '#mp115-browser=' + new URLSearchParams({url: fragment});
    env.source.emit({torrent_info: item});
    assert.equal(env.click(item).stopped, undefined);
    assert.equal(env.opened.length, 0);
  }
});
test('native frontend helper consumes the same capability without a downloader call', () => {
  const {openPluginBrowserDownload} = require('./moviepilotBrowserDownload.ts');
  const priorWindow = global.window, priorLocation = global.location;
  const opened = [];
  global.location = {origin};
  global.window = {open: (...args) => opened.push(args)};
  try {
    assert.equal(openPluginBrowserDownload(torrent()), true);
    assert.equal(opened.length, 1);
    assert.equal(openPluginBrowserDownload(torrent('other', {site_name: 'Other PT'})), false);
    assert.equal(opened.length, 1);
  } finally {
    global.window = priorWindow;
    global.location = priorLocation;
  }
});

function aggregateTorrent(site_name = 'PanSou聚合搜索', id = 'a'.repeat(32)) {
  const query = new URLSearchParams({expires: Math.floor(Date.now()/1000)+3600, sig:'b'.repeat(64)});
  const target = `/api/v1/plugin/PanSouAggregate/download/${id}?${query}`;
  return {title:'Movie', description:'PanSou · 115', site_name, enclosure:`pansou://${id}`,
    page_url:`/api/v1/plugin/PanSouAggregate/resource/${id}?${query}#mp-pansou=${new URLSearchParams({url:target})}`};
}
test('PanSou and BT4G native clicks open scoped browser routes without downloader', () => {
  for (const name of ['PanSou聚合搜索', 'BT4G网页搜索', '聚合网页搜索']) {
    const env=environment(), item=aggregateTorrent(name);
    env.source.emit({torrent_info:item});
    assert.equal(env.click(item).stopped,true);
    assert.equal(env.opened.length,1);
    assert.ok(env.opened[0][0].includes('/PanSouAggregate/download/'));
    assert.equal(env.click(item,true).stopped,undefined);
    assert.equal(env.click({...item,compact:true}).stopped,true);
  }
});
test('same-title document and PanSou cards cannot open each other', () => {
  const env=environment(), a=aggregateTorrent(), b=torrent('one',{title:a.title,description:a.description});
  env.source.emit([a,b]);
  env.click(a);
  assert.equal(env.opened.length,1);
  assert.ok(env.opened[0][0].includes('/PanSouAggregate/'));
  assert.equal(env.dialogs.length,0);
});
test('PanSou rejects forged cross-origin, expired and mismatched capabilities', () => {
  for (const target of ['https://evil.example/file',
    '/api/v1/plugin/PanSouAggregate/download/'+ 'c'.repeat(32)+'?expires=9999999999&sig='+'b'.repeat(64),
    '/api/v1/plugin/PanSouAggregate/download/'+ 'a'.repeat(32)+'?expires=1&sig='+'b'.repeat(64)]) {
    const env=environment(),item=aggregateTorrent();
    item.page_url=item.page_url.split('#')[0]+'#mp-pansou='+new URLSearchParams({url:target});
    env.source.emit(item);
    assert.equal(env.click(item).stopped,undefined);
    assert.equal(env.opened.length,0);
  }
});
test('native source helper handles both new aggregate sources', () => {
  const {openPluginBrowserDownload}=require('./moviepilotBrowserDownload.ts');
  const oldWindow=global.window,oldLocation=global.location,opened=[];
  global.window={open:(...a)=>opened.push(a)};global.location={origin};
  try {
    assert.equal(openPluginBrowserDownload(aggregateTorrent()),true);
    assert.equal(openPluginBrowserDownload(aggregateTorrent('BT4G网页搜索')),true);
    assert.equal(opened.length,2);
  } finally {global.window=oldWindow;global.location=oldLocation;}
});
