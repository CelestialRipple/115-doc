import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from .calendar import fetch_calendar
from .metadata import match_metadata, query_title
from .store import Cache


class CalendarEngine:
    def __init__(self, path, proxy=""):
        self.cache = Cache(path)
        self.proxy = proxy
        self.fetch_lock = Lock()
        self.match_lock = Lock()

    def releases(self, month, refresh=False):
        key = "month:" + month
        cached = self.cache.get(key)
        if cached and time.time() - cached[0] < 21600 and not refresh:
            return cached[1], cached[0], ""
        if not self.fetch_lock.acquire(timeout=1):
            return (
                (cached[1], cached[0], "正在更新发行列表")
                if cached
                else ([], 0, "正在更新，请稍后刷新")
            )
        try:
            # Enforce a minimum refresh interval, including manual refresh clicks.
            cached = self.cache.get(key)
            if cached and time.time() - cached[0] < 60:
                return cached[1], cached[0], ""
            items = fetch_calendar(month, self.proxy)
            self.cache.set(key, items)
            return items, time.time(), ""
        except Exception as error:
            message = (
                str(error)
                if isinstance(error, ValueError)
                else "发行来源暂时不可用，请检查网络或代理"
            )
            return (
                (cached[1], cached[0], message + "；当前显示缓存")
                if cached
                else ([], 0, message)
            )
        finally:
            self.fetch_lock.release()

    @staticmethod
    def meta_key(item):
        text = (
            query_title(item)
            + "|"
            + item.get("year", "")
            + "|"
            + item.get("year_end", "")
        )
        return "meta:" + hashlib.sha256(text.encode()).hexdigest()

    def metadata(self, item):
        row = self.cache.get(self.meta_key(item))
        if row and time.time() - row[0] < (
            30 * 86400 if row[1].get("state") == "matched" else 86400
        ):
            return row[1]
        return {"state": "pending"}

    def match_one(self, item):
        previous = self.metadata(item)
        if previous["state"] != "pending":
            return previous
        try:
            result = match_metadata(item)
        except Exception:
            # Network errors are retryable, not durable failed identity matches.
            return {"state": "error", "message": "元数据暂时不可用，可稍后重试"}
        self.cache.set(self.meta_key(item), result)
        return result

    def match(self, month, ids):
        row = self.cache.get("month:" + month)
        if not row:
            return {}
        by_id = {x["id"]: x for x in row[1]}
        items = [by_id[rid] for rid in ids if rid in by_id]
        if not self.match_lock.acquire(timeout=1):
            return {x["id"]: self.metadata(x) for x in items}
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                values = list(pool.map(self.match_one, items))
            return {x["id"]: value for x, value in zip(items, values)}
        finally:
            self.match_lock.release()
