import sys
from types import ModuleType, SimpleNamespace

from pansouaggregate.providers import Resource, clean_link

BASE = "/api/v1/plugin/PanSouAggregate"
KEY = {"x-pansou-key": "test-private-key"}
MAGNET = "magnet:?xt=urn:btih:" + "a" * 40


def test_every_route_checks_authorization(client):
    assert client.get(BASE + "/ui").status_code == 401
    assert client.get(BASE + "/search?keyword=Movie").status_code == 401
    assert client.get(BASE + "/download/missing").status_code == 401
    assert client.get(BASE + "/resource/missing").status_code == 401
    assert client.post(BASE + "/resource/missing").status_code == 401


def test_independent_provider_failure_cached_and_native_page(
    monkeypatch, plugin, client
):
    calls = []

    def pansou(*args):
        calls.append(1)
        return [Resource("Movie", clean_link(MAGNET), "PanSou", "magnet")]

    monkeypatch.setattr("pansouaggregate.engine.PanSouClient.search", pansou)
    data = client.get(BASE + "/search?keyword=Movie", headers=KEY).json()
    assert len(data["items"]) == 2 and not data["errors"]
    native = plugin.search_torrents({}, "Movie")
    assert len(native) == 2 and len(calls) == 1
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
    assert "http://testserver" not in response.text
    assert 'href="/api/v1/plugin/PanSouAggregate/download/' in response.text
    assert client.get(url.replace(item.id, "b" * 32)).status_code == 401
    assert client.get(url.replace("expires=", "expires=0")).status_code == 401


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


def test_slow_search_does_not_block_resource_open(plugin, monkeypatch):
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
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(plugin._engine.search, "Movie")
        assert started.wait(2)
        try:
            assert pool.submit(plugin._engine.get, item.id).result(timeout=1) is item
        finally:
            release.set()
        assert running.result(timeout=2)[0][0].cloud == "bt4g"


def test_bt4g_is_independent_shortcut_no_server_fetch(plugin, client, monkeypatch):
    def fail(*args):
        raise RuntimeError("secret must not escape")

    monkeypatch.setattr("pansouaggregate.engine.PanSouClient.search", fail)
    data = client.get(BASE + "/search?keyword=A%26B", headers=KEY).json()
    assert len(data["items"]) == 1 and data["items"][0]["cloud"] == "bt4g"
    assert "secret" not in str(data)
    item = data["items"][0]
    for path in (
        item["action_url"],
        item["action_url"].replace("/resource/", "/download/"),
    ):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "https://bt4gprx.com/search?q=A%26B&p=1"
    assert (
        client.post(BASE + "/browser-import", json={}, headers=KEY).status_code == 404
    )


def test_115_download_lists_files_preserves_code_and_user_agent(
    plugin, client, monkeypatch
):
    calls = []
    resolver = SimpleNamespace(
        list_video_files=lambda url: (
            calls.append(url)
            or [
                {"file_id": "1", "file_name": "<episode1>.mkv"},
                {"file_id": "2", "file_name": "episode2.mkv"},
            ]
        ),
        resolve_file_url=lambda *a, **kw: (
            calls.append((a, kw)) or "https://115.example/video"
        ),
    )
    monkeypatch.setattr(
        "pansouaggregate.downloads.library_plugin",
        lambda: SimpleNamespace(_resolver=resolver),
    )
    item = Resource("Series", "https://115.com/s/abc", "PanSou", "115", "abcd")
    plugin._engine._remember([item])
    url = plugin._resource_url(item.id).replace("/resource/", "/download/")
    response = client.get(url)
    assert response.status_code == 200 and "&lt;episode1&gt;" in response.text
    assert "http://testserver" not in response.text
    assert len(calls) == 1 and "password=abcd" in calls[0]
    assert client.get(url + "&file_id=unknown").status_code == 404
    response = client.get(
        url + "&file_id=2", headers={"User-Agent": "Browser-UA"}, follow_redirects=False
    )
    assert (
        response.status_code == 302 and response.headers["cache-control"] == "no-store"
    )
    assert calls[-1][0][1:] == ("2", "Browser-UA")
    assert calls[-1][1] == {"force_refresh": True}


def test_save_type_is_visible_validated_and_forwarded(plugin, client, monkeypatch):
    captured = []
    target = SimpleNamespace(
        import_manual_resources=lambda action: (
            captured.append(action) or SimpleNamespace(message="已提交")
        )
    )
    monkeypatch.setattr("pansouaggregate.library_plugin", lambda: target)
    item = Resource("Movie", "https://115.com/s/abc", "PanSou", "115")
    plugin._engine._remember([item])
    url = plugin._resource_url(item.id)
    page = client.get(url).text
    assert '<select name="media_mode">' in page
    assert 'value="mixed" selected' in page
    for mode in ["movie", "tv", "mixed"]:
        assert (
            client.post(url, content="group=Movies&media_mode=" + mode).status_code
            == 200
        )
        assert captured[-1].media_mode == mode
    assert client.post(url, content="media_mode=invalid").status_code == 400
    assert len(captured) == 3


def test_custom_web_native_entries_redirect_without_library_calls(
    plugin, client, monkeypatch
):
    monkeypatch.setattr("pansouaggregate.engine.PanSouClient.search", lambda *a: [])
    plugin._engine.config["web_searches"] = (
        "Example|https://example.com/search?q={keyword}"
    )
    native = plugin.search_torrents({}, "千与千寻")
    item = next(t for t in native if t.site_name == "聚合网页搜索")
    for url in [
        item.page_url,
        item.page_url.split("#")[0].replace("/resource/", "/download/"),
    ]:
        response = client.get(url, follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"].startswith("https://example.com/search?q=")
    assert client.post(item.page_url, content="group=Movies").status_code == 400
