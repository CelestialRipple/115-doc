import asyncio
import sys
from types import ModuleType, SimpleNamespace

from pansouaggregate.bridge import SearchBridge


def test_legacy_virtual_site_coexists_with_115_and_restores_methods(monkeypatch):
    class ChainBase:
        def search_torrents(self, site, keyword, mtype=None, page=0):
            return ["existing-115-or-pt-result"]

        async def async_search_torrents(self, site, keyword, mtype=None, page=0):
            return ["existing-115-or-pt-result"]

    class SearchChain(ChainBase):
        pass

    document_entries = [{"id": -115000001, "tencentdoc115library_local_indexer": True}]

    class SitesHelper:
        def get_indexers(self):
            return list(document_entries)

        async def async_get_indexers(self):
            return self.get_indexers()

    chain = ModuleType("app.chain")
    chain.ChainBase = ChainBase
    search = ModuleType("app.chain.search")
    search.SearchChain = SearchChain
    sites = ModuleType("app.helper.sites")
    sites.SitesHelper = SitesHelper
    monkeypatch.setitem(sys.modules, "app.chain", chain)
    monkeypatch.setitem(sys.modules, "app.chain.search", search)
    monkeypatch.setitem(sys.modules, "app.helper.sites", sites)
    monkeypatch.setattr("pansouaggregate.bridge.called_from_search", lambda: True)

    async def async_search(*args):
        return ["pansou-result"]

    plugin = SimpleNamespace(
        get_state=lambda: True,
        search_torrents=lambda *a: ["pansou-result"],
        async_search_torrents=async_search,
    )
    original = SitesHelper.get_indexers
    bridge = SearchBridge(plugin)
    assert bridge.install() == "兼容站点搜索入口"
    try:
        entries = SitesHelper().get_indexers()
        assert len(entries) == 2 and entries[0]["id"] == -115000001
        assert SearchChain().search_torrents(entries[0], "Movie") == [
            "existing-115-or-pt-result"
        ]
        assert SearchChain().search_torrents(entries[1], "Movie") == ["pansou-result"]
        assert asyncio.run(
            SearchChain().async_search_torrents(entries[1], "Movie")
        ) == ["pansou-result"]
        assert len(asyncio.run(SitesHelper().async_get_indexers())) == 2
        # A protected/nested host helper may suppress the document fallback.
        # Aggregation must preserve it without reinitializing the running library.
        document_entries.clear()
        monkeypatch.setattr(
            "pansouaggregate.bridge.document_fallback_indexer",
            lambda: {"id": -115000001, "tencentdoc115library_local_indexer": True},
        )
        repaired = asyncio.run(SitesHelper().async_get_indexers())
        assert len(repaired) == 2 and repaired[0]["id"] == -115000001
        document_entries.append(
            {"id": -115000001, "tencentdoc115library_local_indexer": True}
        )
        monkeypatch.setattr("pansouaggregate.bridge.called_from_search", lambda: False)
        assert len(SitesHelper().get_indexers()) == 1
    finally:
        bridge.uninstall()
    assert SitesHelper.get_indexers is original
    assert "search_torrents" not in SearchChain.__dict__


def test_modern_moviepilot_does_not_patch_sites(monkeypatch):
    chain = ModuleType("app.chain")
    chain.ChainBase = type("ChainBase", (), {"search_plugin_torrents": lambda: []})
    monkeypatch.setitem(sys.modules, "app.chain", chain)
    bridge = SearchBridge(SimpleNamespace())
    assert bridge.install() == "原生插件搜索"
    assert bridge.patches == []


def test_protected_inherited_host_methods_restore_across_config_reloads():
    class Protected(type):
        def __delattr__(cls, name):
            if name == "get_indexers":
                raise AttributeError("Cannot delete protected attribute")
            return super().__delattr__(name)

    class Base:
        def get_indexers(self):
            return ["existing"]

    class Helper(Base, metaclass=Protected):
        pass

    original = Helper.get_indexers
    for _ in range(3):
        bridge = SearchBridge(SimpleNamespace())
        bridge._patch(Helper, "get_indexers", lambda self: ["aggregate"])
        assert Helper().get_indexers() == ["aggregate"]
        bridge.uninstall()
        assert Helper.get_indexers is original
        assert Helper().get_indexers() == ["existing"]
