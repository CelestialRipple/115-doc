"""Legacy MoviePilot source entry; modern versions use native plugin fan-out."""

import inspect
from threading import Lock

MARKER = "pansouaggregate_indexer"


def called_from_search():
    frame = inspect.currentframe()
    try:
        for _ in range(12):
            frame = frame.f_back if frame else None
            if frame is None:
                break
            name = frame.f_code.co_filename.replace("\\", "/")
            if name.endswith("/app/chain/search.py") or "/app/chain/search/" in name:
                return True
        return False
    finally:
        del frame


def document_fallback_indexer():
    """Preserve the 115 empty-site fallback when nested host wrappers hide its caller."""
    try:
        from .downloads import library_plugin

        target = library_plugin()
        if target.get_state() and target.get_module().get("search_torrents"):
            bridge = getattr(target, "_search_bridge", None)
            if bridge is not None:
                return bridge.local_indexer()
    except (ImportError, AttributeError):
        pass
    except Exception:
        # The document plugin is optional and may be temporarily unavailable.
        pass
    return None


class SearchBridge:
    lock = Lock()

    def __init__(self, plugin):
        self.plugin = plugin
        self.patches = []
        self.active = False

    def install(self):
        try:
            from app.chain.base import ChainBase
        except ImportError:
            from app.chain import ChainBase

        if hasattr(ChainBase, "search_plugin_torrents"):
            return "原生插件搜索"
        from app.chain.search import SearchChain
        from app.helper.sites import SitesHelper

        with self.lock:
            self.active = True
            for name in ("get_indexers", "async_get_indexers"):
                original = getattr(SitesHelper, name, None)
                if original is None:
                    continue

                def add_indexer(items):
                    items = list(items or [])
                    if self.active and self.plugin.get_state() and called_from_search():
                        # Existing PT/document sources retain their own fan-out.
                        # Only repair the empty-source case (possibly containing
                        # our entry from an inner async->sync wrapper already).
                        if not any(not item.get(MARKER) for item in items):
                            document = document_fallback_indexer()
                            if document:
                                items.insert(0, document)
                    if (
                        self.active
                        and self.plugin.get_state()
                        and called_from_search()
                        and not any(item.get(MARKER) for item in items)
                    ):
                        items.append(
                            {
                                "id": -115000002,
                                "name": "PanSou聚合搜索",
                                "domain": "https://pansou.invalid",
                                "public": True,
                                "is_active": True,
                                "pri": 0,
                                "language": "zh",
                                "result_num": 100,
                                MARKER: True,
                            }
                        )
                    return items

                if name.startswith("async_"):

                    async def wrapper(helper, *args, _original=original, **kwargs):
                        value = _original(helper, *args, **kwargs)
                        return add_indexer(
                            await value if inspect.isawaitable(value) else value
                        )
                else:

                    def wrapper(helper, *args, _original=original, **kwargs):
                        return add_indexer(_original(helper, *args, **kwargs))

                self._patch(SitesHelper, name, wrapper)
            # Intercept only our synthetic site before module dispatch. This avoids
            # network requests to fake domains and short-circuit conflicts with 115.
            for name in ("search_torrents", "async_search_torrents"):
                original = getattr(SearchChain, name, None)
                if original is None:
                    continue
                if name.startswith("async_"):

                    async def wrapper(
                        chain, site, keyword, mtype=None, page=0, _original=original
                    ):
                        if (site or {}).get(MARKER):
                            return (
                                await self.plugin.async_search_torrents(
                                    site, keyword, mtype, page
                                )
                                if self.active
                                else []
                            )
                        return await _original(
                            chain, site=site, keyword=keyword, mtype=mtype, page=page
                        )
                else:

                    def wrapper(
                        chain, site, keyword, mtype=None, page=0, _original=original
                    ):
                        if (site or {}).get(MARKER):
                            return (
                                self.plugin.search_torrents(site, keyword, mtype, page)
                                if self.active
                                else []
                            )
                        return _original(
                            chain, site=site, keyword=keyword, mtype=mtype, page=page
                        )

                self._patch(SearchChain, name, wrapper)
        return "兼容站点搜索入口"

    def _patch(self, cls, name, wrapper):
        owned = name in cls.__dict__
        original = getattr(cls, name)
        self.patches.append((cls, name, original, wrapper, owned))
        setattr(cls, name, wrapper)

    def uninstall(self):
        with self.lock:
            self.active = False
            for cls, name, original, wrapper, owned in reversed(self.patches):
                if getattr(cls, name, None) is wrapper:
                    if owned:
                        setattr(cls, name, original)
                    else:
                        try:
                            delattr(cls, name)
                        except AttributeError:
                            # Synology builds protect inherited SitesHelper methods
                            # through their metaclass. Restore via its supported setter.
                            setattr(cls, name, original)
            self.patches.clear()
