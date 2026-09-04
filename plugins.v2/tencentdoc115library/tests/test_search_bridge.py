import asyncio
import sys
from types import ModuleType

from tencentdoc115library.search_bridge import (
    LOCAL_INDEXER_MARKER,
    LocalSearchResults,
    MoviePilotSearchBridge,
)


def _install_sites_helper(monkeypatch, indexers):
    helper_module = ModuleType("app.helper")
    sites_module = ModuleType("app.helper.sites")

    class SitesHelper:
        def get_indexers(self):
            return list(indexers)

        async def async_get_indexers(self):
            return list(indexers)

    sites_module.SitesHelper = SitesHelper
    monkeypatch.setitem(sys.modules, "app.helper", helper_module)
    monkeypatch.setitem(sys.modules, "app.helper.sites", sites_module)
    return SitesHelper


def test_bridge_supplies_local_indexer_and_restores_methods(monkeypatch):
    sites_helper = _install_sites_helper(monkeypatch, [])
    original_sync = sites_helper.get_indexers
    original_async = sites_helper.async_get_indexers
    bridge = MoviePilotSearchBridge(lambda: True, lambda: True)

    assert bridge.install() is True
    sync_indexers = sites_helper().get_indexers()
    async_indexers = asyncio.run(sites_helper().async_get_indexers())
    assert sync_indexers[0][LOCAL_INDEXER_MARKER] is True
    assert async_indexers[0][LOCAL_INDEXER_MARKER] is True

    bridge.uninstall()
    assert sites_helper.get_indexers is original_sync
    assert sites_helper.async_get_indexers is original_async
    assert sites_helper().get_indexers() == []


def test_bridge_preserves_real_indexers(monkeypatch):
    original = [{"id": 1, "name": "real"}]
    sites_helper = _install_sites_helper(monkeypatch, original)
    bridge = MoviePilotSearchBridge(lambda: True, lambda: True)

    assert bridge.install() is True
    assert sites_helper().get_indexers() == original
    assert asyncio.run(sites_helper().async_get_indexers()) == original
    bridge.uninstall()


def test_bridge_stays_outside_search_calls(monkeypatch):
    sites_helper = _install_sites_helper(monkeypatch, [])
    bridge = MoviePilotSearchBridge(lambda: True, lambda: False)

    assert bridge.install() is True
    assert sites_helper().get_indexers() == []
    assert asyncio.run(sites_helper().async_get_indexers()) == []
    bridge.uninstall()


def test_default_checker_detects_moviepilot_search_chain(monkeypatch):
    sites_helper = _install_sites_helper(monkeypatch, [])
    bridge = MoviePilotSearchBridge(lambda: True)
    namespace = {}
    exec(
        compile(
            "async def search_call(helper):\n"
            "    return await helper.async_get_indexers()\n",
            "/opt/moviepilot/app/chain/search.py",
            "exec",
        ),
        namespace,
    )

    assert bridge.install() is True
    assert sites_helper().get_indexers() == []
    indexers = asyncio.run(namespace["search_call"](sites_helper()))
    assert indexers[0][LOCAL_INDEXER_MARKER] is True
    bridge.uninstall()


def test_local_search_results_are_sequence_not_list_or_tuple():
    results = LocalSearchResults(["a", "b"])
    empty = LocalSearchResults([])

    assert list(results) == ["a", "b"]
    assert len(results) == 2
    assert not isinstance(results, (list, tuple))
    assert not empty
