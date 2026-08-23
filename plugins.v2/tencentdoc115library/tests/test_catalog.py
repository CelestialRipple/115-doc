from threading import Event

from tencentdoc115library.catalog import (
    CatalogParser,
    CatalogSynchronizer,
    default_group_for_title,
)
from tencentdoc115library.client import TencentDocumentClient
from tencentdoc115library.store import CatalogStore


def test_default_groups_match_expected_library_layout() -> None:
    assert default_group_for_title("电影大全") == "电影合集"
    assert default_group_for_title("动画") == "电影合集"
    assert default_group_for_title("纪录片") == "电影合集"
    assert default_group_for_title("剧集") == "剧集"
    assert default_group_for_title("星火4K全站资源") == "星火"
    assert default_group_for_title("蚂蚁4K") == "蚂蚁"


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


def test_tencent_range_limits_never_exceed_cell_limit() -> None:
    assert TencentDocumentClient.safe_page_rows(1000, 6) == 1000
    assert TencentDocumentClient.safe_page_rows(1000, 20) == 500
    assert TencentDocumentClient.safe_page_rows(1000, 200) == 50
    assert TencentDocumentClient.column_name(1) == "A"
    assert TencentDocumentClient.column_name(27) == "AA"


def test_cell_scalar_prefers_hyperlink_url() -> None:
    value = {"text": "115资源", "url": "https://115.com/s/example"}
    assert TencentDocumentClient._extract_scalar(value) == "https://115.com/s/example"


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
