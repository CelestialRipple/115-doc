import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import tencentdoc115library.library as library_module
from tencentdoc115library.library import LibraryBuilder
from tencentdoc115library.library import MediaType
from tencentdoc115library.library import safe_path_segment
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
    assert Path(store.get_resource("resource-a")["strm_path"]).resolve() == (
        target_a / first_strm.name
    ).resolve()
    assert Path(store.get_resource("resource-b")["strm_path"]).resolve() == (
        target_b / second_strm.name
    ).resolve()


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


def test_mixed_sheet_accepts_moviepilot_tv_result_and_changes_group(
    tmp_path: Path,
) -> None:
    store = CatalogStore(tmp_path / "catalog.db")
    store.upsert_sheets([_sheet("sheet-a", "星火4K全站资源")])
    store.configure_sheets(
        {"sheet-a": {"enabled": True, "group_name": "星火", "media_mode": "mixed"}}
    )
    checkpoint = store.begin_sheet_scan("sheet-a")
    resource = _resource(
        "resource-a", "魔奇少年 第二季", "https://115.com/s/example"
    )
    resource["group_name"] = "星火"
    store.save_page(
        "sheet-a", checkpoint["scan_id"], 3, {}, [resource], 1
    )
    resource = store.get_resource("resource-a")

    class TelevisionMediaChain:
        @staticmethod
        def recognize_by_meta(metainfo, obtain_images=False):
            return SimpleNamespace(
                title="魔奇少年",
                year="2013",
                type=MediaType.TV,
                media_id="tv-1",
                tmdb_id="60637",
            )

    class Resolver:
        @staticmethod
        def list_video_files(_share_url):
            return [
                {
                    "file_id": "episode-1",
                    "file_name": "魔奇少年.S02E01.mkv",
                    "file_path": "/魔奇少年/Season 02/魔奇少年.S02E01.mkv",
                    "size": 1024,
                }
            ]

    original_media_chain = library_module.MediaChain
    library_module.MediaChain = TelevisionMediaChain
    try:
        builder = LibraryBuilder(
            store=store,
            resolver=Resolver(),
            config_provider=lambda: {
                "output_root": str(tmp_path / "output"),
                "public_base_url": "http://moviepilot:3000",
                "playback_token": "test-token",
                "scrape_metadata": False,
            },
            stop_event=Event(),
        )
        # 模拟表格标签把动画剧集预判为电影；最终以 MoviePilot 返回类型为准。
        builder._media_type = lambda _resource, _files=None: MediaType.MOVIE
        result = builder.build(limit=1)
    finally:
        library_module.MediaChain = original_media_chain

    updated = store.get_resource("resource-a")
    assert result["success"] == 1
    assert updated["detected_media_type"] == "电视剧"
    assert updated["detected_group_name"] == "星火-剧集"
    assert "星火-剧集" in Path(updated["strm_path"]).parts
    episode_strms = list(Path(updated["strm_path"]).rglob("*.strm"))
    assert len(episode_strms) == 1
    assert episode_strms[0].parent.name == "Season 02"


def test_tv_iso_disc_numbers_are_used_as_episode_numbers(tmp_path: Path) -> None:
    captured_files = []

    class Store:
        @staticmethod
        def replace_resource_files(_resource_id, files):
            captured_files.extend(files)

    builder = LibraryBuilder(
        store=Store(),
        resolver=object(),
        config_provider=lambda: {
            "public_base_url": "http://moviepilot:3000",
            "playback_token": "test-token",
        },
        stop_event=Event(),
    )
    resource = {
        "resource_id": "documentary-discs",
        "title": "北美大地",
        "year": "2013",
    }
    mediainfo = SimpleNamespace(title="北美大地", year="2013")
    source_files = [
        {
            "file_id": "disc-1",
            "file_name": "北美大地 North America 2013 Disc1.iso",
            "file_path": "/北美大地/北美大地 North America 2013 Disc1.iso",
        },
        {
            "file_id": "disc-3",
            "file_name": "D3.iso",
            "file_path": "/北美大地/D3.iso",
        },
        {
            "file_id": "bonus",
            "file_name": "Bonus.iso",
            "file_path": "/北美大地/Bonus.iso",
        },
    ]

    result = builder._build_tv(
        resource,
        meta=SimpleNamespace(),
        mediainfo=mediainfo,
        directory=tmp_path / "北美大地 (2013)",
        source_files=source_files,
    )

    generated = sorted(Path(result).rglob("*.strm"))
    assert [item.name for item in generated] == [
        "北美大地 (2013) - S01E01.strm",
        "北美大地 (2013) - S01E03.strm",
    ]
    assert [(item["season"], item["episode"]) for item in captured_files] == [
        (1, 1),
        (1, 3),
    ]


def test_tv_iso_disc_episode_patterns() -> None:
    assert LibraryBuilder._episode_identity("D1.iso") == (1, 1)
    assert LibraryBuilder._episode_identity("D2.iso") == (1, 2)
    assert LibraryBuilder._episode_identity("North America Disc02.iso") == (1, 2)
    assert LibraryBuilder._episode_identity("CD03.iso") == (1, 3)
    assert LibraryBuilder._episode_identity("第4碟.iso") == (1, 4)
    assert LibraryBuilder._episode_identity("05.iso") == (1, 5)
    assert LibraryBuilder._episode_identity("Bonus.iso") == (None, None)


