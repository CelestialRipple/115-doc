from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tencentdoc115library.browser_download import BrowserDownloads
from tencentdoc115library.download_marker import build_download_marker
from tencentdoc115library.signing import sign_action, verify_action


def test_action_signatures_bind_purpose_resource_and_expiry(monkeypatch):
    monkeypatch.setattr("tencentdoc115library.signing.time", lambda: 100)
    token = sign_action("private-secret", "browser", "r1", ttl=10)
    assert verify_action("private-secret", token, "browser", "r1")
    assert not verify_action("private-secret", token, "save", "r1")
    assert not verify_action("private-secret", token, "browser", "r2")
    assert not verify_action("different-secret", token, "browser", "r1")
    assert not verify_action(
        "private-secret", "9" * 10000 + "." + "a" * 64, "browser", "r1"
    )
    monkeypatch.setattr("tencentdoc115library.signing.time", lambda: 111)
    assert not verify_action("private-secret", token, "browser", "r1")


def browser_service(media_type="电影"):
    calls = []
    resource = {
        "resource_id": "r1",
        "title": "<script>movie</script>",
        "share_url": "https://115.com/s/example",
        "media_type": media_type,
    }
    files = [
        {"file_id": "1", "file_name": "Show.S01E01.mkv", "file_size": 20},
        {"file_id": "2", "file_name": "Show.S01E02.mkv", "file_size": 30},
    ]
    resolver = SimpleNamespace(
        list_video_files=lambda url: files,
        choose_movie_file=lambda items: max(items, key=lambda item: item["file_size"]),
        resolve_file_url=lambda *args, **kwargs: (
            calls.append((args, kwargs)) or "https://cdn.example/video.mkv"
        ),
    )
    service = BrowserDownloads(
        SimpleNamespace(get_resource=lambda rid: resource),
        resolver,
        lambda: {"action_signing_key": "private-secret"},
    )
    app = FastAPI()
    app.add_api_route(
        "/api/v1/plugin/TencentDoc115Library/resources/browser/{resource_id}",
        service.handle,
        methods=["GET"],
    )
    return service, TestClient(app), calls


def test_browser_movie_returns_302_with_actual_browser_agent():
    service, client, calls = browser_service()
    response = client.get(
        service.url("r1"), headers={"user-agent": "Browser-UA"}, follow_redirects=False
    )
    assert response.status_code == 302
    assert response.headers["location"] == "https://cdn.example/video.mkv"
    assert response.headers["cache-control"] == "no-store"
    assert calls == [
        (("https://115.com/s/example", "2", "Browser-UA"), {"force_refresh": True})
    ]


def test_browser_rejects_playback_token_and_wrong_resource_before_any_resolution():
    service, client, calls = browser_service()
    url = service.url("r1")
    assert client.get(url.replace("/r1?", "/r2?")).status_code == 401
    assert client.get(url.split("?")[0] + "?token=playback-secret").status_code == 401
    assert calls == []


def test_tv_download_selects_only_requested_share_file_and_escapes_title():
    service, client, calls = browser_service("电视剧")
    page = client.get(service.url("r1"))
    assert page.status_code == 200
    assert "&lt;script&gt;" in page.text and "<script>movie" not in page.text
    assert "Show.S01E01.mkv" in page.text and "Show.S01E02.mkv" in page.text
    assert calls == []
    response = client.get(service.url("r1") + "&file_id=1", follow_redirects=False)
    assert response.status_code == 302 and calls[0][0][1] == "1"
    assert client.get(service.url("r1") + "&file_id=not-in-share").status_code == 404


def test_stale_frontend_cannot_create_downloader_job_in_browser_mode(plugin):
    result = plugin._direct_downloader.download(
        build_download_marker("r1"), "/unused", ""
    )
    assert result[1] is None
    assert plugin._store.list_download_tasks(include_organized=True) == []


def test_search_detail_carries_separate_browser_capability(plugin):
    url = plugin._library_save_url("r1")
    parsed = urlsplit(url)
    save = parse_qs(parsed.query)["token"][0]
    browser_url = parse_qs(parsed.fragment.removeprefix("mp115-browser="))["url"][0]
    browser = parse_qs(urlsplit(browser_url).query)["token"][0]
    secret = plugin._config["action_signing_key"]
    assert verify_action(secret, save, "save", "r1")
    assert verify_action(secret, browser, "browser", "r1")
    assert not verify_action(secret, browser, "save", "r1")
    assert plugin._config["playback_token"] not in url
