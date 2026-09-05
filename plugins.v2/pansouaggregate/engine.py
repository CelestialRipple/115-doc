"""In-memory search cache and independent provider failure reporting."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock, RLock

from .providers import BT4GClient, PanSouClient, ProviderError, dedupe


class SearchEngine:
    def __init__(self, config):
        self.config = dict(config)
        self.lock = RLock()
        self.search_lock = Lock()
        self.cache = {}
        self.imported = {}
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

    def import_browser(self, keyword, items):
        with self.lock:
            key = self.key(keyword)
            old = self.imported.get(key)
            previous = old[1] if old and time.monotonic() - old[0] < 1800 else []
            resolved_pages = {item.page_url for item in items if item.cloud == "magnet"}
            items = dedupe(
                [
                    *items,
                    *(item for item in previous if item.url not in resolved_pages),
                ],
                self.config["limit"],
            )
            self.imported[key] = (time.monotonic(), items)
            while len(self.imported) > 50:
                self.imported.pop(next(iter(self.imported)))
            self.cache.pop(key, None)
            self._remember(items)
            return len(items)

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
            imported = self.imported.get(key)
            browser_items = (
                imported[1]
                if self.config.get("bt4g_enabled")
                and imported
                and now - imported[0] < 1800
                else []
            )
        # Network calls never hold the resource/cache lock: opening a result or
        # importing a verified browser page must remain responsive during searches.
        providers = {}
        if self.config.get("pansou_enabled"):
            providers["PanSou"] = PanSouClient(self.config)
        if self.config.get("bt4g_enabled") and not browser_items:
            providers["BT4G"] = BT4GClient(self.config)
        items, errors = list(browser_items), {}
        with ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="pansou-search"
        ) as pool:
            futures = {
                name: pool.submit(client.search, keyword)
                for name, client in providers.items()
            }
            for name, future in futures.items():
                try:
                    items.extend(future.result())
                except ProviderError as error:
                    errors[name] = str(error)
                except Exception:
                    # requests exceptions may contain authorization URLs or
                    # share keys. Never expose their raw text in UI or logs.
                    errors[name] = "请求失败，请检查网络、服务地址和认证配置"
        with self.lock:
            if self.stopped:
                return [], {}
            imported = self.imported.get(key)
            if (
                self.config.get("bt4g_enabled")
                and imported
                and time.monotonic() - imported[0] < 1800
            ):
                items = [*imported[1], *items]
                errors.pop("BT4G", None)
            items = dedupe(items, self.config["limit"])
            self.errors = errors
            self.cache[key] = (time.monotonic(), items, errors)
            while len(self.cache) > 50:
                self.cache.pop(next(iter(self.cache)))
            self._remember(items)
            return list(items), dict(errors)

    def stop(self):
        self.stopped = True
