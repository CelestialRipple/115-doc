from threading import Event
from types import SimpleNamespace

import pytest

from tencentdoc115library.downloader import DirectDownloadManager


class Response:
    status_code = 206
    headers = {"Content-Range": "bytes 3-5/6"}

    def iter_content(self, **kwargs):
        yield b"def"

    def close(self):
        self.closed = True


def test_resume_checks_range_and_publishes_complete_file_without_overwrite(tmp_path):
    response = Response()
    requests = []
    store = SimpleNamespace(update_download_task=lambda *a, **k: None)
    resolver = SimpleNamespace(
        resolve_file_url=lambda *a, **k: "https://cdn.example/video"
    )
    manager = DirectDownloadManager(store, resolver, lambda: {"download_retries": 0})
    manager._request = SimpleNamespace(
        get_res=lambda **kwargs: requests.append(kwargs) or response
    )
    target = tmp_path / "movie.mkv"
    target.with_suffix(".mkv.part").write_bytes(b"abc")
    size = manager._download_one(
        "t1",
        {"share_url": "https://115.com/s/a"},
        {"file_id": "1", "file_size": 6},
        target,
        Event(),
        0,
        6,
    )
    assert size == 6 and target.read_bytes() == b"abcdef"
    assert requests[0]["headers"]["Range"] == "bytes=3-"
    assert not target.with_suffix(".mkv.part").exists()
    assert response.closed


def test_mismatched_range_retains_partial_without_publishing(tmp_path):
    response = Response()
    response.headers = {"Content-Range": "bytes 1-3/6"}
    manager = DirectDownloadManager(
        SimpleNamespace(update_download_task=lambda *a, **k: None),
        SimpleNamespace(resolve_file_url=lambda *a, **k: "https://cdn.example/video"),
        lambda: {"download_retries": 0},
    )
    manager._request = SimpleNamespace(get_res=lambda **k: response)
    target = tmp_path / "movie.mkv"
    partial = target.with_suffix(".mkv.part")
    partial.write_bytes(b"abc")
    with pytest.raises(RuntimeError, match="范围"):
        manager._download_one(
            "t1",
            {"share_url": "https://115.com/s/a"},
            {"file_id": "1", "file_size": 6},
            target,
            Event(),
            0,
            6,
        )
    assert partial.read_bytes() == b"abc" and not target.exists()


def test_missing_task_releases_worker_state(plugin):
    manager = plugin._direct_downloader
    event = Event()
    manager._task_events["missing-task"] = event
    manager._run_task("missing-task", event)
    assert not manager.has_active_tasks()


def test_browser_mode_cannot_resume_legacy_downloader_jobs(plugin):
    assert plugin._direct_downloader.start_torrents("old-task") is False
    assert not plugin._direct_downloader.has_active_tasks()
