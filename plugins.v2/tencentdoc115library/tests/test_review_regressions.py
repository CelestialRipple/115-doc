"""Regression tests for the 2026-09-05 security and correctness review.

No real accounts, external requests, or production files are used.
Tests exercise actual imported plugin code with isolated host and network boundaries.
"""

import asyncio
import threading
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

from tencentdoc115library.downloader import DirectDownloadManager
from tencentdoc115library.gateway import DirectPlayGateway
from tencentdoc115library.library import LibraryBuilder
from tencentdoc115library.resolver import ShareResolver, ShareResolutionError
from tencentdoc115library.catalog import CatalogSynchronizer, sheet_config_key
from tencentdoc115library.store import CatalogStore

LOGGER = SimpleNamespace(
    info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None
)


def resource(identity="resource-a"):
    return dict(
        resource_id=identity,
        title="Same Movie",
        year="2024",
        version="",
        share_url=f"https://115.com/s/{identity}",
        group_name="Movies",
        sheet_id="sheet-a",
        media_type="电影",
        status="ready",
    )


def builder(tmp_path):
    config = dict(
        output_root=str(tmp_path / "output"),
        playback_token="test-secret",
        public_base_url="http://moviepilot.invalid",
        scrape_metadata=True,
    )
    store = SimpleNamespace(
        replace_resource_files=lambda *a: None, find_metadata_source=lambda **k: None
    )
    return LibraryBuilder(
        store,
        SimpleNamespace(choose_movie_file=ShareResolver.choose_movie_file),
        lambda: config,
        threading.Event(),
    )


def test_distinct_shares_keep_distinct_strm_files(tmp_path):
    build = builder(tmp_path)
    media = SimpleNamespace(title="Same Movie", year="2024")
    file = dict(file_id="1", file_name="movie.mkv", file_size=123)
    first = build._build_movie(resource("resource-a"), None, media, source_files=[file])
    original = Path(first).read_text()
    second = build._build_movie(
        resource("resource-b"), None, media, source_files=[file]
    )
    assert first != second and Path(first).read_text() == original, (
        f"Two different shares map to {first}; first STRM now contains resource-b"
    )


def test_partial_scrape_is_retried_instead_of_treated_as_complete(tmp_path):
    build = builder(tmp_path)
    directory = tmp_path / "output" / "movie"
    directory.mkdir(parents=True)
    (directory / "poster.jpg").write_bytes(b"partial prior scrape")
    calls = []
    build._scrape = lambda *a: calls.append("scrape")
    build._scrape_with_reuse(
        resource(),
        directory,
        None,
        SimpleNamespace(title="Same Movie", year="2024", tmdb_id="123"),
    )
    assert calls, "A leftover poster prevents all future scraping, even with no NFO"


def test_clear_preserves_untracked_user_metadata(tmp_path):
    build = builder(tmp_path)
    directory = tmp_path / "output" / "existing-user-library"
    directory.mkdir(parents=True)
    original = directory / "user-owned.nfo"
    original.write_text("user metadata, never generated or recorded by the plugin")
    build.clear_generated_output()
    assert original.exists(), "Cleanup deletes untracked files based only on suffix"


def test_gateway_cannot_reuse_other_clients_auth_via_spoofed_forwarding_headers(
    tmp_path,
):
    path = tmp_path / "movie.strm"
    path.write_text(
        "http://mp/api/v1/plugin/TencentDoc115Library/play/resource-a?token=test-secret"
    )
    config = {"emby_strm_paths": str(tmp_path), "playback_token": "test-secret"}
    gateway = DirectPlayGateway(
        lambda: config,
        SimpleNamespace(resolve=lambda *a, **k: "https://cdn.invalid/movie"),
    )
    gateway._session = object()
    SimpleNamespace(
        remote="192.0.2.10",
        query={"api_key": "valid-client-key"},
        headers={"User-Agent": "Infuse"},
    )
    # Previous authenticated playback does not grant IP-based access.
    gateway._item_paths["item1"] = str(path)
    attacker = SimpleNamespace(
        remote="198.51.100.20",
        query={},
        headers={"User-Agent": "Infuse", "X-Forwarded-For": "192.0.2.10"},
        path="/Videos/item1/stream",
        method="GET",
    )
    gateway._schedule_media_probe = lambda *a: None
    response = asyncio.run(gateway._direct_response(attacker, "item1", config))
    assert response is None or response.status != 302, (
        f"Unauthenticated different peer received HTTP {response.status}"
    )


