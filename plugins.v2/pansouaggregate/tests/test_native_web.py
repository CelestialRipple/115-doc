import asyncio
import sys
from types import ModuleType, SimpleNamespace

from pansouaggregate.bridge import SearchBridge
from pansouaggregate.native_web import MANUAL_SEARCH, install_manual_web
from pansouaggregate.providers import Resource


def test_manual_tmdb_and_stream_preserve_separate_web_cards_only(monkeypatch, plugin):
    class Context:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Meta:
        def __init__(self, title):
            self.cn_name = title.split(" · ")[0]
            self.en_name = ""

    context = ModuleType("app.core.context")
    context.Context = Context
    meta = ModuleType("app.core.metainfo")
    meta.MetaInfo = Meta
    monkeypatch.setitem(sys.modules, "app.core.context", context)
    monkeypatch.setitem(sys.modules, "app.core.metainfo", meta)
    items = [
        Resource("Movie · A 网页搜索", "https://a.example/search?q=Movie", "A", "web"),
        Resource("Movie · B 网页搜索", "https://b.example/search?q=Movie", "B", "web"),
    ]
    plugin._engine._remember(items)
    web = [SimpleNamespace(enclosure="pansou://" + x.id, title=x.title) for x in items]
    real = SimpleNamespace(enclosure="magnet:?real", title="Movie")
    calls = []

    def original_parse(self, torrents, mediainfo):
        calls.append(list(torrents))
        return [
            Context(torrent_info=t, meta_info=Meta(t.title))
            for t in torrents
            if t is real
        ]

    class Search:
        def search_by_id(self):
            return self._SearchChain__parse_result(
                [real, *web], SimpleNamespace(title="Movie")
            )

        async def async_search_by_id(self):
            return await asyncio.to_thread(self.search_by_id)

        async def async_search_by_id_stream(self):
            yield {"type": "result", "data": await self.async_search_by_id()}

    Search._SearchChain__parse_result = original_parse
    bridge = SearchBridge(plugin)
    bridge.active = True
    install_manual_web(bridge, Search)
    try:
        result = asyncio.run(Search().async_search_by_id())
        assert len(result) == 3 and calls[-1] == [real]
        assert len({c.meta_info.en_name for c in result[1:]}) == 2
        assert not MANUAL_SEARCH.get()

        async def stream():
            async for event in Search().async_search_by_id_stream():
                assert not MANUAL_SEARCH.get()
                assert len(event["data"]) == 3

        asyncio.run(stream())
        assert len(Search()._SearchChain__parse_result([real, *web], None)) == 1
        assert calls[-1] == [real, *web]  # background subscription filtering unchanged
    finally:
        bridge.uninstall()
    assert Search._SearchChain__parse_result is original_parse


def test_serialized_sse_batches_keep_website_names(plugin):
    from pansouaggregate.native_web import distinct_contexts

    items = [
        Resource("Movie · A 网页搜索", "https://a.example/", "A", "web"),
        Resource("Movie · B 网页搜索", "https://b.example/", "B", "web"),
    ]
    plugin._engine._remember(items)
    rows = [
        {
            "torrent_info": {"enclosure": "pansou://" + x.id, "title": x.title},
            "meta_info": {"name": "Movie", "cn_name": "Movie"},
        }
        for x in items
    ]
    real = {
        "torrent_info": {"enclosure": "magnet:?real", "title": "Movie"},
        "meta_info": {"name": "Movie"},
    }
    distinct_contexts(plugin, {"type": "append", "items": [*rows, real]})
    assert {r["meta_info"]["name"] for r in rows} == {x.title for x in items}
    assert real["meta_info"] == {"name": "Movie"}
