"""在官方 MoviePilot 镜像中运行的插件装载冒烟测试。"""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN_ROOT))

from tencentdoc115library import TencentDoc115Library
from tencentdoc115library.download_marker import parse_download_marker
from tencentdoc115library import library as library_module
from app.plugins import _PluginBase
from app.schemas.types import TorrentStatus


def main() -> None:
    with TemporaryDirectory() as temporary_directory:
        _PluginBase.__init__ = lambda self: None
        plugin = TencentDoc115Library()
        plugin.get_data_path = lambda: Path(temporary_directory)
        plugin.update_config = lambda config: None
        plugin.init_plugin(
            {
                "enabled": True,
                "native_search_enabled": True,
                "search_ready_only": True,
                "output_root": str(Path(temporary_directory) / "output"),
                "output_size_limit_gb": 1,
            }
        )
        module = plugin.get_module()
        expected_methods = {
            "search_torrents",
            "download",
            "list_torrents",
            "start_torrents",
            "stop_torrents",
            "remove_torrents",
            "set_torrents_tag",
            "transfer_completed",
        }
        assert expected_methods.issubset(module)
        protected_apis = [
            api for api in plugin.get_api() if not api.get("allow_anonymous")
        ]
        assert protected_apis
        assert all(api.get("auth") == "bear" for api in protected_apis)
        api_paths = {api["path"] for api in plugin.get_api()}
        assert "/sync-all" in api_paths
        assert "/tasks/stop" in api_paths
        assert plugin.get_page()

        output_root = Path(temporary_directory) / "output"
        output_root.mkdir()
        (output_root / "usage.bin").write_bytes(b"12")

        original_scraping_chain = library_module.ScrapingChain

        class LegacyScrapingChain:
            @staticmethod
            def scrape_metadata(**kwargs):
                return None

        library_module.ScrapingChain = LegacyScrapingChain
        plugin._builder._scrape(output_root, object(), object())
        library_module.ScrapingChain = original_scraping_chain

        plugin._config["output_size_limit_gb"] = 1 / (1024 ** 3)
        limited_build = plugin._builder.build()
        assert limited_build["status"] == "space_limit"
        assert limited_build["processed"] == 0
        plugin._config["output_size_limit_gb"] = 1

        store = plugin._store
        store.upsert_sheets(
            [
                {
                    "sheet_id": "smoke-sheet",
                    "title": "电影大全",
                    "row_count": 2,
                    "column_count": 6,
                    "used_row_count": 2,
                    "used_column_count": 6,
                }
            ]
        )
        store.configure_sheets(
            {"smoke-sheet": {"enabled": True, "group_name": "电影合集"}}
        )
        checkpoint = store.begin_sheet_scan("smoke-sheet")
        store.save_page(
            "smoke-sheet",
            checkpoint["scan_id"],
            3,
            {},
            [
                {
                    "resource_id": "smoke-resource",
                    "row_number": 2,
                    "title": "测试电影",
                    "version": "4K",
                    "share_url": "https://115.com/s/smoke?password=abcd",
                    "media_type": "电影",
                    "rating": "8.0",
                    "year": "2024",
                    "group_name": "电影合集",
                    "row_hash": "smoke-hash",
                }
            ],
            1,
        )
        store.update_resource_status(
            "smoke-resource",
            "ready",
            strm_path="/media/电影合集/测试电影/测试电影.strm",
        )

        results = plugin.search_torrents({}, "测试电影")
        assert len(results) == 1
        assert results[0].site_downloader == "腾讯文档115直链"
        assert parse_download_marker(results[0].enclosure) == "smoke-resource"

        store.upsert_download_task(
            {
                "task_id": "smoke-task",
                "resource_id": "smoke-resource",
                "title": "测试电影",
                "download_dir": "/media/downloads",
                "content_path": "/media/downloads/测试电影.mkv",
            }
        )
        store.update_download_task(
            "smoke-task",
            state="completed",
            total_size=1024,
            downloaded_size=1024,
        )
        completed = module["list_torrents"](
            status=TorrentStatus.TRANSFER,
            downloader="腾讯文档115直链",
        )
        assert len(completed) == 1
        assert completed[0].progress == 100
        module["transfer_completed"](
            "smoke-task",
            downloader="腾讯文档115直链",
        )
        assert module["list_torrents"](
            status=TorrentStatus.TRANSFER,
            downloader="腾讯文档115直链",
        ) == []

        class FakeSynchronizer:
            calls = 0

            def sync(self, mode="manual"):
                self.calls += 1
                if self.calls == 1:
                    return {
                        "status": "paused",
                        "message": "继续",
                        "processed_pages": 2,
                        "processed_rows": 20,
                    }
                return {
                    "status": "completed",
                    "message": "同步完成",
                    "processed_pages": 1,
                    "processed_rows": 10,
                }

        class FakeBuilder:
            calls = 0

            def build(self, known_usage_bytes=None):
                self.calls += 1
                if self.calls == 1:
                    assert known_usage_bytes is None
                    return {
                        "status": "completed",
                        "message": "批次完成",
                        "processed": 2,
                        "success": 2,
                        "failed": 0,
                        "usage_bytes": 1024,
                        "limit_bytes": 2048,
                    }
                assert known_usage_bytes == 1024
                return {
                    "status": "completed",
                    "message": "无待处理资源",
                    "processed": 0,
                    "success": 0,
                    "failed": 0,
                    "usage_bytes": 1024,
                    "limit_bytes": 2048,
                }

        plugin._synchronizer = FakeSynchronizer()
        plugin._builder = FakeBuilder()
        pipeline = plugin._sync_all_and_build()
        assert pipeline["status"] == "completed"
        assert pipeline["synced_pages"] == 3
        assert pipeline["synced_rows"] == 30
        assert pipeline["success"] == 2
        assert plugin._pipeline_snapshot()["phase"] == "completed"
        plugin.stop_service()
        print("MoviePilot plugin smoke test passed")


if __name__ == "__main__":
    main()