def test_fast_background_task_does_not_deadlock_submit(plugin, monkeypatch):
    class ImmediatePool:
        def submit(self, function, *args, **kwargs):
            future = Future()
            future.set_result(function(*args, **kwargs))
            return future

    monkeypatch.setattr("tencentdoc115library.ThreadHelper", ImmediatePool)
    plugin._task_lock = threading.Lock()
    plugin._future = None
    plugin._task_state = "idle"
    plugin._resume_spec = None
    plugin._stop_event = threading.Event()
    plugin._pause_event = threading.Event()
    done = threading.Event()

    def invoke():
        try:
            plugin._submit("quick task", lambda: {"status": "completed"})
        except RuntimeError:
            pass  # Only possible after artificial teardown unlock below.
        finally:
            done.set()

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    completed = done.wait(0.5)
    if not completed:
        # Release only this artificial test object's lock to collect the worker.
        plugin._task_lock.release()
        worker.join(1)
    assert completed, "Immediate Future callback re-enters the non-reentrant _task_lock"


class FakeResponse:
    def __init__(self, status=200, body=b"new content", error=None):
        self.status_code, self.body, self.error = status, body, error
        self.headers = {}

    def __bool__(self):
        return self.status_code < 400  # requests.Response behavior

    def iter_content(self, **kwargs):
        if self.error:
            raise self.error
        yield self.body

    def close(self):
        pass


def downloader(resolver):
    if not hasattr(resolver, "clear_url_cache"):
        resolver.clear_url_cache = lambda: None
    return DirectDownloadManager(
        SimpleNamespace(update_download_task=lambda *a, **k: None),
        resolver,
        lambda: {"download_retries": 2},
    )


def test_expired_url_is_refreshed_during_download_retry(tmp_path):
    fresh_requests = []
    resolver = ShareResolver(object(), lambda: {"direct_url_cache_ttl": 6000})
    resolver._download_url = lambda *a: (
        fresh_requests.append(1) or f"https://cdn.invalid/{len(fresh_requests)}"
    )
    manager = downloader(resolver)
    manager._request = SimpleNamespace(get_res=lambda **k: FakeResponse(403))
    with pytest.raises(RuntimeError):
        manager._download_one(
            "task1",
            resource(),
            {"file_id": "1"},
            tmp_path / "movie.mkv",
            threading.Event(),
            0,
            0,
        )
    assert len(fresh_requests) > 1, "All retries reuse the same cached rejected URL"


def test_interrupted_download_stream_is_retried(tmp_path):
    manager = downloader(
        SimpleNamespace(resolve_file_url=lambda *a, **k: "https://cdn.invalid/movie")
    )
    calls = []

    def request(**kwargs):
        calls.append(1)
        return FakeResponse(error=ConnectionError("simulated interrupted stream"))

    manager._request = SimpleNamespace(get_res=request)
    with pytest.raises(RuntimeError):
        manager._download_one(
            "task1",
            resource(),
            {"file_id": "1"},
            tmp_path / "movie.mkv",
            threading.Event(),
            0,
            0,
        )
    assert len(calls) > 1, "iter_content exception escapes the retry loop immediately"


def test_unbuilt_tv_resource_can_select_episodes():
    manager = downloader(
        SimpleNamespace(
            list_video_files=lambda *a: [
                {
                    "file_id": "1",
                    "file_name": "Show.S01E02.mkv",
                    "file_path": "/Show.S01E02.mkv",
                    "file_size": 123,
                }
            ]
        )
    )
    manager.store.list_resource_files = lambda *a: []
    tv = {**resource(), "media_type": "电视剧", "group_name": "剧集"}
    try:
        files = manager._select_files(tv, {2})
    except ShareResolutionError:
        files = []
    assert files, (
        "Raw share enumeration has no episode field; episode 2 is incorrectly filtered out"
    )