def test_tv_files_without_episode_markers_use_natural_path_order(
    tmp_path: Path,
) -> None:
    captured_files = []

    class Store:
        @staticmethod
        def replace_resource_files(_resource_id, files):
            captured_files.extend(files)

    builder = LibraryBuilder(
        store=Store(),
        resolver=object(),
        config_provider=lambda: {
            "public_base_url": "http://moviepilot:3000",
            "playback_token": "test-token",
        },
        stop_event=Event(),
    )
    source_files = [
        {"file_id": "ten", "file_name": "video10.mkv", "file_path": "/video10.mkv"},
        {"file_id": "two", "file_name": "video2.mkv", "file_path": "/video2.mkv"},
        {
            "file_id": "amazon",
            "file_name": "[神话亚马逊 Mythos Amazonas 2010][无中字].iso",
            "file_path": "/[神话亚马逊 Mythos Amazonas 2010][无中字].iso",
        },
    ]

    result = builder._build_tv(
        {"resource_id": "natural-tv", "title": "测试剧集", "year": "2020"},
        meta=SimpleNamespace(begin_season=2),
        mediainfo=SimpleNamespace(title="测试剧集", year="2020"),
        directory=tmp_path / "测试剧集 (2020)",
        source_files=source_files,
    )

    generated = sorted(Path(result).rglob("*.strm"))
    assert [item.name for item in generated] == [
        "测试剧集 (2020) - S02E01.strm",
        "测试剧集 (2020) - S02E02.strm",
        "测试剧集 (2020) - S02E03.strm",
    ]
    assert [item["file_id"] for item in captured_files] == ["amazon", "two", "ten"]
    assert [item["episode"] for item in captured_files] == [1, 2, 3]


def test_movie_strm_filename_respects_utf8_path_segment_limit(tmp_path: Path) -> None:
    captured_files = []

    class Store:
        @staticmethod
        def replace_resource_files(_resource_id, files):
            captured_files.extend(files)

    class Resolver:
        @staticmethod
        def choose_movie_file(files):
            return files[0]

    builder = LibraryBuilder(
        store=Store(),
        resolver=Resolver(),
        config_provider=lambda: {
            "public_base_url": "http://moviepilot:3000",
            "playback_token": "test-token",
        },
        stop_event=Event(),
    )
    resource = {
        "resource_id": "long-movie",
        "title": "天国王朝",
        "year": "2005",
        "version": "导演剪辑版国语简繁字幕" * 30,
    }
    path = builder._build_movie(
        resource,
        meta=SimpleNamespace(),
        mediainfo=SimpleNamespace(title="天国王朝", year="2005"),
        directory=tmp_path / "天国王朝 (2005)",
        source_files=[
            {"file_id": "movie", "file_name": "movie.iso", "file_path": "/movie.iso"}
        ],
    )

    assert Path(path).is_file()
    assert len(Path(path).name.encode("utf-8")) <= 255
    assert len(safe_path_segment("中" * 200).encode("utf-8")) <= 180
    assert captured_files[0]["strm_path"] == path


def test_unrecognized_movie_still_generates_strm_without_metadata(
    tmp_path: Path,
) -> None:
    store = CatalogStore(tmp_path / "catalog.db")
    store.upsert_sheets([_sheet("sheet-a", "星火4K全站资源")])
    store.configure_sheets(
        {"sheet-a": {"enabled": True, "group_name": "星火", "media_mode": "mixed"}}
    )
    checkpoint = store.begin_sheet_scan("sheet-a")
    resource = _resource(
        "resource-a", "77年航空港", "https://115.com/s/example"
    )
    resource["year"] = "1977"
    resource["group_name"] = "星火"
    store.save_page(
        "sheet-a", checkpoint["scan_id"], 3, {}, [resource], 1
    )

    class UnrecognizedMediaChain:
        @staticmethod
        def recognize_by_meta(metainfo, obtain_images=False):
            return None

    class Resolver:
        @staticmethod
        def list_video_files(_share_url):
            return [
                {
                    "file_id": "file-1",
                    "file_name": "77年航空港.1977.mkv",
                    "file_path": "/77年航空港.1977.mkv",
                    "size": 1024,
                }
            ]

        @staticmethod
        def choose_movie_file(files):
            return files[0]

    original_media_chain = library_module.MediaChain
    library_module.MediaChain = UnrecognizedMediaChain
    try:
        builder = LibraryBuilder(
            store=store,
            resolver=Resolver(),
            config_provider=lambda: {
                "output_root": str(tmp_path / "output"),
                "public_base_url": "http://moviepilot:3000",
                "playback_token": "test-token",
                "scrape_metadata": True,
            },
            stop_event=Event(),
        )
        result = builder.build(limit=1)
    finally:
        library_module.MediaChain = original_media_chain

    updated = store.get_resource("resource-a")
    assert result["success"] == 1
    assert result["failed"] == 0
    assert updated["status"] == "ready"
    assert updated["strm_status"] == "ready"
    assert updated["scrape_status"] == "unrecognized"
    assert updated["detected_media_type"] == "电影"
    assert updated["detected_group_name"] == "星火"
    strm_path = Path(updated["strm_path"])
    assert strm_path.is_file()
    assert "TencentDoc115Library/play/resource-a" in strm_path.read_text(
        encoding="utf-8"
    )
    assert not list(strm_path.parent.glob("*.nfo"))
