import sys
from types import ModuleType, SimpleNamespace

from pansouaggregate.providers import ChallengeRequired, Resource, clean_link

BASE = "/api/v1/plugin/PanSouAggregate"
KEY = {"x-pansou-key": "test-private-key"}
MAGNET = "magnet:?xt=urn:btih:" + "a" * 40


def test_every_route_checks_authorization(client):
    assert client.get(BASE + "/ui").status_code == 401
    assert client.get(BASE + "/search?keyword=Movie").status_code == 401
    assert client.post(BASE + "/browser-import", json={}).status_code == 401
    assert client.get(BASE + "/resource/missing").status_code == 401
    assert client.post(BASE + "/resource/missing").status_code == 401


def test_independent_provider_failure_cached_and_native_page(
    monkeypatch, plugin, client
):
    calls = []

    def pansou(*args):
        calls.append(1)
        return [Resource("Movie", clean_link(MAGNET), "PanSou", "magnet")]

    def bt4g(*args):
        raise ChallengeRequired("需要真人验证")

    monkeypatch.setattr("pansouaggregate.engine.PanSouClient.search", pansou)
    monkeypatch.setattr("pansouaggregate.engine.BT4GClient.search", bt4g)
    data = client.get(BASE + "/search?keyword=Movie", headers=KEY).json()
    assert len(data["items"]) == 1 and "BT4G" in data["errors"]
    native = plugin.search_torrents({}, "Movie")
    assert len(native) == 1 and len(calls) == 1
    assert native[0].page_url.startswith(BASE + "/resource/")
    assert plugin.search_torrents({"id": 5}, "Movie") == []
    assert plugin.search_torrents({}, "Movie", page=1) == []


def test_resource_signature_scoped_and_html_escaped(plugin, client):
    item = Resource(
        "<script>alert(1)</script>",
        "https://115.com/s/abc",
        "PanSou",
        "115",
        '"<unsafe>',
    )
    plugin._engine._remember([item])
    url = plugin._resource_url(item.id)
    response = client.get(url)
    assert response.status_code == 200
    assert "<script>alert" not in response.text
    assert "&lt;unsafe&gt;" in response.text
    assert response.headers["referrer-policy"] == "no-referrer"
    assert client.get(url.replace(item.id, "b" * 32)).status_code == 401
    assert client.get(url.replace("expires=", "expires=0")).status_code == 401


def test_browser_import_only_allowed_origin_and_magnets(plugin, client):
    data = {
        "keyword": "Movie",
        "items": [
            {"title": "unsafe", "url": "javascript:alert(1)"},
            {"title": "other", "url": "https://evil.test/magnet/id"},
        ],
    }
    assert (
        client.post(BASE + "/browser-import", json=data, headers=KEY).status_code == 400
    )
    data["items"] += [{"title": "Movie", "url": MAGNET}]
    assert (
        client.post(BASE + "/browser-import", json=data, headers=KEY).json()["count"]
        == 1
    )
    plugin._config["pansou_enabled"] = False
    plugin._engine.config["pansou_enabled"] = False
    items, errors = plugin._engine.search("Movie")
    assert len(items) == 1 and not errors
    assert (
        client.post(
            BASE + "/browser-import", content=b"x" * (256 * 1024 + 1), headers=KEY
        ).status_code
        == 413
    )


def test_library_import_preserves_code_and_uses_mixed_mode(plugin, client, monkeypatch):
    captured = []
    target = SimpleNamespace(
        import_manual_resources=lambda action: (
            captured.append(action) or SimpleNamespace(success=True, message="已提交")
        )
    )
    manager = ModuleType("app.core.plugin")
    manager.PluginManager = lambda: SimpleNamespace(
        running_plugins={"TencentDoc115Library": target}
    )
    monkeypatch.setitem(sys.modules, "app.core.plugin", manager)
    item = Resource("Movie", "https://115.com/s/abc", "PanSou", "115", "abcd")
    plugin._engine._remember([item])
    url = plugin._resource_url(item.id)
    assert client.post(url, content="group=..%2Fescape").status_code == 400
    assert client.post(url, content="group=Movies").status_code == 200
    assert captured[0].media_mode == "mixed"
    assert "password=abcd" in captured[0].links


def test_disabled_plugin_cannot_be_used(client, plugin):
    plugin._config["enabled"] = False
    assert client.get(BASE + "/ui", headers=KEY).status_code == 409
    assert plugin.search_torrents({}, "Movie") == []


def test_slow_search_does_not_block_resource_open_or_browser_import(
    plugin, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    started, release = Event(), Event()
    item = Resource("Movie", clean_link(MAGNET), "PanSou", "magnet")
    plugin._engine._remember([item])

    def slow(*args):
        started.set()
        assert release.wait(5)
        return []

    monkeypatch.setattr("pansouaggregate.engine.PanSouClient.search", slow)
    monkeypatch.setattr("pansouaggregate.engine.BT4GClient.search", lambda *a: [])
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(plugin._engine.search, "Movie")
        assert started.wait(2)
        try:
            assert pool.submit(plugin._engine.get, item.id).result(timeout=1) is item
            assert (
                pool.submit(plugin._engine.import_browser, "Movie", [item]).result(
                    timeout=1
                )
                == 1
            )
        finally:
            release.set()
        assert running.result(timeout=2)[0][0].id == item.id