def test_unknown_file_id_does_not_mark_valid_share_invalid():
    state = resource()

    def update(identity, status, *args, **kwargs):
        state["status"] = status

    store = SimpleNamespace(
        get_resource=lambda *a: state,
        get_resource_file=lambda *a: None,
        update_resource_status=update,
    )
    resolver = ShareResolver(store, lambda: {})
    with pytest.raises(ShareResolutionError):
        resolver.resolve("resource-a", file_id="nonexistent")
    assert state["status"] != "invalid_share", (
        "A bad playback file_id poisons valid catalog state"
    )


def test_different_download_sources_do_not_overwrite_same_target(tmp_path):
    manager = downloader(
        SimpleNamespace(resolve_file_url=lambda *a, **k: "https://cdn.invalid/movie")
    )
    file = {"file_id": "1", "file_name": "movie.mkv", "file_size": 11}
    first = manager._target_path(resource("a"), file, tmp_path, False)
    second = manager._target_path(resource("b"), file, tmp_path, False)
    first.write_bytes(b"original movie")
    manager._request = SimpleNamespace(
        get_res=lambda **k: FakeResponse(body=b"replacement")
    )
    manager._download_one(
        "task-b", resource("b"), file, second, threading.Event(), 0, 11
    )
    assert first != second and first.read_bytes() == b"original movie", (
        "Another resource silently replaces the existing video"
    )


def test_incremental_worksheet_sync_reads_rows_added_beyond_old_dimensions(tmp_path):
    store = CatalogStore(tmp_path / "catalog.db")
    store.upsert_sheets(
        [
            dict(
                sheet_id="sheet-a",
                title="电影大全",
                row_count=2,
                used_row_count=2,
                column_count=2,
                used_column_count=2,
            )
        ]
    )
    config = {
        "page_rows": 2,
        "pages_per_run": 10,
        f"sheet_{sheet_config_key('sheet-a')}_enabled": True,
    }
    calls = []
    rows = [
        ["电影名称", "网盘链接"],
        ["Old", "https://115.com/s/old"],
        ["New", "https://115.com/s/new"],
    ]

    def get_range(**kwargs):
        start = kwargs["start_row"]
        calls.append(start)
        return {"rows": rows[start - 1 : start + 1], "requested_rows": 2}

    sync = CatalogSynchronizer(
        store,
        lambda: SimpleNamespace(get_range=get_range),
        lambda: config,
        lambda c: None,
        threading.Event(),
    )
    sync.sync()
    sync.sync()  # A second automatic scan must discover the appended third row.
    found = store.search_resources("New", ready_only=False)
    assert found, (
        f"Both scans stop at stale row_count=2; requested starting rows: {calls}"
    )


def test_playback_token_alone_cannot_authorize_library_writes(plugin):
    class Html:
        def __init__(self, content, status_code=200):
            self.status_code = status_code

    plugin._config = {"playback_token": "secret-present-in-every-strm"}
    modifications = []
    plugin._store = SimpleNamespace(
        get_resource=lambda *a: {**resource(), "status": "pending"},
        configure_resource_for_save=lambda *a: modifications.append(a) or True,
    )
    plugin._builder = SimpleNamespace(build=lambda: None)
    plugin._submit = lambda *a, **k: SimpleNamespace(success=True, message="accepted")
    plugin._save_page_html = lambda *a, **k: "ok"

    async def body():
        return b"group_name=Changed&media_mode=movie"

    request = SimpleNamespace(method="POST", body=body)
    asyncio.run(
        plugin.save_search_resource(
            request, "resource-a", token="secret-present-in-every-strm"
        )
    )
    assert not modifications, (
        "Read/play credential also authorizes persistent library writes"
    )
