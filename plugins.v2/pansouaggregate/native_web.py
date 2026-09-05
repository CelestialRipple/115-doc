"""Keep browser shortcuts distinct and outside torrent matching in manual searches."""

import inspect
from contextvars import ContextVar
from functools import wraps

MANUAL_SEARCH = ContextVar("pansou_manual_search", default=False)


def web_item(plugin, torrent):
    marker = str(
        (
            torrent.get("enclosure")
            if isinstance(torrent, dict)
            else getattr(torrent, "enclosure", "")
        )
        or ""
    )
    if not marker.startswith("pansou://"):
        return None
    item = plugin._engine.get(marker.removeprefix("pansou://"))
    return item if item and item.cloud in {"web", "bt4g"} else None


def distinct_contexts(plugin, value):
    if isinstance(value, (list, tuple)):
        for child in value:
            distinct_contexts(plugin, child)
    elif isinstance(value, dict):
        if web_item(plugin, value.get("torrent_info")) and isinstance(
            value.get("meta_info"), dict
        ):
            # SSE batches are serialized before yielding from the host method.
            title = value["torrent_info"]["title"]
            value["meta_info"].update(name=title, cn_name=None, en_name=title)
        else:
            for child in value.values():
                distinct_contexts(plugin, child)
    elif web_item(plugin, getattr(value, "torrent_info", None)):
        meta = getattr(value, "meta_info", None)
        if meta is not None:
            # MoviePilot groups cards by parsed name and video specs. These are
            # website actions, so retain the full website-specific display name.
            meta.cn_name = None
            meta.en_name = value.torrent_info.title
    return value


def install_manual_web(bridge, chain_class):
    plugin = bridge.plugin
    for name in (
        "last_search_results",
        "async_last_search_results",
        "search_by_title",
        "search_by_id",
        "async_search_by_title",
        "async_search_by_id",
        "async_search_by_title_stream",
        "async_search_by_id_stream",
    ):
        original = getattr(chain_class, name, None)
        if original is None:
            continue
        if inspect.isasyncgenfunction(original):

            @wraps(original)
            async def wrapper(chain, *args, _original=original, **kwargs):
                iterator = _original(chain, *args, **kwargs)
                try:
                    while True:
                        token = MANUAL_SEARCH.set(bridge.active)
                        try:
                            value = await anext(iterator)
                            if bridge.active:
                                distinct_contexts(plugin, value)
                        except StopAsyncIteration:
                            return
                        finally:
                            MANUAL_SEARCH.reset(token)
                        yield value
                finally:
                    await iterator.aclose()
        elif inspect.iscoroutinefunction(original):

            @wraps(original)
            async def wrapper(chain, *args, _original=original, **kwargs):
                token = MANUAL_SEARCH.set(bridge.active)
                try:
                    value = await _original(chain, *args, **kwargs)
                    return distinct_contexts(plugin, value) if bridge.active else value
                finally:
                    MANUAL_SEARCH.reset(token)
        else:

            @wraps(original)
            def wrapper(chain, *args, _original=original, **kwargs):
                token = MANUAL_SEARCH.set(bridge.active)
                try:
                    value = _original(chain, *args, **kwargs)
                    return distinct_contexts(plugin, value) if bridge.active else value
                finally:
                    MANUAL_SEARCH.reset(token)

        bridge._patch(chain_class, name, wrapper)

    # Legacy MoviePilot uses this shared parser for both normal and SSE TMDB
    # searches. Preserve real torrent filtering and only append our web actions.
    name = "_SearchChain__parse_result"
    original = getattr(chain_class, name, None)
    if original is not None:

        @wraps(original)
        def parse(chain, torrents, mediainfo, *args, **kwargs):
            if not bridge.active or not MANUAL_SEARCH.get():
                return original(chain, torrents, mediainfo, *args, **kwargs)
            shortcuts = [t for t in torrents if web_item(plugin, t)]
            regular = [t for t in torrents if not web_item(plugin, t)]
            result = list(original(chain, regular, mediainfo, *args, **kwargs) or [])
            if shortcuts:
                try:
                    from app.domain.context import Context
                    from app.domain.metainfo import MetaInfo
                except ImportError:
                    from app.core.context import Context
                    from app.core.metainfo import MetaInfo
                for torrent in shortcuts:
                    result.append(
                        Context(
                            torrent_info=torrent,
                            media_info=mediainfo,
                            meta_info=MetaInfo(title=torrent.title),
                            resource_source="search",
                        )
                    )
            return distinct_contexts(plugin, result)

        bridge._patch(chain_class, name, parse)
