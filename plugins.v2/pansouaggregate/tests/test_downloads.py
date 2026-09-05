import importlib.util
from pathlib import Path
from types import SimpleNamespace

from pansouaggregate.downloads import DownloadService
from pansouaggregate.providers import Resource


def test_offline_download_uses_separate_persistent_catalog_and_preserves_library(
    tmp_path,
):
    # Exercise the real SQLite schema (including foreign keys), not a fake store.
    path = Path(__file__).parents[2] / "tencentdoc115library" / "store.py"
    spec = importlib.util.spec_from_file_location("aggregate_test_catalog", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original_store = module.CatalogStore(tmp_path / "library" / "catalog.db")
    seen = []

    class Resolver:
        store = original_store

        def resolve(self, rid, user_agent=""):
            resource = self.store.get_resource(rid)
            assert resource["share_url"].startswith("magnet:")
            self.store.upsert_offline_playback(
                {
                    "resource_id": rid,
                    "state": "ready",
                    "source_hash": "a" * 40,
                    "pick_code": "pick-code",
                    "expires_at": "2099-01-01",
                }
            )
            seen.append((rid, user_agent))
            return "https://115.example/download"

        def cleanup_offline_cache(self):
            seen.append("cleanup")

    target = SimpleNamespace(_resolver=Resolver())
    item = Resource("Movie", "magnet:?xt=urn:btih:" + "a" * 40, "PanSou", "magnet")
    service = DownloadService(lambda: tmp_path / "aggregate")
    assert (
        service.resolve_offline(target, item, "Actual browser")
        == "https://115.example/download"
    )
    rid = seen[0][0]
    assert target._resolver.store is original_store
    assert original_store.get_resource(rid) is None
    assert original_store.get_offline_playback(rid) is None
    assert service.store.get_offline_playback(rid)["state"] == "ready"
    restarted = DownloadService(lambda: tmp_path / "aggregate")
    assert (
        restarted.resolver(target).store.get_offline_playback(rid)["pick_code"]
        == "pick-code"
    )
    assert seen[0][1] == "Actual browser"
