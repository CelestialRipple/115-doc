"""In-memory search cache and independent provider failure reporting."""

import time
from threading import Lock, RLock

from .providers import (
    PanSouClient,
    ProviderError,
    Resource,
    bt4g_search_url,
    dedupe,
    http_url,
)

from .shortcuts import website_shortcuts


class SearchEngine:
    def __init__(self, config):
        self.config = dict(config)
        self.lock = RLock()
        self.search_lock = Lock()
        self.cache = {}
        self.resources = {}
        self.errors = {}
        self.stopped = False

    @staticmethod
    def key(keyword):
        return " ".join(keyword.split()).casefold()

    def _remember(self, items):
        now = time.monotonic()
        self.resources = {
            rid: pair for rid, pair in self.resources.items() if now - pair[0] < 3600
        }
        for item in items:
            self.resources[item.id] = (now, item)
        while len(self.resources) > 2000:
            self.resources.pop(next(iter(self.resources)))

    def get(self, rid):
        with self.lock:
            pair = self.resources.get(rid)
            return pair[1] if pair and time.monotonic() - pair[0] < 3600 else None

    def search(self, keyword, refresh=False):
        # Bound contention as well as HTTP calls; a slow provider must not leave
        # an unbounded queue of MoviePilot search workers waiting on our lock.
        if not self.search_lock.acquire(timeout=2):
            return [], {"聚合搜索": "已有搜索正在进行，请稍后重试"}
        try:
            return self._search_locked(keyword, refresh)
        finally:
            self.search_lock.release()

    def _search_locked(self, keyword, refresh=False):
        with self.lock:
            if self.stopped:
                return [], {}
            key = self.key(keyword)
            now = time.monotonic()
            cached = self.cache.get(key)
            if (
                not refresh
                and cached
                and now - cached[0] < self.config["cache_seconds"]
            ):
                self.errors = dict(cached[2])
                self._remember(cached[1])
                return list(cached[1]), dict(cached[2])
        # Only PanSou is fetched on the server. BT4G is a browser shortcut,
        # independent of PanSou failures and excluded from the resource limit.
        items, errors = [], {}
        if self.config.get("pansou_enabled"):
            try:
                provider = PanSouClient(self.config)
                items = (
                    provider.search(keyword, refresh=True)
                    if refresh
                    else provider.search(keyword)
                )
            except ProviderError as error:
                errors["PanSou"] = str(error)
            except Exception:
                errors["PanSou"] = "请求失败，请检查网络、服务地址和认证配置"
        items = dedupe(
            [item for item in items if item.cloud in {"115", "magnet"}],
            self.config["limit"],
        )
        if self.config.get("bt4g_enabled"):
            base = http_url(self.config.get("bt4g_url", ""))
            if base:
                items.append(
                    Resource(
                        keyword + " · BT4G 网页搜索",
                        bt4g_search_url(base, keyword),
                        "BT4G网页搜索",
                        "bt4g",
                    )
                )
            else:
                errors["BT4G"] = "请配置有效的 BT4G 地址"
        shortcuts, shortcut_errors = website_shortcuts(
            self.config.get("web_searches"), keyword
        )
        items.extend(shortcuts)
        errors.update(shortcut_errors)
        with self.lock:
            if self.stopped:
                return [], {}
            self.errors = errors
            self.cache[key] = (time.monotonic(), items, errors)
            while len(self.cache) > 50:
                self.cache.pop(next(iter(self.cache)))
            self._remember(items)
            return list(items), dict(errors)

    def stop(self):
        self.stopped = True
