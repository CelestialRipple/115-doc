// ==UserScript==
// @name         MoviePilot BT4G 结果助手
// @namespace    https://github.com/CelestialRipple/115-doc
// @version      0.1.0
// @description  用户完成验证后，将当前 BT4G 页面资源带回 MoviePilot；不读取 Cookie，不自动点击验证码。
// @match        https://bt4gprx.com/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==

(() => {
  'use strict';
  const storageKey = 'mp-pansou-return-context';
  let context;
  try {
    if (location.hash.startsWith('#mp-pansou=')) {
      context = JSON.parse(decodeURIComponent(location.hash.slice('#mp-pansou='.length)));
      const origin = new URL(context.origin);
      if (!['http:', 'https:'].includes(origin.protocol) || origin.origin !== context.origin || !/^[a-zA-Z0-9-]{16,64}$/.test(context.nonce)) return;
      context.expires = Date.now() + 30 * 60 * 1000;
      sessionStorage.setItem(storageKey, JSON.stringify(context));
      history.replaceState(null, '', location.pathname + location.search);
    } else {
      context = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
    }
  } catch { return; }
  if (!context || context.expires < Date.now()) return;

  function collect() {
    if (/just a moment|请稍候/i.test(document.title) || document.querySelector('iframe[src*="challenges.cloudflare.com"]')) {
      throw Error('请先手动完成真人验证，再带回结果。');
    }
    const currentKeyword = new URL(location.href).searchParams.get('q');
    if (currentKeyword && currentKeyword !== context.keyword) throw Error('搜索关键词已改变，请从 MoviePilot 重新打开对应搜索。');
    const items = [], seen = new Set();
    function add(title, url, size=0, seeders=0, page_url='') {
      if (!title || seen.has(url) || items.length >= 200) return;
      seen.add(url); items.push({ title: title.slice(0, 500), url, size, seeders, page_url });
    }
    const detailTitle = document.querySelector('h1.notion-detail-title')?.textContent?.trim();
    if (detailTitle) {
      for (const a of document.querySelectorAll('.notion-btn-group a[href]')) {
        const target = new URL(a.getAttribute('href'), location.origin);
        const match = target.pathname.match(/^\/hash\/([a-f0-9]{40})$/i);
        if (target.hostname === 'downloadtorrentfile.com' && match) {
          const magnet = 'magnet:?' + new URLSearchParams({xt:'urn:btih:'+match[1].toLowerCase(),dn:detailTitle});
          add(detailTitle,magnet,0,0,location.origin+location.pathname);
        }
      }
    }
    for (const a of document.querySelectorAll('a[href^="magnet:?"]')) {
      const url = a.getAttribute('href');
      if (!/urn:btih:(?:[a-f0-9]{40}|[a-z2-7]{32})(?:&|$)/i.test(decodeURIComponent(url))) continue;
      const row = a.closest('.notion-list-item,.result,.search-result,.torrent,.list-group-item,li') || a.parentElement;
      const heading = row?.querySelector('h3,h4,h5,h6');
      const title = heading?.textContent?.trim() || new URL(url).searchParams.get('dn') || a.textContent.trim();
      add(title, url);
    }
    for (const a of document.querySelectorAll('.notion-list-item-title a[href],h3 a[href],h4 a[href],h5 a[href]')) {
      const url = new URL(a.getAttribute('href'), location.origin);
      const row = a.closest('.notion-list-item');
      const total = row?.querySelector('.red-pill')?.textContent || '';
      const match = total.match(/([\d.,]+)\s*(TB|GB|MB|KB|B)\b/i);
      const size = match ? Math.round(Number(match[1].replaceAll(',','')) * 1024 ** ({B:0,KB:1,MB:2,GB:3,TB:4}[match[2].toUpperCase()])) : 0;
      const seeders = Number(row?.querySelector('.notion-seeders')?.textContent || 0);
      if (url.origin === location.origin && /^\/(magnet|hash|torrent)\/[a-zA-Z0-9_-]+\/?$/.test(url.pathname)) add(a.textContent.trim(), url.href, size, seeders);
    }
    if (!items.length) throw Error('当前页没有识别到资源链接。请打开搜索结果页或资源详情页后重试。');
    return { type: 'mp-pansou-results', nonce: context.nonce, keyword: context.keyword, items };
  }
  const box = document.createElement('aside');
  box.style.cssText = 'position:fixed;bottom:20px;right:20px;z-index:2147483647;background:#fff;color:#17263c;padding:16px;border:2px solid #254fa1;border-radius:12px;max-width:380px;font:15px system-ui;box-shadow:0 3px 20px #0003';
  const message = document.createElement('p');
  message.textContent = 'MoviePilot：完成验证后带回当前页结果';
  const send = document.createElement('button');send.textContent = '带回 MoviePilot';
  const copy = document.createElement('button');copy.textContent = '显示可复制 JSON';
  send.onclick = () => {try {const payload=collect();if(!window.opener)throw Error('浏览器隔离了窗口，请使用“显示可复制 JSON”后粘贴到 MoviePilot。');window.opener.postMessage(payload,context.origin);message.textContent='已发送 '+payload.items.length+' 条结果，请回 MoviePilot 查看导入状态。'}catch(error){message.textContent=error.message}};
  copy.onclick = () => {try {const payload=collect();let area=box.querySelector('textarea');if(!area){area=document.createElement('textarea');area.style.cssText='width:100%;height:120px';box.append(area)}area.value=JSON.stringify(payload);area.focus();area.select();message.textContent='复制下面 JSON，在 MoviePilot 聚合搜索页的手动导入处粘贴。'}catch(error){message.textContent=error.message}};
  box.append(message,send,copy);document.body.append(box);
})();
