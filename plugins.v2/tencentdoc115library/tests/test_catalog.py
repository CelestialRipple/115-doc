from threading import Event

from tencentdoc115library.catalog import (
    CatalogParser,
    CatalogSynchronizer,
    default_group_for_title,
    default_media_mode_for_title,
    document_sources,
    namespaced_sheet_id,
)
from tencentdoc115library.client import (
    TencentDocumentClient,
    TencentDocumentMcpClient,
)
from tencentdoc115library.store import CatalogStore


def test_default_groups_match_expected_library_layout() -> None:
    assert default_group_for_title("电影大全") == "电影合集"
    assert default_group_for_title("动画") == "电影合集"
    assert default_group_for_title("纪录片") == "电影合集"
    assert default_group_for_title("剧集") == "剧集"
    assert default_group_for_title("星火4K全站资源") == "星火"
    assert default_group_for_title("蚂蚁4K") == "蚂蚁"
    assert default_media_mode_for_title("电影大全") == "movie"
    assert default_media_mode_for_title("剧集大全") == "tv"
    assert default_media_mode_for_title("星火4K全站资源") == "mixed"


def test_multiple_document_sources_support_aliases_and_unique_sheet_ids() -> None:
    sources = document_sources(
        {
            "document_urls": (
                "电影库|https://docs.qq.com/sheet/movie\n"
                "https://docs.qq.com/sheet/tv\n"
                "电影库重复|https://docs.qq.com/sheet/movie"
            )
        }
    )
    assert sources == [
        {"name": "电影库", "url": "https://docs.qq.com/sheet/movie"},
        {"name": "文档2", "url": "https://docs.qq.com/sheet/tv"},
    ]
    assert namespaced_sheet_id("file-a", "000001") == namespaced_sheet_id(
        "file-a", "000001"
    )
    assert namespaced_sheet_id("file-a", "000001") != namespaced_sheet_id(
        "file-b", "000001"
    )


def test_discover_namespaces_same_sheet_id_across_documents(tmp_path) -> None:
    store = CatalogStore(tmp_path / "catalog.db")

    class FakeClient:
        @staticmethod
        def convert_file_id(url):
            return "file-movie" if url.endswith("movie") else "file-tv"

        @staticmethod
        def get_sheets(file_id):
            return [
                {
                    "sheet_id": "000001",
                    "title": "电影大全" if file_id == "file-movie" else "剧集大全",
                    "row_count": 10,
                    "column_count": 6,
                    "used_row_count": 10,
                    "used_column_count": 6,
                }
            ]

    config = {
        "document_urls": (
            "电影库|https://docs.qq.com/sheet/movie\n"
            "剧集库|https://docs.qq.com/sheet/tv"
        )
    }
    synchronizer = CatalogSynchronizer(
        store=store,
        client_factory=FakeClient,
        config_provider=lambda: dict(config),
        config_updater=lambda updated: config.update(updated),
        stop_event=Event(),
    )

    sheets = synchronizer.discover_sheets()
    assert len(sheets) == 2
    assert {sheet["title"] for sheet in sheets} == {
        "电影库（电影大全）",
        "剧集库（剧集大全）",
    }
    assert len({sheet["sheet_id"] for sheet in sheets}) == 2
    assert {sheet["remote_sheet_id"] for sheet in sheets} == {"000001"}


