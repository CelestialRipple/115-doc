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
        {"sheet-1": {"enabled": True, "group_name": "电影合集"}}
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
    store.save_page(
        "sheet-1", first["scan_id"], 101, {}, [_resource()], 99
    )
    store.complete_sheet_scan("sheet-1", first["scan_id"])

    second = store.begin_sheet_scan("sheet-1")
    store.save_page("sheet-1", second["scan_id"], 101, {}, [], 99)
    store.complete_sheet_scan("sheet-1", second["scan_id"])
    assert store.get_resource("resource-1")["status"] == "removed"

    third = store.begin_sheet_scan("sheet-1")
    store.save_page(
        "sheet-1", third["scan_id"], 101, {}, [_resource()], 99
    )
    store.complete_sheet_scan("sheet-1", third["scan_id"])
    assert store.get_resource("resource-1")["status"] == "pending"


def test_group_change_requeues_existing_resource(tmp_path: Path) -> None:
    store = _store(tmp_path)
    checkpoint = store.begin_sheet_scan("sheet-1")
    store.save_page(
        "sheet-1", checkpoint["scan_id"], 101, {}, [_resource()], 99
    )
    store.update_resource_status(
        "resource-1", "ready", strm_path="/old/group/movie.strm"
    )
    store.configure_sheets({"sheet-1": {"enabled": True, "group_name": "星火"}})
    resource = store.get_resource("resource-1")
    assert resource["status"] == "pending"
    assert resource["group_name"] == "星火"
    assert resource["strm_path"] is None


def test_local_search_defaults_to_ready_resources(tmp_path: Path) -> None:
    store = _store(tmp_path)
    checkpoint = store.begin_sheet_scan("sheet-1")
    store.save_page(
        "sheet-1", checkpoint["scan_id"], 101, {}, [_resource()], 99
    )
    assert store.search_resources("测试电影") == []

    store.update_resource_status(
        "resource-1",
        "ready",
        strm_path="/media/电影合集/测试电影/测试电影.strm",
    )

    results = store.search_resources("测试")
    assert [item["resource_id"] for item in results] == ["resource-1"]
