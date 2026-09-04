from threading import Event

from tencentdoc115library.manual import ManualLibraryImporter
from tencentdoc115library.store import CatalogStore


class FakeResolver:
    def __init__(self):
        self.calls = []

    def list_video_files(self, share_url):
        self.calls.append(share_url)
        return [
            {
                "file_id": "file-1",
                "file_name": "Example.Movie.2024.mkv",
                "file_path": "/Example Movie (2024)/Example.Movie.2024.mkv",
                "file_size": 1024,
            }
        ]


def test_parse_manual_links_with_optional_title_and_year():
    entries = ManualLibraryImporter.parse_entries(
        "示例电影|2024|https://115cdn.com/s/abc123?password=xy12\n"
        "https://115.com/s/def456?password=zz99"
    )

    assert len(entries) == 2
    assert entries[0]["title"] == "示例电影"
    assert entries[0]["year"] == "2024"
    assert entries[0]["share_code"] == "abc123"
    assert entries[1]["title"] == ""


def test_manual_import_persists_files_and_skips_unchanged_share(tmp_path):
    store = CatalogStore(tmp_path / "catalog.db")
    resolver = FakeResolver()
    importer = ManualLibraryImporter(store, resolver, Event())
    raw = "https://115cdn.com/s/abc123?password=xy12"

    first = importer.import_links(raw, "朋友分享", "movie")
    second = importer.import_links(raw, "朋友分享", "movie")

    assert first["imported"] == 1
    assert len(first["queued_ids"]) == 1
    assert second["imported"] == 0
    assert second["unchanged"] == 1
    resource = store.get_resource(first["queued_ids"][0])
    assert resource["title"] == "Example Movie"
    assert resource["year"] == "2024"
    assert resource["group_name"] == "朋友分享"
    assert resource["status"] == "pending"
    assert store.list_resource_files(resource["resource_id"])[0]["file_id"] == "file-1"


def test_targeted_build_candidates_do_not_consume_other_pending_rows(tmp_path):
    store = CatalogStore(tmp_path / "catalog.db")
    resolver = FakeResolver()
    importer = ManualLibraryImporter(store, resolver, Event())
    first = importer.import_links(
        "电影甲|https://115.com/s/first?password=1111",
        "手动",
        "movie",
    )
    second = importer.import_links(
        "电影乙|https://115.com/s/second?password=2222",
        "手动",
        "movie",
    )

    rows = store.list_build_candidates(
        limit=10,
        resource_ids=second["queued_ids"],
    )

    assert [row["resource_id"] for row in rows] == second["queued_ids"]
    assert first["queued_ids"][0] != rows[0]["resource_id"]