def test_discover_combines_regular_and_smart_sheets(tmp_path) -> None:
    store = CatalogStore(tmp_path / "catalog.db")

    class FakeClient:
        @staticmethod
        def convert_file_id(url):
            return "open-file-id"

        @staticmethod
        def get_sheets(file_id):
            return [
                {
                    "sheet_id": "BB08J2",
                    "title": "首页导航",
                    "row_count": 20,
                    "column_count": 6,
                    "used_row_count": 20,
                    "used_column_count": 6,
                }
            ]

    class FakeMcpClient:
        @staticmethod
        def get_sheet_info(file_id):
            assert file_id == "DUG5mV2p1SUh2bHhT"
            return [
                {
                    "sheet_id": "BB08J2",
                    "title": "首页导航",
                    "sheet_type": "worksheet",
                },
                {
                    "sheet_id": "ss_nvtytt",
                    "title": "电影区（新）",
                    "sheet_type": "smartsheet",
                },
            ]

    config = {
        "document_urls": (
            "混合文档|https://docs.qq.com/sheet/DUG5mV2p1SUh2bHhT"
            "?tab=ss_nvtytt&viewId=vvYzbT"
        )
    }
    synchronizer = CatalogSynchronizer(
        store=store,
        client_factory=FakeClient,
        mcp_client_factory=FakeMcpClient,
        config_provider=lambda: dict(config),
        config_updater=lambda updated: config.update(updated),
        stop_event=Event(),
    )

    sheets = synchronizer.discover_sheets()

    assert len(sheets) == 2
    smart = next(sheet for sheet in sheets if sheet["source_kind"] == "smartsheet")
    regular = next(sheet for sheet in sheets if sheet["source_kind"] == "worksheet")
    assert smart["remote_sheet_id"] == "ss_nvtytt"
    assert smart["mcp_file_id"] == "DUG5mV2p1SUh2bHhT"
    assert smart["view_id"] == "vvYzbT"
    assert regular["row_count"] == 20


def test_parser_recognizes_headers_and_share_link() -> None:
    headers = ["影片名称", "资源版本", "资源链接", "类型", "豆瓣评分", "年份"]
    mapping = CatalogParser.identify_headers(headers)
    resource = CatalogParser.parse_row(
        sheet_id="sheet-a",
        sheet_title="电影大全",
        group_name="电影合集",
        row_number=2,
        row=[
            "流浪地球",
            "4K HDR",
            "提取链接：https://115.com/s/example?password=abcd",
            "电影",
            "7.9",
            "2019",
        ],
        header_map=mapping,
    )
    assert resource is not None
    assert resource["title"] == "流浪地球"
    assert resource["share_url"] == "https://115.com/s/example?password=abcd"
    assert resource["group_name"] == "电影合集"


def test_parser_prioritizes_share_path_across_alternate_domains() -> None:
    resource = CatalogParser.parse_row(
        sheet_id="sheet-a",
        sheet_title="星火4K全站资源",
        group_name="星火",
        row_number=2,
        row=[
            "龙虎金刚",
            "https://example.com/details/123",
            "https://115cdn.com/s/swsbevb3hkk?password=sk4k&",
        ],
        header_map={"title": 0, "share_url": 1},
    )
    assert resource is not None
    assert resource["share_url"] == ("https://115cdn.com/s/swsbevb3hkk?password=sk4k&")


def test_parser_accepts_ed2k_and_magnet_rows() -> None:
    ed2k = (
        "ed2k://|file|Example.Movie.2026.iso|42714660864|"
        "19B8C66BBDCE9D7B920015F96FDC113C|/"
    )
    ed2k_resource = CatalogParser.parse_row(
        sheet_id="sheet-offline",
        sheet_title="混合资源",
        group_name="离线",
        row_number=2,
        row=["示例电影", ed2k],
        header_map={"title": 0, "share_url": 1},
        media_mode="movie",
    )
    magnet_resource = CatalogParser.parse_row(
        sheet_id="sheet-offline",
        sheet_title="混合资源",
        group_name="离线",
        row_number=3,
        row=["示例电影2", "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"],
        header_map={"title": 0, "share_url": 1},
        media_mode="movie",
    )

    assert ed2k_resource is not None
    assert ed2k_resource["share_url"] == ed2k
    assert magnet_resource is not None
    assert magnet_resource["share_url"].startswith("magnet:?")


