from tencentdoc115library.storage_limit import (
    GIB,
    configured_limit_bytes,
    directory_size,
    format_gib,
)


def test_configured_limit_bytes_supports_zero_fraction_and_invalid_values() -> None:
    assert configured_limit_bytes({"output_size_limit_gb": 0}) == 0
    assert configured_limit_bytes({"output_size_limit_gb": "1.5"}) == int(1.5 * GIB)
    assert configured_limit_bytes({"output_size_limit_gb": "invalid"}) == 0
    assert configured_limit_bytes({"output_size_limit_gb": -1}) == 0


def test_directory_size_counts_files_but_not_symlinks(tmp_path) -> None:
    nested = tmp_path / "电影" / "测试"
    nested.mkdir(parents=True)
    (nested / "movie.strm").write_bytes(b"12345")
    (nested / "movie.nfo").write_bytes(b"1234567")
    (tmp_path / "shortcut").symlink_to(nested / "movie.nfo")

    assert directory_size(tmp_path) == 12
    assert format_gib(GIB) == "1.00 GiB"


def test_display_snapshot_never_waits_for_slow_disk_or_duplicates_scans(monkeypatch):
    import threading
    from tencentdoc115library.storage_limit import DisplayStorageCache
    entered, release = threading.Event(), threading.Event()
    calls = []
    def slow(path):
        calls.append(str(path))
        entered.set()
        assert release.wait(3)
        return 123
    monkeypatch.setattr('tencentdoc115library.storage_limit.directory_size', slow)
    cache = DisplayStorageCache()
    config = {'output_root': '/slow', 'output_size_limit_gb': 1}
    try:
        first = cache.snapshot(config)
        assert first['usage_pending'] and first['usage_bytes'] is None
        assert entered.wait(1)
        for _ in range(10):
            assert cache.snapshot(config)['usage_refreshing']
        assert calls == ['/slow']
    finally:
        release.set()
        cache._worker.join(2)
    assert cache.snapshot(config)['usage_bytes'] == 123
    assert calls == ['/slow']
    assert cache.snapshot({'output_root': ''})['usage_bytes'] == 0


def test_display_failure_does_not_report_zero_usage(monkeypatch):
    from tencentdoc115library.storage_limit import DisplayStorageCache
    def fail(path):
        raise OSError('disk unavailable')
    monkeypatch.setattr('tencentdoc115library.storage_limit.directory_size', fail)
    cache = DisplayStorageCache()
    cache.snapshot({'output_root': '/missing'})
    cache._worker.join(2)
    result = cache.snapshot({'output_root': '/missing'})
    assert result['usage_bytes'] is None and result['usage_error']
