// ==UserScript==
// @name         MoviePilot 聚合资源浏览器下载
// @namespace    https://github.com/CelestialRipple/115-doc
// @version      0.14.0
// @description  点击腾讯文档115搜索结果后使用浏览器直链下载，不创建下载器任务
// @match        http://*/*
// @match        https://*/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

(() => {
  'use strict';
  // 安装后应在脚本管理器中将匹配范围限制为自己的 MoviePilot 地址。
  // 只读取同源搜索响应，不读取登录 Token，不发送额外网络请求。
  if (window.__mpAggregateBrowserAdapter) return;
  window.__mpAggregateBrowserAdapter = true;
  const pluginNames = ['腾讯文档115媒体库', 'PanSou聚合搜索', 'BT4G网页搜索'];
  const pluginName = '腾讯文档115媒体库';
  const prefix = '/api/v1/plugin/TencentDoc115Library/resources/browser/';
  const records = new Map();
  function browserUrl(torrent, origin = location.origin) {
    try {
      if (['PanSou聚合搜索', 'BT4G网页搜索'].includes(torrent?.site_name)) {
        const marker = /^pansou:\/\/([a-f0-9]{32})$/.exec(torrent.enclosure || '');
        if (!marker) return '';
        const detail = new URL(torrent.page_url, origin);
        if (detail.origin !== origin || detail.pathname !== '/api/v1/plugin/PanSouAggregate/resource/' + marker[1]
            || !detail.hash.startsWith('#mp-pansou=')) return '';
        const raw = new URLSearchParams(detail.hash.slice('#mp-pansou='.length)).get('url');
        if (!raw) return '';
        const target = new URL(raw, origin);
        if (target.origin !== origin || target.pathname !== '/api/v1/plugin/PanSouAggregate/download/' + marker[1]
            || !/^[a-f0-9]{64}$/.test(target.searchParams.get('sig') || '')
            || !/^\d{1,12}$/.test(target.searchParams.get('expires') || '')
            || Number(target.searchParams.get('expires')) * 1000 <= Date.now()) return '';
        return target.href;
      }
      if (torrent?.site_name !== pluginName) return '';
      const marker = new URL(torrent.enclosure);
      const id = marker.searchParams.get('x.td115');
      if (marker.protocol !== 'magnet:' || !id) return '';
      const detail = new URL(torrent.page_url, origin);
      if (detail.origin !== origin || !detail.hash.startsWith('#mp115-browser=')) return '';
      const fragment = new URLSearchParams(detail.hash.slice('#mp115-browser='.length));
      const target = new URL(fragment.get('url'), origin);
      if (target.origin !== origin || target.pathname !== prefix + encodeURIComponent(id)) return '';
      const token = target.searchParams.get('token') || '';
      if (!/^\d{1,12}\.[a-f0-9]{64}$/.test(token) || Number(token.split('.')[0]) * 1000 <= Date.now()) return '';
      return target.href;
    } catch { return ''; }
  }
  function collect(value, depth = 0) {
    if (!value || typeof value !== 'object' || depth > 12) return;
    const torrent = value.torrent_info || value;
    const url = browserUrl(torrent);
    if (url) {
      records.set(torrent.enclosure, { title: torrent.title, description: torrent.description || '', url, torrent });
      while (records.size > 2000) records.delete(records.keys().next().value);
    }
    if (Array.isArray(value)) value.forEach(item => collect(item, depth + 1));
    else Object.values(value).forEach(item => collect(item, depth + 1));
  }
  function isSearchUrl(url) {
    try {
      const target = new URL(url, location.href);
      return target.origin === location.origin && target.pathname.startsWith('/api/v1/search/');
    } catch { return false; }
  }
  function consume(text) { try { collect(JSON.parse(text)); } catch {} }

  const originalOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    if (isSearchUrl(url)) this.addEventListener('load', () => {
      try {
        if (this.responseType === 'json') collect(this.response);
        else if (!this.responseType || this.responseType === 'text') consume(this.responseText);
      } catch {}
    }, { once: true });
    return originalOpen.call(this, method, url, ...rest);
  };
  if (window.EventSource) {
    const NativeEventSource = window.EventSource;
    window.EventSource = new Proxy(NativeEventSource, {
      construct(Target, args) {
        const source = new Target(...args);
        if (isSearchUrl(args[0])) source.addEventListener('message', event => consume(event.data));
        return source;
      },
    });
  }
  if (window.fetch) {
    const originalFetch = window.fetch;
    window.fetch = async function(input, init) {
      const response = await originalFetch.call(this, input, init);
      if (isSearchUrl(typeof input === 'string' || input instanceof URL ? input : input.url)
          && response.headers.get('content-type')?.includes('application/json')) {
        response.clone().json().then(value => collect(value)).catch(() => {});
      }
      return response;
    };
  }
  function selectResource(items) {
    // 同名不同分享绝不能猜测；让用户从本次真实搜索结果中选取。
    const dialog = document.createElement('dialog');
    const heading = document.createElement('h3');
    heading.textContent = '选择资源';
    dialog.append(heading);
    items.forEach((item, index) => {
      const link = document.createElement('a');
      link.textContent = `${index + 1}. ${item.title} · ${item.description}`;
      link.href = item.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.style.cssText = 'display:block;padding:12px';
      dialog.append(link);
    });
    const close = document.createElement('button');
    close.textContent = '关闭';
    close.onclick = () => dialog.close();
    dialog.append(close);
    dialog.addEventListener('close', () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
  }
  function intercept(event) {
    if (!(event.target instanceof Element)) return;
    if (event.type === 'keydown' && event.key !== 'Enter') return;
    if (event.target.closest('a,button,input,select,textarea')) return;
    const card = event.target.closest('.torrent-card,.torrent-item');
    if (!card) return;
    const siteName = pluginNames.find(name => card.textContent.includes(name) || card.querySelector(`[title="${name}"]`));
    if (!siteName) return;
    const title = card.querySelector('.text-subtitle-2[title]')?.getAttribute('title');
    const candidates = [...records.values()].filter(item => item.title === title && item.torrent.site_name === siteName && browserUrl(item.torrent));
    // 没有精确匹配时不劫持；插件默认也会拒绝创建旧下载器任务。
    if (!candidates.length) return;
    const description = card.querySelector('.text-body-2[title]')?.getAttribute('title');
    const exact = candidates.filter(item => item.description === description);
    const choices = exact.length ? exact : candidates;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (choices.length === 1) window.open(choices[0].url, '_blank', 'noopener,noreferrer');
    else selectResource(choices);
  }
  document.addEventListener('click', intercept, true);
  document.addEventListener('keydown', intercept, true);
})();
