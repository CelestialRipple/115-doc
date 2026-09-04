from pathlib import Path

from tencentdoc115library.store import CatalogStore


def _resource(row_hash: str = "hash-1") -> dict:
    return {
        "resource_id": "resource-1",
        "row_number": 2,
        "title": "测试电影",
        "version": "4K",
        "share_url": "https://115.com/s/example",
        "media_type": "电影",
        "rating": "8.0",
        "year": "2024",
        "group_name": "电影合集",
        "row_hash": row_hash,
    }


def _store(tmp_path: Path) -> CatalogStore:
    store = CatalogStore(tmp_path / "catalog.db")
    store.upsert_sheets(
        [
            {
                "sheet_id": "sheet-1",
                "title": "电影大全",
                "row_count": 100,
                "column_count": 6,
                "used_row_count": 100,
                "used_column_count": 6,
            }
        ]
    )
    store.configure_sheets(
        {
            "sheet-1": {
                "enabled": True,
                "group_name": "电影合集",
                "media_mode": "movie",
            }
        }
    )
    return store


def test_page_and_checkpoint_commit_together(tmp_path: Path) -> None:
    store = _store(tmp_path)
    checkpoint = store.begin_sheet_scan("sheet-1")
    store.save_page(
        sheet_id="sheet-1",
        scan_id=checkpoint["scan_id"],
        next_row=51,
        header_map={"title": 0, "share_url": 2},
        resources=[_resource()],
        source_row_count=49,
    )
    resumed = store.begin_sheet_scan("sheet-1")
    assert resumed["scan_id"] == checkpoint["scan_id"]
    assert resumed["checkpoint_row"] == 51
    assert store.get_resource("resource-1")["status"] == "pending"


