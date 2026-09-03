import os
from pathlib import Path
from threading import Event

from tencentdoc115library.library import LibraryBuilder
from tencentdoc115library.store import CatalogStore


def _sheet(sheet_id: str, title: str) -> dict:
    return {
        "sheet_id": sheet_id,
        "title": title,
        "row_count": 2,
        "column_count": 6,
        "used_row_count": 2,
        "used_column_count": 6,
    }


def _resource(resource_id: str, title: str, share_url: str) -> dict:
    return {
        "resource_id": resource_id,
        "row_number": 2,
        "title": title,
        "version": "",
        "share_url": share_url,
        "media_type": "电影",
        "rating": "",
        "year": "2024",
        "group_name": "共享",
        "row_hash": resource_id,
    }


def test_migrate_shared_old_directory_splits_strm_without_rescraping(
    tmp_path: Path,
) -> None:
    root = tmp_path / "output"
    old_directory = root / "共享" / "测试电影 (2024)"
    old_directory.mkdir(parents=True)
    (old_directory / "movie.nfo").write_text("metadata", encoding="utf-8")
    first_strm = old_directory / "测试电影-A.strm"
    second_strm = old_directory / "测试电影-B.strm"
    first_strm.write_text("https://example/A\n", encoding="utf-8")
    second_strm.write_text("https://example/B\n", encoding="utf-8")

    store = CatalogStore(tmp_path / "catalog.db")
    store.upsert_sheets([_sheet("sheet-a", "工作表甲"), _sheet("sheet-b", "工作表乙")])
    store.configure_sheets(
        {
            "sheet-a": {"enabled": True, "group_name": "共享", "media_mode": "movie"},
            "sheet-b": {"enabled": True, "group_name": "共享", "media_mode": "movie"},
        }
    )
    for sheet_id, resource_id, share_url, strm_path in (
        ("sheet-a", "resource-a", "https://115.com/s/a", first_strm),
        ("sheet-b", "resource-b", "https://115.com/s/b", second_strm),
    ):
        checkpoint = store.begin_sheet_scan(sheet_id)
        store.save_page(
            sheet_id,
            checkpoint["scan_id"],
            3,
            {},
            [_resource(resource_id, "测试电影", share_url)],
            1,
        )
        store.update_resource_status(
            resource_id,
            "ready",
            strm_path=str(strm_path),
            strm_status="ready",
            scrape_status="ready",
        )
        store.replace_resource_files(
            resource_id,
            [
                {
                    "file_id": resource_id,
                    "file_name": f"{resource_id}.mkv",
                    "file_path": f"/{resource_id}.mkv",
                    "strm_path": str(strm_path),
                }
            ],
        )

    builder = LibraryBuilder(
        store=store,
        resolver=object(),
        config_provider=lambda: {
            "output_root": str(root),
            "separate_source_folders": True,
        },
        stop_event=Event(),
        pause_event=Event(),
    )
    result = builder.migrate_existing_output()

    assert result["status"] == "completed"
    target_a = root / "共享" / "工作表甲" / "测试电影 (2024)"
    target_b = root / "共享" / "工作表乙" / "测试电影 (2024)"
    assert (target_a / first_strm.name).is_file()
    assert (target_b / second_strm.name).is_file()
    assert (target_a / "movie.nfo").read_text(encoding="utf-8") == "metadata"
    assert (target_b / "movie.nfo").read_text(encoding="utf-8") == "metadata"
    if os.name != "nt":
        assert (target_a / "movie.nfo").stat().st_ino == (
            target_b / "movie.nfo"
        ).stat().st_ino
    assert store.get_resource("resource-a")["strm_path"] == str(
        target_a / first_strm.name
    )
    assert store.get_resource("resource-b")["strm_path"] == str(
        target_b / second_strm.name
    )


def test_build_honors_pause_before_starting_next_resource(tmp_path: Path) -> None:
    store = CatalogStore(tmp_path / "catalog.db")
    store.upsert_sheets([_sheet("sheet-a", "工作表甲")])
    store.configure_sheets(
        {"sheet-a": {"enabled": True, "group_name": "共享", "media_mode": "movie"}}
    )
    checkpoint = store.begin_sheet_scan("sheet-a")
    store.save_page(
        "sheet-a",
        checkpoint["scan_id"],
        3,
        {},
        [_resource("resource-a", "测试电影", "https://115.com/s/a")],
        1,
    )
    pause_event = Event()
    pause_event.set()
    builder = LibraryBuilder(
        store=store,
        resolver=object(),
        config_provider=lambda: {
            "output_root": str(tmp_path / "output"),
            "scrape_metadata": True,
        },
        stop_event=Event(),
        pause_event=pause_event,
    )

    result = builder.build(limit=1)

    assert result["status"] == "paused"
    assert result["processed"] == 0
    assert store.get_resource("resource-a")["status"] == "pending"