def test_mixed_sheet_splits_tv_rows_into_separate_group() -> None:
    mapping = CatalogParser.identify_headers(
        ["影片名称", "资源版本", "资源链接", "类型", "年份"]
    )
    tv_resource = CatalogParser.parse_row(
        sheet_id="spark",
        sheet_title="星火4K全站资源",
        group_name="星火",
        row_number=2,
        row=[
            "林肯律师",
            "4K",
            "https://115.com/s/tv",
            "欧美剧集",
            "2022",
        ],
        header_map=mapping,
        media_mode="mixed",
    )
    movie_resource = CatalogParser.parse_row(
        sheet_id="spark",
        sheet_title="星火4K全站资源",
        group_name="星火",
        row_number=3,
        row=[
            "流浪地球",
            "4K",
            "https://115.com/s/movie",
            "科幻",
            "2019",
        ],
        header_map=mapping,
        media_mode="mixed",
    )

    assert tv_resource is not None
    assert tv_resource["media_type"] == "电视剧"
    assert tv_resource["group_name"] == "星火-剧集"
    assert movie_resource is not None
    assert movie_resource["media_type"] == "电影"
    assert movie_resource["group_name"] == "星火"


def test_fixed_sheet_mode_overrides_row_type_column() -> None:
    mapping = {"title": 0, "share_url": 1, "media_type": 2}
    movie_resource = CatalogParser.parse_row(
        sheet_id="movie",
        sheet_title="自定义",
        group_name="电影库",
        row_number=2,
        row=["测试", "https://115.com/s/movie", "剧集"],
        header_map=mapping,
        media_mode="movie",
    )
    tv_resource = CatalogParser.parse_row(
        sheet_id="tv",
        sheet_title="自定义",
        group_name="剧集库",
        row_number=2,
        row=["测试", "https://115.com/s/tv", "电影"],
        header_map=mapping,
        media_mode="tv",
    )

    assert movie_resource is not None
    assert movie_resource["media_type"] == "电影"
    assert movie_resource["group_name"] == "电影库"
    assert tv_resource is not None
    assert tv_resource["media_type"] == "电视剧"
    assert tv_resource["group_name"] == "剧集库"


def test_tencent_range_limits_never_exceed_cell_limit() -> None:
    assert TencentDocumentClient.safe_page_rows(1000, 6) == 1000
    assert TencentDocumentClient.safe_page_rows(1000, 20) == 500
    assert TencentDocumentClient.safe_page_rows(1000, 200) == 50
    assert TencentDocumentClient.column_name(1) == "A"
    assert TencentDocumentClient.column_name(27) == "AA"


def test_cell_scalar_prefers_hyperlink_url() -> None:
    value = {"text": "115资源", "url": "https://115.com/s/example"}
    assert TencentDocumentClient._extract_scalar(value) == "https://115.com/s/example"


def test_smart_record_values_support_text_url_options_and_numbers() -> None:
    values = TencentDocumentMcpClient.record_values(
        {
            "field_values": [
                {
                    "field": "电影名称",
                    "text_value": {"items": [{"text": "血爱杀手", "type": "text"}]},
                },
                {
                    "field": "磁力链接",
                    "url_value": {
                        "items": [
                            {
                                "link": "https://magnet:?xt=urn:btih:abc",
                                "text": "magnet:?xt=urn:btih:abc",
                            }
                        ]
                    },
                },
                {
                    "field": "电影类型",
                    "option_value": {"items": [{"text": "动作"}]},
                },
                {"field": "上映年份", "number_value": 2026},
            ]
        }
    )

    assert values == {
        "电影名称": "血爱杀手",
        "磁力链接": "magnet:?xt=urn:btih:abc",
        "电影类型": "动作",
        "上映年份": "2026",
    }