def test_completed_scan_marks_missing_rows_removed_and_can_restore_them(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = store.begin_sheet_scan("sheet-1")
    store.save_page("sheet-1", first["scan_id"], 101, {}, [_resource()], 99)
    store.complete_sheet_scan("sheet-1", first["scan_id"])

    second = store.begin_sheet_scan("sheet-1")
    store.save_page("sheet-1", second["scan_id"], 101, {}, [], 99)
    store.complete_sheet_scan("sheet-1", second["scan_id"])
    assert store.get_resource("resource-1")["status"] == "removed"

    third = store.begin_sheet_scan("sheet-1")
    store.save_page("sheet-1", third["scan_id"], 101, {}, [_resource()], 99)
    store.complete_sheet_scan("sheet-1", third["scan_id"])
    assert store.get_resource("resource-1")["status"] == "pending"


def test_group_change_requeues_existing_resource(tmp_path: Path) -> None:
    store = _store(tmp_path)
    checkpoint = store.begin_sheet_scan("sheet-1")
    store.save_page("sheet-1", checkpoint["scan_id"], 101, {}, [_resource()], 99)
    store.update_resource_status(
        "resource-1", "ready", strm_path="/old/group/movie.strm"
    )
    store.configure_sheets(
        {
            "sheet-1": {
                "enabled": True,
                "group_name": "星火",
                "media_mode": "movie",
            }
        }
    )
    resource = store.get_resource("resource-1")
    assert resource["status"] == "pending"
    assert resource["group_name"] == "星火"
    assert resource["strm_path"] is None


def test_media_mode_change_requires_fresh_sheet_sync(tmp_path: Path) -> None:
    store = _store(tmp_path)
    checkpoint = store.begin_sheet_scan("sheet-1")
    store.save_page("sheet-1", checkpoint["scan_id"], 101, {}, [_resource()], 99)
    store.update_resource_status("resource-1", "ready", strm_path="/old/movie.strm")

    store.configure_sheets(
        {
            "sheet-1": {
                "enabled": True,
                "group_name": "星火",
                "media_mode": "mixed",
            }
        }
    )

    sheet = store.get_sheet("sheet-1")
    assert sheet["media_mode"] == "mixed"
    assert sheet["checkpoint_row"] == 1
    assert sheet["scan_status"] == "idle"
    assert store.get_resource("resource-1")["status"] == "removed"


def test_clear_all_removes_database_records(tmp_path: Path) -> None:
    store = _store(tmp_path)
    checkpoint = store.begin_sheet_scan("sheet-1")
    store.save_page("sheet-1", checkpoint["scan_id"], 101, {}, [_resource()], 99)
    store.upsert_offline_playback(
        {
            "resource_id": "resource-1",
            "source_hash": "hash-1",
            "task_hash": "hash-1",
            "directory_id": "100",
            "owned_file_id": "200",
            "pick_code": "pick-code",
            "file_name": "movie.iso",
            "file_size": 1024,
            "state": "ready",
            "last_access_at": "2026-09-05T00:00:00+00:00",
            "expires_at": "2026-09-06T00:00:00+00:00",
        }
    )
    assert store.get_offline_playback("resource-1")["pick_code"] == "pick-code"

    store.clear_all()

    assert store.list_offline_playbacks() == []

    assert store.list_sheets() == []
    assert store.get_resource("resource-1") is None
    assert store.status_snapshot()["total_resources"] == 0


def test_local_search_defaults_to_ready_resources(tmp_path: Path) -> None:
    store = _store(tmp_path)
    checkpoint = store.begin_sheet_scan("sheet-1")
    store.save_page("sheet-1", checkpoint["scan_id"], 101, {}, [_resource()], 99)
    assert store.search_resources("测试电影") == []

    store.update_resource_status(
        "resource-1",
        "ready",
        strm_path="/media/电影合集/测试电影/测试电影.strm",
    )

    results = store.search_resources("测试")
    assert [item["resource_id"] for item in results] == ["resource-1"]


def test_local_search_can_show_only_resources_without_strm(tmp_path: Path) -> None:
    store = _store(tmp_path)
    checkpoint = store.begin_sheet_scan("sheet-1")
    store.save_page("sheet-1", checkpoint["scan_id"], 101, {}, [_resource()], 99)

    pending = store.search_resources(
        "测试",
        ready_only=False,
        unbuilt_only=True,
    )
    assert [item["resource_id"] for item in pending] == ["resource-1"]

    store.update_resource_status(
        "resource-1",
        "ready",
        strm_status="ready",
        scrape_status="ready",
        strm_path="/media/电影合集/测试电影/测试电影.strm",
    )
    assert store.search_resources(
        "测试",
        ready_only=False,
        unbuilt_only=True,
    ) == []


def test_local_search_supports_moviepilot_imdb_lookup(tmp_path: Path) -> None:
    store = _store(tmp_path)
    checkpoint = store.begin_sheet_scan("sheet-1")
    store.save_page("sheet-1", checkpoint["scan_id"], 101, {}, [_resource()], 99)
    store.update_resource_status(
        "resource-1",
        "ready",
        strm_status="ready",
        tmdb_id="129",
        imdb_id="tt0245429",
    )

    by_imdb = store.search_resources("tt0245429", imdb_id="tt0245429")
    assert [item["resource_id"] for item in by_imdb] == ["resource-1"]

    by_tmdb_mapping = store.search_resources(
        "tt0245429",
        imdb_id="tt-does-not-exist",
        tmdb_ids=["129"],
    )
    assert [item["resource_id"] for item in by_tmdb_mapping] == ["resource-1"]


def test_find_metadata_source_matches_media_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    checkpoint = store.begin_sheet_scan("sheet-1")
    store.save_page("sheet-1", checkpoint["scan_id"], 101, {}, [_resource()], 99)
    store.update_resource_status(
        "resource-1",
        "ready",
        strm_path="/media/电影合集/测试电影/测试电影.strm",
        media_id="movie-1",
        tmdb_id="123",
        scrape_status="ready",
    )

    source = store.find_metadata_source(
        media_id="movie-1",
        tmdb_id="123",
        media_type="电影",
        exclude_resource_id="other-resource",
    )

    assert source["resource_id"] == "resource-1"
    assert source["strm_path"].endswith("测试电影.strm")


def test_migration_paths_can_be_listed_and_updated_atomically(tmp_path: Path) -> None:
    store = _store(tmp_path)
    checkpoint = store.begin_sheet_scan("sheet-1")
    store.save_page("sheet-1", checkpoint["scan_id"], 101, {}, [_resource()], 99)
    store.update_resource_status(
        "resource-1",
        "ready",
        strm_path="/media/电影合集/测试电影/测试电影.strm",
    )
    store.replace_resource_files(
        "resource-1",
        [
            {
                "file_id": "file-1",
                "file_name": "测试电影.mkv",
                "file_path": "/测试电影.mkv",
                "strm_path": "/media/电影合集/测试电影/测试电影.strm",
            }
        ],
    )

    resources = store.list_migration_resources()
    assert [item["resource_id"] for item in resources] == ["resource-1"]
    store.update_resource_paths(
        "resource-1",
        "/media/电影合集/电影大全/测试电影/测试电影.strm",
        {
            "file-1": "/media/电影合集/电影大全/测试电影/测试电影.strm",
        },
    )
    assert store.get_resource("resource-1")["strm_path"].endswith(
        "电影大全/测试电影/测试电影.strm"
    )
    assert store.get_resource_file("resource-1", "file-1")["strm_path"].endswith(
        "电影大全/测试电影/测试电影.strm"
    )


def test_known_v2_signature_errors_can_be_requeued_automatically(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    checkpoint = store.begin_sheet_scan("sheet-1")
    store.save_page("sheet-1", checkpoint["scan_id"], 101, {}, [_resource()], 99)
    store.update_resource_status(
        "resource-1",
        "build_error",
        "MediaChain.recognize_by_meta() got an unexpected keyword argument 'mtype'",
    )

    assert store.retry_errors_containing("unexpected keyword argument 'mtype'") == 1
    resource = store.get_resource("resource-1")
    assert resource["status"] == "pending"
    assert resource["last_error"] is None


def test_build_progress_is_visible_and_all_failures_can_be_requeued(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    checkpoint = store.begin_sheet_scan("sheet-1")
    store.save_page("sheet-1", checkpoint["scan_id"], 101, {}, [_resource()], 99)
    store.update_resource_status(
        "resource-1",
        "processing",
        strm_status="ready",
        scrape_status="scraping",
        strm_path="/media/movie.strm",
    )

    snapshot = store.status_snapshot()
    assert snapshot["strm_counts"] == {"ready": 1}
    assert snapshot["scrape_counts"] == {"scraping": 1}
    assert snapshot["current_resources"][0]["title"] == "测试电影"

    store.update_resource_status(
        "resource-1",
        "metadata_error",
        "TMDB unavailable",
        strm_status="ready",
        scrape_status="failed",
    )
    assert store.retry_all_failed_resources() == 1
    resource = store.get_resource("resource-1")
    assert resource["status"] == "pending"
    assert resource["strm_status"] == "pending"
    assert resource["scrape_status"] == "pending"
    assert resource["last_error"] is None

    store.update_resource_status(
        "resource-1",
        "invalid_share",
        "115 分享访问码错误",
        strm_status="failed",
        scrape_status="blocked",
    )
    assert store.retry_all_failed_resources() == 0
    assert store.retry_resources(["resource-1"]) == 0
    assert store.get_resource("resource-1")["status"] == "invalid_share"


def test_unrecognized_strm_can_be_requeued_for_future_recognition(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    checkpoint = store.begin_sheet_scan("sheet-1")
    store.save_page("sheet-1", checkpoint["scan_id"], 101, {}, [_resource()], 99)
    store.update_resource_status(
        "resource-1",
        "ready",
        "MoviePilot 未识别到媒体；已仅生成 STRM",
        strm_status="ready",
        scrape_status="unrecognized",
        strm_path="/media/movie.strm",
    )

    assert store.retry_all_failed_resources() == 1
    resource = store.get_resource("resource-1")
    assert resource["status"] == "pending"
    assert resource["strm_status"] == "pending"
    assert resource["scrape_status"] == "pending"
