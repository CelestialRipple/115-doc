// MoviePilot subscription page entry. No credentials are read here.
(() => {
  'use strict';
  if (window.__mpBlurayEntry) return;
  window.__mpBlurayEntry = true;
  function update() {
    const existing = document.getElementById('mp-bluray-calendar-entry');
    if (!/^\/subscribe(?:\/|$)/.test(location.pathname)) { existing?.remove(); return; }
    const main = document.querySelector('main.v-main');
    if (!main || existing) return;
    const bar = document.createElement('div');
    bar.id = 'mp-bluray-calendar-entry';
    bar.style.cssText = 'padding:12px 24px 0;display:flex;justify-content:flex-end';
    const link = document.createElement('a');
    link.href = '/api/v1/plugin/BlurayReleaseCalendar/ui';
    link.target = '_blank'; link.rel = 'noopener noreferrer';
    link.textContent = '近期蓝光发行 ↗';
    link.style.cssText = 'font:500 14px system-ui;padding:9px 16px;border-radius:8px;background:#315bd6;color:#fff;text-decoration:none';
    bar.append(link); main.prepend(bar);
  }
  function start() {
    let pending = false;
    new MutationObserver(() => {
      if (pending) return;
      pending = true;
      requestAnimationFrame(() => { pending = false; update(); });
    }).observe(document.body, {childList: true, subtree: true});
    window.addEventListener('popstate', update);
    update();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once: true});
  else start();
})();