def test_smart_sheet_sync_uses_offset_checkpoint_and_normalizes_links(tmp_path) -> None:
    store = CatalogStore(tmp_path / "catalog.db")
    store.upsert_sheets(
        [
            {
                "sheet_id": "smart-sheet",
                "title": "文档（电影区）",
                "remote_sheet_id": "ss_movies",
                "source_kind": "smartsheet",
                "mcp_file_id": "encoded-file",
                "view_id": "view-1",
            }
        ]
    )

    class FakeClient:
        pass

    class FakeMcpClient:
        @staticmethod
        def get_fields(file_id, sheet_id, view_id=""):
            return [
                "电影名称",
                "资源描述",
                "网盘链接/115离线下载",
                "磁力链接",
                "电影类型",
                "上映年份",
            ]

        @staticmethod
        def record_values(record):
            return TencentDocumentMcpClient.record_values(record)

        @staticmethod
        def get_records(file_id, sheet_id, offset, limit, view_id=""):
            records = [
                {
                    "field_values": [
                        {"field": "电影名称", "text_value": {"items": [{"text": "电影甲"}]}},
                        {"field": "资源描述", "text_value": {"items": [{"text": "4K"}]}},
                        {"field": "网盘链接/115离线下载", "url_value": {"items": [{"link": "https://115cdn.com/s/example"}]}},
                        {"field": "电影类型", "option_value": {"items": [{"text": "动作"}]}},
                        {"field": "上映年份", "number_value": 2025},
                    ]
                },
                {
                    "field_values": [
                        {"field": "电影名称", "text_value": {"items": [{"text": "电影乙"}]}},
                        {"field": "磁力链接", "url_value": {"items": [{"link": "https://magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", "text": "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"}]}},
                    ]
                },
            ]
            record = records[offset : offset + 1]
            next_offset = offset + len(record)
            return {
                "records": record,
                "total": 2,
                "next": next_offset,
                "has_more": next_offset < 2,
                "requested_rows": 1,
            }

    config = {
        "pages_per_run": 1,
        "page_rows": 1,
        "sheet_smart_sheet_enabled": True,
        "sheet_smart_sheet_group": "智能电影",
        "sheet_smart_sheet_media_mode": "movie",
    }
    synchronizer = CatalogSynchronizer(
        store=store,
        client_factory=FakeClient,
        mcp_client_factory=FakeMcpClient,
        config_provider=lambda: dict(config),
        config_updater=lambda updated: config.update(updated),
        stop_event=Event(),
    )

    assert synchronizer.sync()["status"] == "paused"
    assert store.get_sheet("smart-sheet")["checkpoint_row"] == 2
    assert synchronizer.sync()["status"] == "completed"
    with store.connection() as connection:
        resources = [dict(row) for row in connection.execute(
            "SELECT title, share_url FROM resource ORDER BY row_number"
        ).fetchall()]
    assert resources == [
        {"title": "电影甲", "share_url": "https://115cdn.com/s/example"},
        {
            "title": "电影乙",
            "share_url": "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
        },
    ]
    assert store.get_sheet("smart-sheet")["row_count"] == 2


def test_sync_continues_next_incomplete_sheet_before_new_round(tmp_path) -> None:
    store = CatalogStore(tmp_path / "catalog.db")
    store.upsert_sheets(
        [
            {
                "sheet_id": "sheet-a",
                "title": "A电影",
                "row_count": 2,
                "column_count": 3,
                "used_row_count": 2,
                "used_column_count": 3,
            },
            {
                "sheet_id": "sheet-b",
                "title": "B电影",
                "row_count": 2,
                "column_count": 3,
                "used_row_count": 2,
                "used_column_count": 3,
            },
        ]
    )
    calls = []

    class FakeClient:
        def get_range(self, file_id, sheet_id, start_row, row_count, column_count):
            calls.append(sheet_id)
            return {
                "rows": [
                    ["影片名称", "资源版本", "资源链接"],
                    [sheet_id, "4K", f"https://115.com/s/{sheet_id}"],
                ],
                "requested_rows": 2,
            }

    config = {
        "file_id": "file-id",
        "pages_per_run": 1,
        "page_rows": 2,
        "max_columns": 3,
        "sheet_sheet_a_enabled": True,
        "sheet_sheet_a_group": "电影合集",
        "sheet_sheet_b_enabled": True,
        "sheet_sheet_b_group": "电影合集",
    }
    synchronizer = CatalogSynchronizer(
        store=store,
        client_factory=FakeClient,
        config_provider=lambda: dict(config),
        config_updater=lambda updated: config.update(updated),
        stop_event=Event(),
    )

    assert synchronizer.sync()["status"] == "paused"
    assert synchronizer.sync()["status"] == "completed"
    assert synchronizer.sync()["status"] == "paused"
    assert calls == ["sheet-a", "sheet-b", "sheet-a"]
