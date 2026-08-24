"""在官方 MoviePilot 镜像中运行的插件装载冒烟测试。"""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN_ROOT))

from tencentdoc115library import TencentDoc115Library
from tencentdoc115library.download_marker import parse_download_marker
from tencentdoc115library import library as library_module
from tencentdoc115library.resolver import ShareResolutionError
from app.plugins import _PluginBase
from app.schemas.types import MediaType, TorrentStatus


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
        assert "/resources/retry-all" in api_paths
        assert "/gateway/restart" in api_paths
        page_text = str(plugin.get_page())
        assert "STRM 与刮削进度" in page_text
        assert "重试全部失败并生成" in page_text
        assert "内置直链网关" in page_text

        output_root = Path(temporary_directory) / "output"
        output_root.mkdir()
        (output_root / "usage.bin").write_bytes(b"12")

        original_media_chain = library_module.MediaChain
        original_meta_info = library_module.MetaInfo

        class LegacyMediaChain:
            @staticmethod
            def recognize_by_meta(metainfo, obtain_images=False):
                assert obtain_images is True
                return SimpleNamespace(type=metainfo.type)

        library_module.MediaChain = LegacyMediaChain
        library_module.MetaInfo = lambda title, subtitle=None: SimpleNamespace(
            title=title,
            subtitle=subtitle,
            year=None,
            type=None,
        )
        _, recognized = plugin._builder._recognize(
            {"title": "兼容测试", "year": "2024", "version": ""},
            MediaType.MOVIE,
        )
        assert recognized.type == MediaType.MOVIE
        library_module.MediaChain = original_media_chain
        library_module.MetaInfo = original_meta_info

        assert (
            plugin._builder._media_type(
                {"media_type": "电影", "group_name": "电影合集"},
                [{"file_path": "/某剧/Show.S01E02.mkv"}],
            )
            == MediaType.TV
        )
        assert (
            plugin._builder._media_type(
                {"media_type": "", "group_name": "未分类"},
                [{"file_path": "/普通电影/普通电影.2024.mkv"}],
            )
            == MediaType.MOVIE
        )
        library_module.MetaInfo = lambda _title: SimpleNamespace(
            begin_season=None,
            begin_episode=None,
        )
        assert plugin._builder._episode_identity("02.mkv", "/某剧/第一季/02.mkv") == (
            1,
            2,
        )
        assert plugin._builder._episode_identity(
            "Show.S02.iso", "/某剧/Show.S02.iso"
        ) == (2, 1)
        assert plugin._builder._episode_identity(
            "某剧 第一季.mkv", "/某剧/某剧 第一季.mkv"
        ) == (1, 1)
        library_module.MetaInfo = original_meta_info

        original_scraping_chain = library_module.ScrapingChain

        class LegacyScrapingChain:
            @staticmethod
            def scrape_metadata(**kwargs):
                return None

        library_module.ScrapingChain = LegacyScrapingChain
        plugin._builder._scrape(output_root, object(), object())
        library_module.ScrapingChain = original_scraping_chain

        plugin._config["output_size_limit_gb"] = 1 / (1024**3)
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

        original_list_video_files = plugin._resolver.list_video_files
        original_recognize = plugin._builder._recognize

        def invalid_share(_share_url):
            raise ShareResolutionError(
                "115 分享无效或访问码错误",
                status_code=404,
                retryable=False,
            )

        plugin._resolver.list_video_files = invalid_share
        plugin._builder._recognize = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("无效分享不应进入媒体识别")
        )
        invalid_build = plugin._builder.build(limit=1)
        assert invalid_build["failed"] == 1
        invalid_resource = store.get_resource("smoke-resource")
        assert invalid_resource["status"] == "invalid_share"
        assert invalid_resource["strm_status"] == "failed"
        assert invalid_resource["scrape_status"] == "blocked"
        assert not list(output_root.rglob("*.strm"))
        plugin._resolver.list_video_files = original_list_video_files
        plugin._builder._recognize = original_recognize

        original_scrape = plugin._builder._scrape
        movie_files = [
            {
                "file_id": "small",
                "file_name": "花絮.mp4",
                "file_path": "/花絮.mp4",
                "file_size": 100,
            },
            {
                "file_id": "main",
                "file_name": "测试电影.mkv",
                "file_path": "/测试电影.mkv",
                "file_size": 1000,
            },
        ]
        store.update_resource_status(
            "smoke-resource",
            "pending",
            strm_status="pending",
            scrape_status="pending",
        )
        plugin._resolver.list_video_files = lambda _share_url: movie_files
        plugin._builder._recognize = lambda *_args, **_kwargs: (
            object(),
            SimpleNamespace(
                title="测试电影",
                year="2024",
                type=MediaType.MOVIE,
                media_source="themoviedb",
                source="themoviedb",
                media_id="movie-1",
                tmdb_id="1",
            ),
        )

        def successful_scrape(*_args, **_kwargs):
            scraping_resource = store.get_resource("smoke-resource")
            assert scraping_resource["strm_status"] == "ready"
            assert scraping_resource["scrape_status"] == "scraping"

        plugin._builder._scrape = successful_scrape
        successful_build = plugin._builder.build(limit=1)
        assert successful_build["success"] == 1
        resource = store.get_resource("smoke-resource")
        movie_path = resource["strm_path"]
        assert resource["status"] == "ready"
        assert resource["strm_status"] == "ready"
        assert resource["scrape_status"] == "ready"
        plugin._resolver.list_video_files = original_list_video_files
        plugin._builder._recognize = original_recognize
        plugin._builder._scrape = original_scrape
        assert "file_id=main" in Path(movie_path).read_text(encoding="utf-8")
        assert store.get_resource_file("smoke-resource", "main")

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
        assert (
            module["list_torrents"](
                status=TorrentStatus.TRANSFER,
                downloader="腾讯文档115直链",
            )
            == []
        )

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
