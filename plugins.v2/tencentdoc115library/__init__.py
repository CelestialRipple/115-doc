import asyncio
import re
from concurrent.futures import Future
from html import escape
from secrets import compare_digest, token_urlsafe
from threading import Event, Lock
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode

from apscheduler.triggers.cron import CronTrigger
from fastapi import Body, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response as HttpResponse,
)

from app.log import logger
from app.plugins import _PluginBase
from app.schemas import Response
from app.schemas.types import MediaType

try:
    from app.domain.context import TorrentInfo
except ImportError:
    from app.core.context import TorrentInfo

try:
    from app.runtime.thread import ThreadHelper
except ImportError:
    from app.helper.thread import ThreadHelper

from .catalog import (
    CatalogSynchronizer,
    default_group_for_title,
    default_media_mode_for_title,
    sheet_config_key,
)
from .client import TencentDocumentClient
from .downloader import (
    DIRECT_DOWNLOADER_NAME,
    DirectDownloadManager,
)
from .download_marker import build_download_marker
from .gateway import DirectPlayGateway
from .library import LibraryBuilder
from .manual import MANUAL_SHEET_PREFIX, ManualImportError, ManualLibraryImporter
from .resolver import ShareResolutionError, ShareResolver
from .schemas import (
    BuildActionRequest,
    ManualImportRequest,
    ResourceRetryRequest,
    SyncActionRequest,
)
from .search_bridge import (
    LOCAL_INDEXER_MARKER,
    LocalSearchResults,
    MoviePilotSearchBridge,
)
from .source_link import is_offline_link
from .storage_limit import format_gib
from .store import CatalogStore

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "auto_sync": False,
    "auto_build": False,
    "sync_cron": "0 */6 * * *",
    "build_cron": "*/5 * * * *",
    "document_url": "",
    "document_urls": "",
    "manual_import_links": "",
    "manual_import_group": "自定义",
    "manual_import_media_mode": "mixed",
    "client_id": "",
    "client_secret": "",
    "openid": "",
    "access_token": "",
    "refresh_token": "",
    "page_rows": 1000,
    "pages_per_run": 5,
    "max_columns": 10,
    "build_batch": 20,
    "scrape_workers": 1,
    "separate_source_folders": False,
    "output_root": "/media/tencentdoc115",
    "output_size_limit_gb": 0,
    "public_base_url": "http://127.0.0.1:3000",
    "playback_token": "",
    "scrape_metadata": True,
    "native_search_enabled": True,
    "native_search_scope": "all",
    "search_ready_only": True,
    "search_page_size": 50,
    "reuse_p115_cookie": True,
    "p115_cookie": "",
    "share_page_size": 1000,
    "share_max_files": 5000,
    "share_max_depth": 20,
    "request_interval": 0.5,
    "request_retries": 4,
    "direct_url_cache_ttl": 6000,
    "offline_playback_enabled": True,
    "offline_temp_path": "/temp/tencentdoc115library",
    "offline_wait_seconds": 60,
    "offline_poll_seconds": 2,
    "offline_retention_hours": 24,
    "offline_cleanup_cron": "*/30 * * * *",
    "offline_clear_recycle": True,
    "download_retries": 4,
    "download_chunk_size": 1048576,
    "video_extensions": ".mp4,.mkv,.ts,.m2ts,.avi,.mov,.wmv,.flv,.webm,.iso",
    "direct_gateway_enabled": False,
    "direct_gateway_port": 8097,
    "emby_internal_url": "http://127.0.0.1:8096",
    "emby_api_key": "",
    "emby_strm_paths": "/data/tencentdoc115",
    "emby_path_mappings": "",
    "clear_confirmation": False,
}


class TencentDoc115Library(_PluginBase):
    """将腾讯文档中的 115 分享资源构建为可按需播放的媒体库。"""

    plugin_name = "腾讯文档115媒体库"
    plugin_desc = "同步115分享、磁力和ED2K，使用MoviePilot刮削并按需返回115直链。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/src/assets/images/misc/u115.png"
    plugin_version = "0.11.1"
    plugin_author = "Codex"
    author_url = "https://github.com/CelestialRipple/115-doc"
    plugin_config_prefix = "tencentdoc115library_"
    plugin_order = 25
    auth_level = 1

    def __init__(self) -> None:
        """初始化插件运行状态。"""
        super().__init__()
        self._config: Dict[str, Any] = dict(DEFAULT_CONFIG)
        self._enabled = False
        self._stop_event = Event()
        self._pause_event = Event()
        self._task_lock = Lock()
        self._pipeline_lock = Lock()
        self._search_id_lock = Lock()
        self._imdb_tmdb_cache: Dict[str, Tuple[str, ...]] = {}
        self._future: Optional[Future] = None
        self._task_name = ""
        self._task_state = "idle"
        self._task_message = "没有后台任务"
        self._resume_spec: Optional[Tuple[Any, Tuple[Any, ...], Dict[str, Any]]] = None
        self._store: Optional[CatalogStore] = None
        self._synchronizer: Optional[CatalogSynchronizer] = None
        self._resolver: Optional[ShareResolver] = None
        self._builder: Optional[LibraryBuilder] = None
        self._manual_importer: Optional[ManualLibraryImporter] = None
        self._direct_downloader: Optional[DirectDownloadManager] = None
        self._gateway: Optional[DirectPlayGateway] = None
        self._search_bridge = MoviePilotSearchBridge(
            lambda: self._enabled
            and bool(self._config.get("native_search_enabled", True))
        )
        self._pipeline_status: Dict[str, Any] = {
            "phase": "idle",
            "message": "尚未启动同步全部任务",
            "synced_pages": 0,
            "synced_rows": 0,
            "built": 0,
            "success": 0,
            "failed": 0,
        }

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """加载配置并初始化持久化和任务组件。"""
        self.stop_service()
        self._stop_event = Event()
        self._pause_event.clear()
        self._config = {**DEFAULT_CONFIG, **(config or {})}
        config_changed = False
        # 清理旧版本曾保存的转存和 ISO 实验开关，避免升级后继续显示或生效。
        for legacy_key in (
            "iso_fresh_redirect",
            "iso_blank_user_agent",
            "playback_transfer_enabled",
            "playback_transfer_path",
            "playback_transfer_retention_hours",
            "playback_transfer_wait_seconds",
            "playback_transfer_permanent_delete",
            "playback_transfer_recycle_password",
            "playback_transfer_cleanup_cron",
            "playback_iso_open_api",
        ):
            if legacy_key in self._config:
                self._config.pop(legacy_key, None)
                config_changed = True
        if not self._config.get("playback_token"):
            self._config["playback_token"] = token_urlsafe(32)
            config_changed = True
        if config_changed:
            self.update_config(dict(self._config))
        self._enabled = bool(self._config.get("enabled"))
        data_path = self.get_data_path()
        self._store = CatalogStore(data_path / "catalog.db")
        retried = self._store.retry_errors_containing(
            "unexpected keyword argument 'mtype'"
        )
        if retried:
            logger.info(f"已自动重新排队 {retried} 条 V2 识别接口兼容失败资源")
        repaired_builds = sum(
            self._store.retry_errors_containing(fragment)
            for fragment in (
                "无法识别任何剧集编号",
                "File name too long",
            )
        )
        if repaired_builds:
            logger.info(f"已自动重新排队 {repaired_builds} 条可修复的构建失败资源")
        self._resolver = ShareResolver(
            store=self._store,
            config_provider=self._current_config,
        )
        self._gateway = DirectPlayGateway(
            config_provider=self._current_config,
            resolver=self._resolver,
        )
        self._synchronizer = CatalogSynchronizer(
            store=self._store,
            client_factory=self._create_client,
            config_provider=self._current_config,
            config_updater=self._replace_config,
            stop_event=self._stop_event,
            pause_event=self._pause_event,
        )
        self._builder = LibraryBuilder(
            store=self._store,
            resolver=self._resolver,
            config_provider=self._current_config,
            stop_event=self._stop_event,
            pause_event=self._pause_event,
        )
        self._manual_importer = ManualLibraryImporter(
            store=self._store,
            resolver=self._resolver,
            stop_event=self._stop_event,
            pause_event=self._pause_event,
        )
        self._direct_downloader = DirectDownloadManager(
            store=self._store,
            resolver=self._resolver,
            config_provider=self._current_config,
        )
        if (
            self._enabled
            and bool(self._config.get("native_search_enabled", True))
            and self._search_bridge.install()
        ):
            logger.info("已启用无站点原生搜索兼容桥接")
        self._gateway.start_background()

    def _current_config(self) -> Dict[str, Any]:
        """返回当前配置的副本。"""
        return dict(self._config)

    def _replace_config(self, config: Dict[str, Any]) -> None:
        """保存同步器发现的工作表和文件 ID 配置。"""
        self._config = {**DEFAULT_CONFIG, **config}
        for legacy_key in (
            "iso_fresh_redirect",
            "iso_blank_user_agent",
            "playback_transfer_enabled",
            "playback_transfer_path",
            "playback_transfer_retention_hours",
            "playback_transfer_wait_seconds",
            "playback_transfer_permanent_delete",
            "playback_transfer_recycle_password",
            "playback_transfer_cleanup_cron",
            "playback_iso_open_api",
        ):
            self._config.pop(legacy_key, None)
        self.update_config(dict(self._config))

    def _save_tokens(self, token_data: Dict[str, Any]) -> None:
        """持久化腾讯文档刷新后的令牌。"""
        self._config["access_token"] = str(token_data.get("access_token") or "")
        if token_data.get("refresh_token"):
            self._config["refresh_token"] = str(token_data["refresh_token"])
        if token_data.get("access_token_expires_at"):
            self._config["access_token_expires_at"] = float(
                token_data["access_token_expires_at"]
            )
        if token_data.get("open_id"):
            self._config["openid"] = str(token_data["open_id"])
        self.update_config(dict(self._config))

    def _create_client(self) -> TencentDocumentClient:
        """按当前配置创建腾讯文档客户端。"""
        config = self._current_config()
        return TencentDocumentClient(
            client_id=str(config.get("client_id") or ""),
            open_id=str(config.get("openid") or ""),
            access_token=str(config.get("access_token") or ""),
            client_secret=str(config.get("client_secret") or ""),
            refresh_token=str(config.get("refresh_token") or ""),
            access_token_expires_at=float(config.get("access_token_expires_at") or 0),
            retry_count=int(config.get("request_retries") or 4),
            on_token_refresh=self._save_tokens,
        )

    def _submit(
        self,
        name: str,
        function: Any,
        *args: Any,
        resume_function: Optional[Any] = None,
        resume_args: Optional[Tuple[Any, ...]] = None,
        resume_kwargs: Optional[Dict[str, Any]] = None,
        resume_paused: bool = False,
        **kwargs: Any,
    ) -> Response:
        """把长任务提交到 MoviePilot 共享线程池。"""
        with self._task_lock:
            if self._future and not self._future.done():
                return Response(success=False, message="已有同步或构建任务正在运行")
            if (
                self._task_state == "paused"
                and self._resume_spec is not None
                and not resume_paused
            ):
                return Response(
                    success=False,
                    message="后台任务已暂停；请先点击恢复，或结束暂停任务后再启动其它操作",
                )
            self._stop_event.clear()
            self._pause_event.clear()
            self._task_name = name
            self._task_state = "running"
            self._task_message = f"{name}已启动"
            self._resume_spec = (
                resume_function or function,
                tuple(args if resume_args is None else resume_args),
                dict(kwargs if resume_kwargs is None else resume_kwargs),
            )
            self._future = ThreadHelper().submit(function, *args, **kwargs)
            self._future.add_done_callback(lambda future: self._task_done(name, future))
        return Response(success=True, message=f"{name}已在后台启动")

    def _task_done(self, name: str, future: Future) -> None:
        """记录后台任务异常，但不泄露任何凭据。"""
        try:
            result = future.result()
            logger.info(f"{name}完成：{result}")
        except Exception as error:
            logger.error(f"{name}失败：{error}")
            with self._task_lock:
                if self._future is not future:
                    return
                self._task_state = "failed"
                self._task_message = f"{name}失败：{error}"
                self._resume_spec = None
            return
        result_status = str(result.get("status") or "") if isinstance(result, dict) else ""
        with self._task_lock:
            # 前一任务结束与下一任务启动极近时，旧回调不得覆盖新任务状态。
            if self._future is not future:
                return
            if result_status in {"stopped", "interrupted"} or self._stop_event.is_set():
                self._task_state = "stopped"
                self._task_message = f"{name}已停止；检查点和已生成内容已保留"
                self._resume_spec = None
            elif result_status == "paused" or self._pause_event.is_set():
                self._task_state = "paused"
                self._task_message = f"{name}已暂停；点击恢复可从断点继续"
            elif result_status in {"failed", "configuration_error"}:
                self._task_state = "failed"
                self._task_message = str(result.get("message") or f"{name}失败")
                self._resume_spec = None
            else:
                self._task_state = "completed"
                self._task_message = str(result.get("message") or f"{name}已完成")
                self._resume_spec = None

    def _task_snapshot(self) -> Dict[str, Any]:
        """返回后台任务控制状态。"""
        with self._task_lock:
            running = bool(self._future and not self._future.done())
            return {
                "name": self._task_name,
                "state": self._task_state,
                "message": self._task_message,
                "running": running,
                "can_resume": self._task_state == "paused" and self._resume_spec is not None,
            }

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def get_module(self) -> Dict[str, Any]:
        """把本地目录检索和 115 直链任务接入 MoviePilot 原生流程。"""
        if not self._config.get("native_search_enabled", True):
            return {}
        if not self._direct_downloader:
            return {
                "search_torrents": self.search_torrents,
                "async_search_torrents": self.async_search_torrents,
            }
        return {
            "search_torrents": self.search_torrents,
            "async_search_torrents": self.async_search_torrents,
            "download": self._direct_downloader.download,
            "list_torrents": self._direct_downloader.list_torrents,
            "start_torrents": self._direct_downloader.start_torrents,
            "stop_torrents": self._direct_downloader.stop_torrents,
            "remove_torrents": self._direct_downloader.remove_torrents,
            "set_torrents_tag": self._direct_downloader.set_torrents_tag,
            "transfer_completed": self._direct_downloader.transfer_completed,
        }

    @staticmethod
    def _resource_media_type(resource: Dict[str, Any]) -> MediaType:
        """按表格类型和分组判断检索结果的影视类型。"""
        save_mode = str(resource.get("save_media_mode") or "").strip().lower()
        if save_mode == "tv":
            return MediaType.TV
        if save_mode == "movie":
            return MediaType.MOVIE
        raw_type = str(
            resource.get("detected_media_type")
            or resource.get("media_type")
            or ""
        ).lower()
        group_name = str(
            resource.get("detected_group_name")
            or resource.get("group_name")
            or ""
        ).lower()
        if any(
            keyword in raw_type or keyword in group_name
            for keyword in ("电视剧", "剧集", "连续剧", "tv", "番剧")
        ):
            return MediaType.TV
        return MediaType.MOVIE

    def search_torrents(
        self,
        site: dict,
        keyword: str,
        mtype: Optional[MediaType] = None,
        page: Optional[int] = 0,
    ) -> List[TorrentInfo]:
        """搜索插件本地 SQLite 镜像；不会访问腾讯文档或 115。"""
        local_indexer = bool((site or {}).get(LOCAL_INDEXER_MARKER))
        if not self._store or not str(keyword or "").strip():
            return LocalSearchResults([]) if local_indexer else []
        page_size = min(
            max(int(self._config.get("search_page_size") or 50), 1),
            100,
        )
        page_index = max(int(page or 0), 0)
        search_scope = str(
            self._config.get("native_search_scope") or "all"
        ).strip().lower()
        if search_scope not in {"unbuilt", "all", "ready"}:
            search_scope = "all"
        normalized_keyword = str(keyword or "").strip()
        imdb_id = (
            normalized_keyword
            if re.fullmatch(r"tt\d+", normalized_keyword, re.I)
            else ""
        )
        tmdb_ids = self._tmdb_ids_for_imdb(imdb_id, mtype) if imdb_id else []
        resources = self._store.search_resources(
            keyword=keyword,
            limit=page_size,
            offset=page_index * page_size,
            ready_only=(
                search_scope == "ready"
                or (
                    "native_search_scope" not in self._config
                    and bool(self._config.get("search_ready_only", True))
                )
            ),
            unbuilt_only=search_scope == "unbuilt",
            imdb_id=imdb_id,
            tmdb_ids=tmdb_ids,
        )
        results: List[TorrentInfo] = []
        for resource in resources:
            media_type = self._resource_media_type(resource)
            if mtype and media_type != mtype:
                sheet = self._store.get_sheet(
                    str(resource.get("sheet_id") or "")
                ) or {}
                unresolved_mixed = (
                    str(sheet.get("media_mode") or "").lower() == "mixed"
                    and not str(resource.get("detected_media_type") or "").strip()
                )
                if not unresolved_mixed:
                    continue
                media_type = mtype
            title = str(resource.get("title") or "").strip()
            year = str(resource.get("year") or "").strip()
            version = str(resource.get("version") or "").strip()
            display_title = f"{title} ({year})" if year else title
            effective_group = str(
                resource.get("detected_group_name")
                or resource.get("save_group_name")
                or resource.get("group_name")
                or "腾讯文档"
            )
            is_unbuilt = str(resource.get("strm_status") or "") != "ready"
            details = [
                effective_group,
                "待保存到媒体库" if is_unbuilt else "STRM已生成",
                version,
                f"评分 {resource['rating']}" if resource.get("rating") else "",
            ]
            results.append(
                TorrentInfo(
                    site_name="腾讯文档115媒体库",
                    site_order=0,
                    site_downloader=DIRECT_DOWNLOADER_NAME,
                    title=display_title,
                    description=" · ".join(item for item in details if item),
                    enclosure=build_download_marker(resource["resource_id"]),
                    page_url=self._library_save_url(resource["resource_id"]),
                    size=0,
                    seeders=1,
                    labels=[
                        "腾讯文档",
                        "115离线" if is_offline_link(resource["share_url"]) else "115分享",
                        effective_group,
                        "右下角ⓘ保存" if is_unbuilt else "已入库",
                    ],
                    pri_order=100,
                    category=media_type.value,
                    imdbid=imdb_id or str(resource.get("imdb_id") or "") or None,
                )
            )
        if local_indexer:
            return LocalSearchResults(results)
        return results

    def _tmdb_ids_for_imdb(
        self,
        imdb_id: str,
        media_type: Optional[MediaType],
    ) -> List[str]:
        """把详情页的 IMDb 搜索词映射为目录中已有的 TMDB ID。"""
        cache_key = (
            f"{imdb_id.lower()}::"
            f"{getattr(media_type, 'value', media_type) or ''}"
        )
        with self._search_id_lock:
            cached = self._imdb_tmdb_cache.get(cache_key)
            if cached is not None:
                return list(cached)
            ids: List[str] = []
            try:
                from app.modules.themoviedb.tmdbv3api import Find

                found = Find().find_by_imdb_id(imdb_id) or {}
                result_keys = (
                    ("tv_results",)
                    if media_type == MediaType.TV
                    else ("movie_results",)
                    if media_type == MediaType.MOVIE
                    else ("movie_results", "tv_results")
                )
                for result_key in result_keys:
                    for item in found.get(result_key) or []:
                        tmdb_id = str(item.get("id") or "").strip()
                        if tmdb_id and tmdb_id not in ids:
                            ids.append(tmdb_id)
            except Exception as error:
                logger.warning(f"IMDb 搜索映射 TMDB 失败：{imdb_id} - {error}")
            if len(self._imdb_tmdb_cache) >= 256:
                self._imdb_tmdb_cache.pop(next(iter(self._imdb_tmdb_cache)))
            self._imdb_tmdb_cache[cache_key] = tuple(ids)
            return ids

    def _library_save_url(self, resource_id: str) -> str:
        """生成带插件密钥的搜索结果保存确认页地址。"""
        token = str(self._config.get("playback_token") or "")
        query = urlencode({"token": token})
        return (
            "/api/v1/plugin/TencentDoc115Library/"
            f"resources/save/{resource_id}?{query}"
        )

    async def save_search_resource(
        self,
        request: Request,
        resource_id: str,
        token: str = Query(default=""),
    ) -> HttpResponse:
        """显示或执行搜索结果的定向 STRM 入库操作。"""
        expected_token = str(self._config.get("playback_token") or "")
        if not expected_token or not compare_digest(token, expected_token):
            return HTMLResponse("<h2>保存密钥无效</h2>", status_code=401)
        if not self._store or not self._builder:
            return HTMLResponse("<h2>插件尚未初始化</h2>", status_code=503)
        resource = self._store.get_resource(resource_id)
        if not resource or resource.get("status") == "removed":
            return HTMLResponse("<h2>资源不存在或已移除</h2>", status_code=404)
        title = str(resource.get("title") or resource_id)
        group_name = str(
            resource.get("detected_group_name")
            or resource.get("save_group_name")
            or resource.get("group_name")
            or "未分组"
        )
        configured_mode = str(resource.get("save_media_mode") or "").lower()
        if configured_mode not in {"movie", "tv", "mixed"}:
            raw_type = str(
                resource.get("detected_media_type")
                or resource.get("media_type")
                or ""
            ).lower()
            configured_mode = (
                "tv"
                if any(item in raw_type for item in ("电视剧", "剧集", "tv", "番剧"))
                else "movie"
                if any(item in raw_type for item in ("电影", "movie", "影片"))
                else "mixed"
            )
        error_message = ""
        if request.method.upper() == "POST":
            form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
            requested_group = str((form.get("group_name") or [group_name])[0]).strip()
            requested_mode = str((form.get("media_mode") or [configured_mode])[0]).lower()
            if (
                not requested_group
                or len(requested_group) > 120
                or requested_group in {".", ".."}
                or "/" in requested_group
                or "\\" in requested_group
            ):
                error_message = "文件夹名称不能为空、不能包含斜杠，且最多 120 个字符。"
            elif requested_mode not in {"movie", "tv", "mixed"}:
                error_message = "请选择电影、剧集或自动识别。"
            else:
                group_name = requested_group
                configured_mode = requested_mode
            if str(resource.get("strm_status") or "") == "ready":
                message = "该资源已经生成 STRM，无需重复保存。"
                success = True
            elif error_message:
                message = error_message
                success = False
            elif not self._store.configure_resource_for_save(
                resource_id,
                group_name,
                configured_mode,
            ):
                message = "资源状态已变化，请刷新页面后重试。"
                success = False
            else:
                response = self._submit(
                    "保存搜索资源到媒体库",
                    self._builder.build,
                    limit=1,
                    retry_failed=True,
                    resource_ids=[resource_id],
                )
                success = bool(response.success)
                message = str(response.message or "已提交")
            color = "#2e7d32" if success else "#c62828"
            return HTMLResponse(
                self._save_page_html(
                    title,
                    group_name,
                    configured_mode,
                    escape(message),
                    color,
                    allow_submit=not success,
                )
            )
        if str(resource.get("strm_status") or "") == "ready":
            return HTMLResponse(
                self._save_page_html(
                    title,
                    group_name,
                    configured_mode,
                    "该资源已经在影视库中。",
                    "#2e7d32",
                    allow_submit=False,
                )
            )
        return HTMLResponse(self._save_page_html(title, group_name, configured_mode))

    @staticmethod
    def _save_page_html(
        title: str,
        group_name: str,
        media_mode: str,
        message: str = "",
        message_color: str = "#1565c0",
        allow_submit: bool = True,
    ) -> str:
        """渲染无需 MoviePilot 前端扩展的轻量保存确认页。"""
        result = (
            f'<p style="color:{message_color};font-weight:600">{message}</p>'
            if message
            else ""
        )
        title = escape(title)
        group_name = escape(group_name, quote=True)
        options = "".join(
            f'<option value="{value}"{(" selected" if media_mode == value else "")}>{label}</option>'
            for value, label in (
                ("movie", "电影"),
                ("tv", "剧集"),
                ("mixed", "自动识别"),
            )
        )
        form = (
            ""
            if not allow_submit
            else (
                '<form method="post"><label>输出文件夹</label>'
                f'<input name="group_name" value="{group_name}" maxlength="120" required>'
                '<small>该文件夹位于输出根目录下，与“星火”同一级。</small>'
                '<label>媒体类型</label>'
                f'<select name="media_mode">{options}</select>'
                '<button type="submit">保存到媒体库</button>'
                "</form>"
            )
        )
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>保存到媒体库</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
background:#10131a;color:#eef2f7;margin:0;padding:24px}}
main{{max-width:560px;margin:10vh auto;background:#1b202b;border-radius:16px;
padding:28px;box-shadow:0 12px 40px #0006}}
p{{color:#b8c0cc;line-height:1.7}}label{{display:block;margin:16px 0 7px}}
input,select{{box-sizing:border-box;width:100%;border:1px solid #465064;
border-radius:9px;background:#111620;color:#eef2f7;padding:11px;font-size:15px}}
small{{display:block;color:#8e99aa;margin-top:6px}}button{{border:0;border-radius:10px;
background:#1976d2;color:white;font-size:16px;padding:12px 22px;cursor:pointer}}
</style></head><body><main><h2>{title}</h2>
{result}{form}
</main></body></html>"""

    async def async_search_torrents(
        self,
        site: dict,
        keyword: str,
        mtype: Optional[MediaType] = None,
        page: Optional[int] = 0,
    ) -> List[TorrentInfo]:
        """兼容最新版 MoviePilot V3 的异步插件检索入口。"""
        return await asyncio.to_thread(
            self.search_torrents,
            site,
            keyword,
            mtype,
            page,
        )

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回远程命令；当前插件不注册聊天命令。"""
        return []

    def discover(self) -> Response:
        """仅发现工作表，不读取资源内容。"""
        if not self._synchronizer:
            return Response(success=False, message="插件尚未初始化")
        try:
            sheets = self._synchronizer.discover_sheets()
            return Response(
                success=True,
                message=f"发现 {len(sheets)} 个工作表；请保存需要同步的分组",
                data={"sheets": sheets},
            )
        except Exception as error:
            return Response(success=False, message=str(error))

    def start_sync(
        self,
        payload: Optional[SyncActionRequest] = Body(default=None),
    ) -> Response:
        """后台执行一个受分页上限保护的同步批次。"""
        if not self._synchronizer:
            return Response(success=False, message="插件尚未初始化")
        action = payload or SyncActionRequest()
        return self._submit(
            "目录同步",
            self._synchronizer.sync,
            reset=action.reset,
            max_pages=action.max_pages,
            mode="manual",
            resume_function=self._synchronizer.sync,
            resume_kwargs={
                "reset": False,
                "max_pages": action.max_pages,
                "mode": "manual",
            },
        )

    def start_build(
        self,
        payload: Optional[BuildActionRequest] = Body(default=None),
    ) -> Response:
        """后台生成一个受资源条数保护的媒体库批次。"""
        if not self._builder:
            return Response(success=False, message="插件尚未初始化")
        action = payload or BuildActionRequest()
        return self._submit(
            "媒体库生成",
            self._builder.build,
            limit=action.limit,
            retry_failed=action.retry_failed,
        )

    def _import_manual_resources(
        self,
        links: str,
        group_name: str,
        media_mode: str,
    ) -> Dict[str, Any]:
        """解析手动资源，并只构建本次新增或变化的资源。"""
        if not self._manual_importer or not self._builder:
            return {"status": "failed", "message": "插件尚未初始化"}
        imported = self._manual_importer.import_links(
            links,
            group_name,
            media_mode,
        )
        queued_ids = list(imported.get("queued_ids") or [])
        if imported.get("paused"):
            return {
                **imported,
                "status": "paused",
                "message": "手动导入已暂停；已保存的资源和文件列表不会丢失",
            }
        if imported.get("interrupted"):
            return {
                **imported,
                "status": "interrupted",
                "message": "手动导入已停止；已保存的资源不会丢失",
            }
        if not queued_ids:
            return {
                **imported,
                "status": "completed",
                "message": (
                    f"手动导入完成：0 条需要生成，{imported['unchanged']} 条未变化，"
                    f"{imported['failed']} 条链接无效或解析失败"
                ),
            }
        built = self._builder.build(
            limit=len(queued_ids),
            resource_ids=queued_ids,
        )
        return {
            **imported,
            **built,
            "message": (
                f"手动导入 {imported['imported']} 条，"
                f"STRM/刮削 {built.get('success', 0)} 成功、"
                f"{built.get('failed', 0)} 失败；"
                f"另有 {imported['unchanged']} 条未变化、"
                f"{imported['failed']} 条链接无效或解析失败"
            ),
        }

    def import_manual_resources(
        self,
        payload: Optional[ManualImportRequest] = Body(default=None),
    ) -> Response:
        """单条或批量添加115分享、磁力或ED2K并生成 STRM。"""
        if not self._manual_importer or not self._builder:
            return Response(success=False, message="插件尚未初始化")
        action = payload or ManualImportRequest()
        links = str(action.links or self._config.get("manual_import_links") or "")
        group_name = str(
            action.group_name or self._config.get("manual_import_group") or ""
        ).strip()
        media_mode = str(
            action.media_mode
            or self._config.get("manual_import_media_mode")
            or "mixed"
        ).strip().lower()
        try:
            if not group_name:
                raise ManualImportError("请填写自定义资源输出文件夹")
            if media_mode not in {"movie", "tv", "mixed"}:
                raise ManualImportError("媒体类型必须是电影、剧集或自动识别")
            if not self._manual_importer.parse_entries(links):
                raise ManualImportError("没有识别到有效的115分享、磁力或ED2K链接")
        except ManualImportError as error:
            return Response(success=False, message=str(error))
        return self._submit(
            "导入自定义资源",
            self._import_manual_resources,
            links,
            group_name,
            media_mode,
        )

    def _set_pipeline_status(self, **values: Any) -> None:
        """线程安全地更新一键同步流水线状态。"""
        with self._pipeline_lock:
            self._pipeline_status.update(values)

    def _pipeline_snapshot(self) -> Dict[str, Any]:
        """返回一键同步流水线状态副本。"""
        with self._pipeline_lock:
            return dict(self._pipeline_status)

    def _sync_all_and_build(self) -> Dict[str, Any]:
        """持续同步全部已选工作表，然后逐批生成所有 pending 资源。"""
        if not self._synchronizer or not self._builder:
            return {"status": "failed", "message": "插件尚未初始化"}
        totals = {
            "synced_pages": 0,
            "synced_rows": 0,
            "built": 0,
            "success": 0,
            "failed": 0,
        }
        self._set_pipeline_status(
            phase="syncing",
            message="正在同步全部已勾选工作表",
            **totals,
        )
        while not self._stop_event.is_set() and not self._pause_event.is_set():
            result = self._synchronizer.sync(mode="manual-all")
            totals["synced_pages"] += int(result.get("processed_pages") or 0)
            totals["synced_rows"] += int(result.get("processed_rows") or 0)
            sync_status = str(result.get("status") or "failed")
            self._set_pipeline_status(
                phase="syncing",
                message=str(result.get("message") or "正在同步"),
                **totals,
            )
            if self._pause_event.is_set():
                self._set_pipeline_status(
                    phase="paused",
                    message="任务已暂停；同步检查点和 pending 队列已保留",
                    **totals,
                )
                return {"status": "paused", **totals}
            if sync_status == "completed":
                break
            if sync_status != "paused":
                final_phase = "stopped" if sync_status == "interrupted" else "failed"
                self._set_pipeline_status(
                    phase=final_phase,
                    message=str(result.get("message") or "目录同步未完成"),
                    **totals,
                )
                return {"status": final_phase, **totals}

        if self._stop_event.is_set():
            self._set_pipeline_status(
                phase="stopped",
                message="任务已停止；检查点和 pending 队列已保留",
                **totals,
            )
            return {"status": "stopped", **totals}
        if self._pause_event.is_set():
            self._set_pipeline_status(
                phase="paused",
                message="任务已暂停；同步检查点和 pending 队列已保留",
                **totals,
            )
            return {"status": "paused", **totals}

        self._set_pipeline_status(
            phase="building",
            message="目录同步完成，正在排队识别、刮削并生成 STRM",
            **totals,
        )
        known_usage_bytes: Optional[int] = None
        while not self._stop_event.is_set() and not self._pause_event.is_set():
            result = self._builder.build(known_usage_bytes=known_usage_bytes)
            processed = int(result.get("processed") or 0)
            known_usage_bytes = int(result.get("usage_bytes") or 0)
            totals["built"] += processed
            totals["success"] += int(result.get("success") or 0)
            totals["failed"] += int(result.get("failed") or 0)
            build_status = str(result.get("status") or "failed")
            self._set_pipeline_status(
                phase="building",
                message=str(result.get("message") or "正在生成媒体库"),
                usage_bytes=int(result.get("usage_bytes") or 0),
                limit_bytes=int(result.get("limit_bytes") or 0),
                **totals,
            )
            if self._pause_event.is_set():
                self._set_pipeline_status(
                    phase="paused",
                    message="任务已暂停；已生成内容和 pending 队列已保留",
                    **totals,
                )
                return {"status": "paused", **totals}
            if build_status == "space_limit":
                self._set_pipeline_status(
                    phase="space_limit",
                    message="已达到输出空间上限；剩余资源仍在 pending 队列",
                    **totals,
                )
                return {"status": "space_limit", **totals}
            if build_status in {"busy", "interrupted"}:
                final_phase = "stopped" if build_status == "interrupted" else "failed"
                self._set_pipeline_status(
                    phase=final_phase,
                    message=str(result.get("message") or "媒体库生成未完成"),
                    **totals,
                )
                return {"status": final_phase, **totals}
            if processed == 0:
                self._set_pipeline_status(
                    phase="completed",
                    message="全部已选工作表同步完成，pending 队列已处理完毕",
                    **totals,
                )
                return {"status": "completed", **totals}

        if self._pause_event.is_set() and not self._stop_event.is_set():
            self._set_pipeline_status(
                phase="paused",
                message="任务已暂停；检查点和 pending 队列已保留",
                **totals,
            )
            return {"status": "paused", **totals}
        self._set_pipeline_status(
            phase="stopped",
            message="任务已停止；检查点和 pending 队列已保留",
            **totals,
        )
        return {"status": "stopped", **totals}

    def start_sync_all(self) -> Response:
        """后台同步全部已选工作表并处理完整生成队列。"""
        if not self._synchronizer or not self._builder:
            return Response(success=False, message="插件尚未初始化")
        return self._submit("同步全部并生成", self._sync_all_and_build)

    def migrate_output(self) -> Response:
        """后台按工作表分目录迁移已有 STRM、元数据和数据库路径。"""
        if not self._builder:
            return Response(success=False, message="插件尚未初始化")
        if not self._config.get("separate_source_folders"):
            return Response(
                success=False,
                message="请先开启“按工作表分开文件夹”并保存配置",
            )
        return self._submit("迁移现有目录", self._builder.migrate_existing_output)

    def pause_background_tasks(self) -> Response:
        """请求当前后台任务在当前页或资源完成后暂停。"""
        with self._task_lock:
            if not self._future or self._future.done():
                if self._task_state == "paused":
                    return Response(success=True, message="后台任务已经暂停")
                return Response(success=False, message="当前没有正在运行的后台任务")
            self._pause_event.set()
            self._task_state = "pausing"
            self._task_message = "正在暂停；当前小步骤完成后会保留断点"
        if self._pipeline_snapshot().get("phase") in {"syncing", "building"}:
            self._set_pipeline_status(
                phase="pausing",
                message="正在暂停；当前小步骤完成后会保留断点",
            )
        return Response(success=True, message="已发送暂停请求，请稍后刷新状态")

    def resume_background_tasks(self) -> Response:
        """恢复上一次暂停的后台任务。"""
        with self._task_lock:
            if self._future and not self._future.done():
                return Response(success=False, message="当前后台任务仍在运行")
            if self._task_state != "paused" or not self._resume_spec:
                return Response(success=False, message="没有可恢复的暂停任务")
            function, args, kwargs = self._resume_spec
            name = self._task_name or "后台任务"
        return self._submit(
            name,
            function,
            *args,
            resume_function=function,
            resume_args=args,
            resume_kwargs=kwargs,
            resume_paused=True,
            **kwargs,
        )

    def stop_background_tasks(self) -> Response:
        """请求当前同步或生成任务在安全检查点停止。"""
        with self._task_lock:
            if not self._future or self._future.done():
                if self._task_state == "paused":
                    self._task_state = "stopped"
                    self._task_message = "任务已结束暂停状态；检查点和已生成内容已保留"
                    self._resume_spec = None
                    return Response(success=True, message="已结束暂停任务")
                return Response(success=True, message="当前没有正在运行的后台任务")
            self._pause_event.clear()
            self._stop_event.set()
            self._task_state = "stopping"
            self._task_message = "正在停止；当前小步骤完成后会保留断点"
        self._set_pipeline_status(
            phase="stopping",
            message="正在安全停止；当前小步骤完成后会保留断点",
        )
        return Response(success=True, message="已发送停止请求，请稍后刷新状态")

    def reset_sync(self) -> Response:
        """放弃所有已选工作表的当前检查点并开启一轮新扫描。"""
        if not self._synchronizer:
            return Response(success=False, message="插件尚未初始化")
        return self._submit(
            "重新扫描目录",
            self._synchronizer.sync,
            reset=True,
            max_pages=None,
            mode="manual-reset",
            resume_function=self._synchronizer.sync,
            resume_kwargs={
                "reset": False,
                "max_pages": None,
                "mode": "manual",
            },
        )

    def retry_resources(self, payload: ResourceRetryRequest = Body(...)) -> Response:
        """将用户选定的失败资源重新加入生成队列。"""
        if not self._store:
            return Response(success=False, message="插件尚未初始化")
        count = self._store.retry_resources(payload.resource_ids)
        return Response(success=True, message=f"已重新排队 {count} 条资源")

    def _retry_all_failed_and_build(self) -> Dict[str, Any]:
        """锁定当前全部失败资源，并连续按批次各重试一次。"""
        if not self._store or not self._builder:
            return {"status": "failed", "message": "插件尚未初始化"}
        resource_ids = self._store.list_retryable_resource_ids()
        requeued = len(resource_ids)
        totals = {
            "synced_pages": 0,
            "synced_rows": 0,
            "built": 0,
            "success": 0,
            "failed": 0,
        }
        if not requeued:
            self._set_pipeline_status(
                phase="completed",
                message="当前没有失败资源需要重试",
                **totals,
            )
            return {"status": "completed", "requeued": 0, **totals}
        self._set_pipeline_status(
            phase="building",
            message=f"已锁定 {requeued} 条失败资源，正在逐条重试一次",
            **totals,
        )
        known_usage_bytes: Optional[int] = None
        remaining_ids = list(resource_ids)
        while not self._stop_event.is_set() and not self._pause_event.is_set():
            try:
                batch_limit = min(
                    max(int(self._config.get("build_batch") or 20), 1),
                    500,
                )
            except (TypeError, ValueError):
                batch_limit = 20
            candidates = self._store.list_build_candidates(
                batch_limit,
                retry_failed=True,
                resource_ids=remaining_ids,
            )
            batch_ids = [str(item["resource_id"]) for item in candidates]
            if not batch_ids:
                self._set_pipeline_status(
                    phase="completed",
                    message=(
                        f"失败资源已完成一轮重试：{totals['success']} 成功 / "
                        f"{totals['failed']} 再次失败"
                    ),
                    **totals,
                )
                return {"status": "completed", "requeued": requeued, **totals}
            result = self._builder.build(
                limit=len(batch_ids),
                known_usage_bytes=known_usage_bytes,
                retry_failed=True,
                resource_ids=batch_ids,
            )
            processed = int(result.get("processed") or 0)
            processed_ids = set(batch_ids[:processed])
            remaining_ids = [
                resource_id
                for resource_id in remaining_ids
                if resource_id not in processed_ids
            ]
            known_usage_bytes = int(result.get("usage_bytes") or 0)
            totals["built"] += processed
            totals["success"] += int(result.get("success") or 0)
            totals["failed"] += int(result.get("failed") or 0)
            build_status = str(result.get("status") or "failed")
            self._set_pipeline_status(
                phase="building",
                message=(
                    f"失败资源重试中：{totals['success']} 成功 / "
                    f"{totals['failed']} 再次失败"
                ),
                usage_bytes=int(result.get("usage_bytes") or 0),
                limit_bytes=int(result.get("limit_bytes") or 0),
                **totals,
            )
            if self._pause_event.is_set():
                self._set_pipeline_status(
                    phase="paused",
                    message="失败资源重试已暂停；未处理项保持原状态",
                    **totals,
                )
                return {"status": "paused", "requeued": requeued, **totals}
            if build_status == "space_limit":
                self._set_pipeline_status(
                    phase="space_limit",
                    message="重试已因输出空间上限停止；未处理项保持原状态",
                    **totals,
                )
                return {"status": "space_limit", "requeued": requeued, **totals}
            if build_status in {"busy", "interrupted"}:
                final_phase = "stopped" if build_status == "interrupted" else "failed"
                self._set_pipeline_status(
                    phase=final_phase,
                    message=str(result.get("message") or "失败资源重试未完成"),
                    **totals,
                )
                return {"status": final_phase, "requeued": requeued, **totals}
            if processed == 0:
                self._set_pipeline_status(
                    phase="failed",
                    message="失败资源重试未取得进展，请查看最近错误",
                    **totals,
                )
                return {"status": "failed", "requeued": requeued, **totals}
        if self._pause_event.is_set() and not self._stop_event.is_set():
            self._set_pipeline_status(
                phase="paused",
                message="失败资源重试已暂停；未处理项保持原状态",
                **totals,
            )
            return {"status": "paused", "requeued": requeued, **totals}
        self._set_pipeline_status(
            phase="stopped",
            message="失败资源重试已停止；未处理资源仍为 pending",
            **totals,
        )
        return {"status": "stopped", "requeued": requeued, **totals}

    def retry_all_failed(self) -> Response:
        """后台重试全部失败资源，并持续处理到本轮队列结束。"""
        if not self._store or not self._builder:
            return Response(success=False, message="插件尚未初始化")
        return self._submit("重试全部失败资源", self._retry_all_failed_and_build)

    def status(self) -> Response:
        """返回同步检查点、资源计数和最近错误。"""
        if not self._store:
            return Response(success=False, message="插件尚未初始化")
        snapshot = self._store.status_snapshot()
        task = self._task_snapshot()
        snapshot["task_running"] = bool(task.get("running"))
        snapshot["task"] = task
        snapshot["pipeline"] = self._pipeline_snapshot()
        snapshot["storage"] = self._builder.storage_snapshot() if self._builder else {}
        snapshot["offline_playback"] = self._store.offline_playback_snapshot()
        snapshot["direct_gateway"] = (
            self._gateway.status() if self._gateway else {"state": "disabled"}
        )
        return Response(success=True, data=snapshot)

    def restart_gateway(self) -> Response:
        """按当前配置重启内置 Emby 直链网关。"""
        if not self._gateway:
            return Response(success=False, message="直链网关尚未初始化")
        if not self._config.get("direct_gateway_enabled"):
            return Response(success=False, message="请先在插件配置中启用直链网关")
        self._gateway.restart_background()
        return Response(success=True, message="直链网关正在后台重启")

    def clear_all_data(self) -> Response:
        """清空插件数据库以及输出目录中的 STRM 和元数据文件"""
        if self._future and not self._future.done():
            return Response(success=False, message="请先停止并等待后台任务结束")
        if not self._config.get("clear_confirmation"):
            return Response(
                success=False,
                message=(
                    "请先在插件配置中开启“我确认清空全部插件数据和生成元数据”"
                    "并保存"
                ),
            )
        if not self._store or not self._builder:
            return Response(success=False, message="插件尚未初始化")
        try:
            offline_cleanup = (
                self._resolver.cleanup_offline_cache(include_all=True)
                if self._resolver
                else {"failed": 0, "removed": 0}
            )
            if offline_cleanup.get("failed"):
                return Response(
                    success=False,
                    message="115离线缓存清理失败，已保留数据库记录供稍后重试",
                    data=offline_cleanup,
                )
            cleanup = self._builder.clear_generated_output()
            self._store.clear_all()
            if self._gateway:
                self._gateway.clear_cache()
            if self._resolver:
                self._resolver.clear_url_cache()
            updated_config = dict(self._config)
            for key in list(updated_config):
                if key.startswith("sheet_") and key.endswith(
                    ("_enabled", "_group", "_media_mode")
                ):
                    updated_config.pop(key, None)
            updated_config["clear_confirmation"] = False
            self._replace_config(updated_config)
            self._set_pipeline_status(
                phase="idle",
                message="数据已清空，请重新发现工作表并开始同步",
                synced_pages=0,
                synced_rows=0,
                built=0,
                success=0,
                failed=0,
            )
            retained_files = int(cleanup.get("retained_files") or 0)
            message = (
                f"已清空数据库并删除 {int(cleanup.get('deleted_files') or 0)} 个 "
                "STRM/元数据文件"
            )
            if retained_files:
                message += f"；为安全起见保留了 {retained_files} 个其它文件"
            return Response(success=True, message=message, data=cleanup)
        except Exception as error:
            logger.error(f"清空腾讯文档115媒体库失败：{error}", exc_info=True)
            return Response(success=False, message=str(error))

    def play(
        self,
        resource_id: str,
        request: Request,
        filename: Optional[str] = None,
        token: str = Query(default=""),
        file_id: Optional[str] = Query(default=None),
    ) -> HttpResponse:
        """校验 STRM 密钥，按需解析 115 临时地址并返回 302。"""
        expected_token = str(self._config.get("playback_token") or "")
        if not expected_token or not compare_digest(token, expected_token):
            return JSONResponse(
                status_code=401,
                content={"success": False, "message": "播放密钥无效"},
            )
        if not self._resolver:
            return JSONResponse(
                status_code=503,
                content={"success": False, "message": "插件尚未初始化"},
            )
        try:
            direct_url = self._resolver.resolve(
                resource_id=resource_id,
                file_id=file_id,
                user_agent=request.headers.get("user-agent", ""),
            )
            return RedirectResponse(
                url=direct_url,
                status_code=302,
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )
        except ShareResolutionError as error:
            return JSONResponse(
                status_code=error.status_code,
                content={"success": False, "message": str(error)},
            )

    def get_api(self) -> List[Dict[str, Any]]:
        """注册管理接口和带播放密钥的重定向接口。"""
        return [
            {
                "path": "/discover",
                "endpoint": self.discover,
                "methods": ["POST"],
                "summary": "发现腾讯文档工作表",
                "auth": "bear",
            },
            {
                "path": "/sync",
                "endpoint": self.start_sync,
                "methods": ["POST"],
                "summary": "分页同步目录",
                "auth": "bear",
            },
            {
                "path": "/build",
                "endpoint": self.start_build,
                "methods": ["POST"],
                "summary": "限量生成媒体库",
                "auth": "bear",
            },
            {
                "path": "/resources/import-manual",
                "endpoint": self.import_manual_resources,
                "methods": ["POST"],
                "summary": "导入115分享、磁力或ED2K并生成媒体库",
                "auth": "bear",
            },
            {
                "path": "/resources/save/{resource_id}",
                "endpoint": self.save_search_resource,
                "methods": ["GET", "POST"],
                "summary": "确认并保存原生搜索结果到 STRM 媒体库",
                "allow_anonymous": True,
            },
            {
                "path": "/sync-all",
                "endpoint": self.start_sync_all,
                "methods": ["POST"],
                "summary": "同步全部工作表并生成媒体库",
                "auth": "bear",
            },
            {
                "path": "/tasks/stop",
                "endpoint": self.stop_background_tasks,
                "methods": ["POST"],
                "summary": "安全停止后台同步或生成",
                "auth": "bear",
            },
            {
                "path": "/tasks/pause",
                "endpoint": self.pause_background_tasks,
                "methods": ["POST"],
                "summary": "暂停后台同步、生成或迁移",
                "auth": "bear",
            },
            {
                "path": "/tasks/resume",
                "endpoint": self.resume_background_tasks,
                "methods": ["POST"],
                "summary": "恢复已暂停的后台任务",
                "auth": "bear",
            },
            {
                "path": "/sync/reset",
                "endpoint": self.reset_sync,
                "methods": ["POST"],
                "summary": "重置检查点后限量同步",
                "auth": "bear",
            },
            {
                "path": "/resources/retry",
                "endpoint": self.retry_resources,
                "methods": ["POST"],
                "summary": "重试失败资源",
                "auth": "bear",
            },
            {
                "path": "/resources/retry-all",
                "endpoint": self.retry_all_failed,
                "methods": ["POST"],
                "summary": "重试全部失败资源并生成",
                "auth": "bear",
            },
            {
                "path": "/status",
                "endpoint": self.status,
                "methods": ["GET"],
                "summary": "查看插件状态",
                "auth": "bear",
            },
            {
                "path": "/gateway/restart",
                "endpoint": self.restart_gateway,
                "methods": ["POST"],
                "summary": "重启内置 Emby 直链网关",
                "auth": "bear",
            },
            {
                "path": "/clear-all",
                "endpoint": self.clear_all_data,
                "methods": ["POST"],
                "summary": "清空插件数据库和生成内容",
                "auth": "bear",
            },
            {
                "path": "/migrate-output",
                "endpoint": self.migrate_output,
                "methods": ["POST"],
                "summary": "按工作表分目录迁移已有输出",
                "auth": "bear",
            },
            {
                "path": "/play/{resource_id}/{filename}",
                "endpoint": self.play,
                "methods": ["GET", "HEAD"],
                "summary": "按真实文件名识别格式并解析 115 分享",
                "allow_anonymous": True,
            },
            {
                "path": "/play/{resource_id}",
                "endpoint": self.play,
                "methods": ["GET", "HEAD"],
                "summary": "校验密钥后按需解析 115 分享",
                "allow_anonymous": True,
            },
        ]

    def _automatic_sync(self) -> None:
        """提交一次有界自动同步批次。"""
        if not self._enabled or not self._synchronizer:
            return
        self._submit("自动目录同步", self._synchronizer.sync, mode="automatic")

    def _automatic_build(self) -> None:
        """提交一次有界自动媒体库生成批次。"""
        if not self._enabled or not self._builder:
            return
        self._submit("自动媒体库生成", self._builder.build)

    def _automatic_offline_cleanup(self) -> None:
        """清理到期的磁力和ED2K离线播放文件。"""
        if not self._enabled or not self._resolver:
            return
        result = self._resolver.cleanup_offline_cache()
        if result.get("checked"):
            logger.info(f"115离线缓存定时清理完成：{result}")

    def get_service(self) -> List[Dict[str, Any]]:
        """按配置注册自动同步任务。"""
        if not self._enabled:
            return []
        services: List[Dict[str, Any]] = []
        service_specs = [
            (
                "auto_sync",
                "sync_cron",
                "TencentDoc115LibrarySync",
                "腾讯文档115媒体库分页同步",
                self._automatic_sync,
            ),
            (
                "auto_build",
                "build_cron",
                "TencentDoc115LibraryBuild",
                "腾讯文档115媒体库限量生成",
                self._automatic_build,
            ),
        ]
        for enabled_key, cron_key, service_id, name, function in service_specs:
            if not self._config.get(enabled_key):
                continue
            cron = str(self._config.get(cron_key) or "").strip()
            if not cron:
                continue
            try:
                trigger = CronTrigger.from_crontab(cron)
            except ValueError as error:
                logger.error(f"{name}定时表达式错误：{error}")
                continue
            services.append(
                {
                    "id": service_id,
                    "name": name,
                    "trigger": trigger,
                    "func": function,
                    "kwargs": {},
                }
            )
        if self._config.get("offline_playback_enabled", True):
            cron = str(self._config.get("offline_cleanup_cron") or "").strip()
            if cron:
                try:
                    trigger = CronTrigger.from_crontab(cron)
                    services.append(
                        {
                            "id": "TencentDoc115LibraryOfflineCleanup",
                            "name": "腾讯文档115媒体库离线缓存清理",
                            "trigger": trigger,
                            "func": self._automatic_offline_cleanup,
                            "kwargs": {},
                        }
                    )
                except ValueError as error:
                    logger.error(f"115离线缓存清理定时表达式错误：{error}")
        return services

    @staticmethod
    def _text_field(
        model: str,
        label: str,
        cols: int = 6,
        secret: bool = False,
        hint: str = "",
    ) -> Dict[str, Any]:
        """生成一个配置文本框。"""
        props: Dict[str, Any] = {
            "model": model,
            "label": label,
            "persistent-hint": bool(hint),
            "hint": hint,
        }
        if secret:
            props["type"] = "password"
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": cols},
            "content": [{"component": "VTextField", "props": props}],
        }

    @staticmethod
    def _textarea_field(
        model: str,
        label: str,
        hint: str = "",
    ) -> Dict[str, Any]:
        """生成一个适合多行文档链接的配置框。"""
        return {
            "component": "VCol",
            "props": {"cols": 12},
            "content": [
                {
                    "component": "VTextarea",
                    "props": {
                        "model": model,
                        "label": label,
                        "rows": 4,
                        "auto-grow": True,
                        "persistent-hint": bool(hint),
                        "hint": hint,
                    },
                }
            ],
        }

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回凭据、同步保护、115 和工作表分组配置。"""
        sheets = (
            [
                sheet
                for sheet in self._store.list_sheets()
                if not str(sheet.get("sheet_id") or "").startswith(
                    MANUAL_SHEET_PREFIX
                )
            ]
            if self._store
            else []
        )
        sheet_rows: List[Dict[str, Any]] = []
        for sheet in sheets:
            key = sheet_config_key(sheet["sheet_id"])
            sheet_rows.append(
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": f"sheet_{key}_enabled",
                                        "label": str(sheet["title"]),
                                    },
                                }
                            ],
                        },
                        self._text_field(
                            f"sheet_{key}_group",
                            "输出分组（相同名称即合并）",
                            cols=5,
                        ),
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": "VSelect",
                                    "props": {
                                        "model": f"sheet_{key}_media_mode",
                                        "label": "媒体类型",
                                        "items": [
                                            {"title": "电影", "value": "movie"},
                                            {"title": "剧集", "value": "tv"},
                                            {
                                                "title": "混合（按类型列分流）",
                                                "value": "mixed",
                                            },
                                        ],
                                        "item-title": "title",
                                        "item-value": "value",
                                        "persistent-hint": True,
                                        "hint": (
                                            "混合模式中，类型列含“剧集”则输出到"
                                            "基础分组-剧集，其余按电影处理"
                                        ),
                                    },
                                }
                            ],
                        },
                    ],
                }
            )
        if not sheet_rows:
            sheet_rows = [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "先保存腾讯文档凭据，再到插件详情页点击“发现工作表”。发现操作不会读取资源行。",
                    },
                }
            ]
        form = [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "auto_sync",
                                            "label": "自动分页同步",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "auto_build",
                                            "label": "定时自动生成",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "scrape_metadata",
                                            "label": "MoviePilot刮削",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "native_search_enabled",
                                            "label": "接入原生检索/下载",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "native_search_scope",
                                            "label": "MoviePilot检索范围",
                                            "items": [
                                                {
                                                    "title": "全部有效资源",
                                                    "value": "all",
                                                },
                                                {
                                                    "title": "仅未入库资源",
                                                    "value": "unbuilt",
                                                },
                                                {
                                                    "title": "仅已生成STRM",
                                                    "value": "ready",
                                                },
                                            ],
                                            "item-title": "title",
                                            "item-value": "value",
                                            "persistent-hint": True,
                                            "hint": "默认全部显示；已生成资源会标注“已入库”",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._textarea_field(
                                "document_urls",
                                "腾讯文档表格（每行一条）",
                                hint="支持“文档别名|链接”；只填链接时自动命名为文档1、文档2",
                            ),
                            self._text_field("client_id", "Client ID", 6),
                            self._text_field("openid", "Open ID", 6),
                            self._text_field(
                                "client_secret",
                                "Client Secret（可选，仅 OAuth 应用）",
                                6,
                                True,
                                hint="第三方应用审核通过后获得；仅手动 Access Token 测试可留空",
                            ),
                            self._text_field("access_token", "Access Token", 6, True),
                            self._text_field(
                                "refresh_token",
                                "Refresh Token（可选）",
                                6,
                                True,
                                hint="OAuth 授权码换取 Token 时由接口返回，控制台不会单独展示",
                            ),
                            self._text_field("sync_cron", "自动同步 Cron", 6),
                            self._text_field("build_cron", "自动生成 Cron", 6),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._text_field("output_root", "STRM/元数据输出目录", 6),
                            self._text_field(
                                "output_size_limit_gb",
                                "输出目录空间上限（GiB）",
                                6,
                                hint="统计 STRM、NFO 和图片；0 表示不限。达到上限后保留 pending 队列。",
                            ),
                            self._text_field(
                                "public_base_url",
                                "Emby 可访问的 MoviePilot 地址",
                                6,
                                hint="Unraid 部署后不能使用 127.0.0.1，请填写局域网地址",
                            ),
                            self._text_field("page_rows", "每页行数（最高1000）", 3),
                            self._text_field("pages_per_run", "每次最多页数", 3),
                            self._text_field("build_batch", "每次最多生成资源", 3),
                            self._text_field(
                                "scrape_workers",
                                "并行刮削线程数",
                                3,
                                hint="默认1；建议2-4，过高可能触发TMDB限流或增加内存占用",
                            ),
                            self._text_field("share_max_files", "单分享最多视频数", 3),
                            self._text_field("share_page_size", "115每页文件数", 3),
                            self._text_field("share_max_depth", "115最大目录深度", 3),
                            self._text_field(
                                "request_interval", "115请求间隔（秒）", 3
                            ),
                            self._text_field("request_retries", "临时错误重试次数", 3),
                            self._text_field(
                                "direct_url_cache_ttl", "直链缓存时长（秒）", 3
                            ),
                            self._text_field("search_page_size", "本地检索每页条数", 3),
                            self._text_field("download_retries", "直链下载重试次数", 3),
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "separate_source_folders",
                                            "label": "按工作表分开文件夹",
                                            "hint": "开启后同一媒体在不同工作表使用独立子目录；元数据仍可复用",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "reuse_p115_cookie",
                                            "label": "复用115网盘STRM助手 Cookie",
                                        },
                                    }
                                ],
                            },
                            self._text_field(
                                "p115_cookie", "115 Cookie（复用失败时使用）", 8, True
                            ),
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "offline_playback_enabled",
                                            "label": "允许磁力/ED2K按需离线",
                                        },
                                    }
                                ],
                            },
                            self._text_field(
                                "offline_temp_path",
                                "115离线临时目录",
                                4,
                                hint="必须是 /temp 下的独立子目录；插件只会清理这里由自己创建的资源目录",
                            ),
                            self._text_field(
                                "offline_wait_seconds",
                                "首次播放等待（秒）",
                                4,
                                hint="超时只会提示稍后重试，115离线任务仍会继续",
                            ),
                            self._text_field(
                                "offline_retention_hours",
                                "离线文件保留（小时）",
                                4,
                                hint="按最后一次播放续期；为避免中途清理，实际最短为24小时",
                            ),
                        ],
                    },
                    {
                        "component": "VDivider",
                        "props": {"class": "my-4"},
                    },
                    {
                        "component": "div",
                        "props": {"class": "text-h6 mb-2"},
                        "text": "自定义资源链接",
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "class": "mb-3",
                            "text": (
                                "填写后先保存设置，再到详情页点击“导入自定义资源”。"
                                "115分享会匿名解析；磁力/ED2K只先识别和生成STRM，首次播放才提交115离线任务；"
                                "同一批链接使用下方同一个文件夹和媒体类型。"
                            ),
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._textarea_field(
                                "manual_import_links",
                                "115分享/磁力/ED2K（每行一条，最多100条）",
                                "支持：链接；标题|链接；标题|年份|链接。115访问码可放在链接参数中。",
                            ),
                            self._text_field(
                                "manual_import_group",
                                "输出文件夹",
                                6,
                                hint="例如：自定义、朋友分享；作为输出根目录下的一级分组",
                            ),
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "manual_import_media_mode",
                                            "label": "媒体类型",
                                            "items": [
                                                {"title": "电影", "value": "movie"},
                                                {"title": "剧集", "value": "tv"},
                                                {
                                                    "title": "自动识别并分流",
                                                    "value": "mixed",
                                                },
                                            ],
                                            "item-title": "title",
                                            "item-value": "value",
                                            "persistent-hint": True,
                                            "hint": "自动模式会按文件特征和 MoviePilot 识别结果分到电影或“文件夹-剧集”",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VDivider",
                        "props": {"class": "my-4"},
                    },
                    {
                        "component": "div",
                        "props": {"class": "text-h6 mb-2"},
                        "text": "内置 Emby 直链网关",
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "class": "mb-3",
                            "text": (
                                "客户端必须连接网关端口而不是 Emby 8096；"
                                "网关只为本插件 STRM 返回115直链，"
                                "其余 Emby 请求原样转发。MoviePilot Docker 还需要映射网关端口。"
                            ),
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "direct_gateway_enabled",
                                            "label": "启用直链网关",
                                        },
                                    }
                                ],
                            },
                            self._text_field(
                                "direct_gateway_port",
                                "网关监听端口",
                                4,
                                hint="默认8097；Docker需要映射相同端口",
                            ),
                            self._text_field(
                                "emby_internal_url",
                                "Emby 内部地址",
                                6,
                                hint="MoviePilot 容器可访问的地址，如 http://192.168.5.192:8096",
                            ),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._text_field(
                                "emby_api_key",
                                "Emby API Key",
                                12,
                                True,
                                "用于查询 STRM 路径和按需触发 ISO 媒体探测",
                            ),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._textarea_field(
                                "emby_strm_paths",
                                "Emby STRM 媒体库路径（一行一个）",
                                "必须填写 Emby 容器内看到的路径，如 /data/tencentdoc115",
                            ),
                            self._textarea_field(
                                "emby_path_mappings",
                                "Emby → MoviePilot 路径映射（可选）",
                                "两边路径不同时填写：/Emby路径|/MoviePilot路径，每行一条",
                            ),
                        ],
                    },
                    {
                        "component": "VDivider",
                        "props": {"class": "my-4"},
                    },
                    {
                        "component": "div",
                        "props": {"class": "text-h6 mb-2"},
                        "text": "工作表与输出分组",
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "class": "mb-3",
                            "text": (
                                "每个工作表必须指定电影、剧集或混合类型。"
                                "混合类型逐行读取“类型”列：含剧集关键词的行输出到"
                                "“基础分组-剧集”，其它行输出到基础分组。"
                            ),
                        },
                    },
                    *sheet_rows,
                    {
                        "component": "VDivider",
                        "props": {"class": "my-4"},
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "error",
                            "variant": "tonal",
                            "class": "mb-3",
                            "text": (
                                "危险操作：开启确认开关并保存后，详情页的清空按钮会"
                                "清空 SQLite，并删除输出目录内的 STRM、NFO 和图片。"
                                "其它类型文件会保留。"
                            ),
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "clear_confirmation",
                                            "label": "我确认清空全部插件数据和生成元数据",
                                            "color": "error",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ]
        defaults = dict(DEFAULT_CONFIG)
        defaults.update(self._config)
        if (
            not str(defaults.get("document_urls") or "").strip()
            and str(defaults.get("document_url") or "").strip()
        ):
            defaults["document_urls"] = (
                f"主文档|{str(defaults['document_url']).strip()}"
            )
        for sheet in sheets:
            key = sheet_config_key(sheet["sheet_id"])
            defaults.setdefault(f"sheet_{key}_enabled", False)
            defaults.setdefault(
                f"sheet_{key}_group",
                default_group_for_title(sheet["title"]),
            )
            defaults.setdefault(
                f"sheet_{key}_media_mode",
                default_media_mode_for_title(sheet["title"]),
            )
        return form, defaults

    def get_page(self) -> List[dict]:
        """返回手动操作、断点状态和最近错误页面。"""
        snapshot = (
            self._store.status_snapshot()
            if self._store
            else {
                "total_resources": 0,
                "active_resources": 0,
                "resource_counts": {},
                "strm_counts": {},
                "scrape_counts": {},
                "current_resources": [],
                "sheets": [],
                "recent_errors": [],
            }
        )
        storage = (
            self._builder.storage_snapshot()
            if self._builder
            else {
                "usage_bytes": 0,
                "limit_bytes": 0,
                "limit_reached": False,
            }
        )
        pipeline = self._pipeline_snapshot()
        task = self._task_snapshot()
        gateway = (
            self._gateway.status()
            if self._gateway
            else {"enabled": False, "state": "disabled", "port": 8097}
        )
        offline_playback = (
            self._store.offline_playback_snapshot()
            if self._store
            else {"total": 0, "states": {}, "total_size": 0}
        )
        buttons = [
            ("发现工作表", "mdi-table-search", "discover", "primary"),
            (
                "导入自定义资源",
                "mdi-link-plus",
                "resources/import-manual",
                "info",
            ),
            ("继续同步", "mdi-sync", "sync", "primary"),
            ("同步全部并生成", "mdi-playlist-check", "sync-all", "success"),
            ("重新扫描", "mdi-restart-alert", "sync/reset", "warning"),
            ("生成下一批", "mdi-movie-open-plus", "build", "success"),
            (
                "重试全部失败并生成",
                "mdi-refresh-circle",
                "resources/retry-all",
                "warning",
            ),
            ("暂停后台任务", "mdi-pause-circle", "tasks/pause", "warning"),
            ("恢复后台任务", "mdi-play-circle", "tasks/resume", "success"),
            ("停止后台任务", "mdi-stop-circle", "tasks/stop", "error"),
            ("迁移现有目录", "mdi-folder-move", "migrate-output", "info"),
            (
                "重启直链网关",
                "mdi-lan-connect",
                "gateway/restart",
                "info",
            ),
            (
                "清空并重新开始",
                "mdi-delete-alert",
                "clear-all",
                "error",
            ),
        ]
        task_state = str(task.get("state") or "idle")
        button_components = [
            {
                "component": "VBtn",
                "props": {
                    "color": color,
                    "variant": "tonal",
                    "prepend-icon": icon,
                    "class": "ma-1",
                },
                "text": label,
                "events": {
                    "click": {
                        "api": f"plugin/TencentDoc115Library/{endpoint}",
                        "method": "post",
                    }
                },
            }
            for label, icon, endpoint, color in buttons
        ]
        sheet_items: List[Dict[str, Any]] = []
        media_mode_labels = {
            "movie": "电影",
            "tv": "剧集",
            "mixed": "混合",
        }
        for sheet in snapshot.get("sheets", []):
            if str(sheet.get("sheet_id") or "").startswith(MANUAL_SHEET_PREFIX):
                continue
            total = int(sheet.get("used_row_count") or sheet.get("row_count") or 0)
            checkpoint = max(int(sheet.get("checkpoint_row") or 1) - 1, 0)
            sheet_items.append(
                {
                    "component": "VListItem",
                    "props": {
                        "title": str(sheet.get("title") or ""),
                        "subtitle": (
                            f"分组：{sheet.get('group_name') or '未设置'} · "
                            f"类型：{media_mode_labels.get(str(sheet.get('media_mode')), '电影')} · "
                            f"状态：{sheet.get('scan_status')} · "
                            f"检查点：{checkpoint}/{total or '?'}"
                        ),
                    },
                }
            )
        status_labels = {
            "pending": "等待处理",
            "processing": "处理中",
            "ready": "全部完成",
            "share_error": "分享错误",
            "invalid_share": "无效分享（不再重试）",
            "metadata_error": "刮削错误",
            "build_error": "生成错误",
            "removed": "已移除",
        }
        strm_labels = {
            "pending": "等待校验",
            "validating": "正在校验分享",
            "validated": "分享校验完成",
            "generating": "正在生成 STRM",
            "ready": "STRM 已生成",
            "failed": "STRM 失败",
        }
        scrape_labels = {
            "pending": "等待识别",
            "recognizing": "正在识别媒体",
            "recognized": "媒体识别完成",
            "scraping": "正在刮削元数据",
            "ready": "刮削完成",
            "skipped": "已关闭刮削",
            "unrecognized": "未识别（仅 STRM）",
            "failed": "刮削失败",
            "blocked": "前序失败，未刮削",
        }
        counts = snapshot.get("resource_counts", {})
        count_text = (
            " · ".join(
                f"{status_labels.get(status, status)}: {count}"
                for status, count in sorted(counts.items())
            )
            or "尚无资源"
        )
        active_resources = int(snapshot.get("active_resources") or 0)
        strm_counts = snapshot.get("strm_counts", {})
        scrape_counts = snapshot.get("scrape_counts", {})
        strm_ready = int(strm_counts.get("ready") or 0)
        strm_failed = int(strm_counts.get("failed") or 0)
        strm_waiting = max(active_resources - strm_ready - strm_failed, 0)
        scrape_ready = int(scrape_counts.get("ready") or 0) + int(
            scrape_counts.get("skipped") or 0
        )
        scrape_unrecognized = int(scrape_counts.get("unrecognized") or 0)
        scrape_ready += scrape_unrecognized
        scrape_failed = int(scrape_counts.get("failed") or 0)
        scrape_blocked = int(scrape_counts.get("blocked") or 0)
        scrape_waiting = max(
            active_resources - scrape_ready - scrape_failed - scrape_blocked,
            0,
        )
        strm_percent = (
            round(strm_ready * 100 / active_resources) if active_resources else 0
        )
        scrape_percent = (
            round(scrape_ready * 100 / active_resources) if active_resources else 0
        )
        limit_bytes = int(storage.get("limit_bytes") or 0)
        usage_text = format_gib(int(storage.get("usage_bytes") or 0))
        limit_text = format_gib(limit_bytes) if limit_bytes else "不限"
        phase_labels = {
            "idle": "空闲",
            "syncing": "正在同步",
            "building": "正在刮削和生成",
            "pausing": "正在暂停",
            "paused": "已暂停",
            "stopping": "正在停止",
            "stopped": "已停止",
            "completed": "已完成",
            "space_limit": "空间已满",
            "failed": "失败",
        }
        phase = str(pipeline.get("phase") or "idle")
        pipeline_text = (
            f"一键任务：{phase_labels.get(phase, phase)} · "
            f"同步 {int(pipeline.get('synced_pages') or 0)} 页 / "
            f"{int(pipeline.get('synced_rows') or 0)} 行 · "
            f"生成 {int(pipeline.get('success') or 0)} 成功 / "
            f"{int(pipeline.get('failed') or 0)} 失败。"
            f"{pipeline.get('message') or ''}"
        )
        task_labels = {
            "idle": "空闲",
            "running": "运行中",
            "pausing": "正在暂停",
            "paused": "已暂停",
            "stopping": "正在停止",
            "stopped": "已停止",
            "completed": "已完成",
            "failed": "失败",
        }
        task_text = (
            f"后台任务：{task_labels.get(task_state, task_state)}"
            + (f" · {task.get('name')}" if task.get("name") else "")
            + (f" · {task.get('message')}" if task.get("message") else "")
        )
        gateway_labels = {
            "disabled": "未启用",
            "starting": "正在启动",
            "running": "运行中",
            "stopped": "已停止",
            "error": "启动失败",
        }
        gateway_state = str(gateway.get("state") or "disabled")
        gateway_text = (
            f"内置直链网关：{gateway_labels.get(gateway_state, gateway_state)} · "
            f"端口：{int(gateway.get('port') or 8097)}"
        )
        if gateway.get("last_error"):
            gateway_text += f" · 错误：{gateway.get('last_error')}"
        offline_states = offline_playback.get("states") or {}
        gateway_text += (
            f" · 115离线：{int(offline_playback.get('total') or 0)} 条"
            f"（完成 {int(offline_states.get('ready') or 0)} / "
            f"进行中 {int(offline_states.get('downloading') or 0)} / "
            f"失败 {int(offline_states.get('failed') or 0)}）"
        )
        error_items: List[Dict[str, Any]] = []
        for item in snapshot.get("recent_errors", [])[:10]:
            error_items.append(
                {
                    "component": "VListItem",
                    "props": {
                        "title": str(item.get("title") or item.get("resource_id")),
                        "subtitle": (
                            f"STRM：{strm_labels.get(item.get('strm_status'), item.get('strm_status'))} · "
                            f"刮削：{scrape_labels.get(item.get('scrape_status'), item.get('scrape_status'))} · "
                            f"{item.get('last_error')}"
                        ),
                    },
                }
            )
        current_items: List[Dict[str, Any]] = []
        for item in snapshot.get("current_resources", []):
            current_items.append(
                {
                    "component": "VListItem",
                    "props": {
                        "title": str(item.get("title") or item.get("resource_id")),
                        "subtitle": (
                            f"{item.get('group_name') or '未分组'} · "
                            f"STRM：{strm_labels.get(item.get('strm_status'), item.get('strm_status'))} · "
                            f"刮削：{scrape_labels.get(item.get('scrape_status'), item.get('scrape_status'))}"
                        ),
                    },
                }
            )
        return [
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-4"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "text": f"目录资源：{snapshot.get('total_resources', 0)}",
                    },
                    {"component": "VCardSubtitle", "text": count_text},
                    {
                        "component": "VCardText",
                        "props": {"class": "d-flex flex-wrap"},
                        "content": button_components,
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": (
                                f"{pipeline_text} {task_text} 输出目录占用：{usage_text} / {limit_text}。"
                                "“同步全部并生成”会在后台跑完已勾选工作表，再逐批处理 pending；"
                                "可暂停或停止，恢复会从断点继续。重新打开本页可刷新状态。"
                            ),
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "success" if gateway_state == "running" else "warning",
                            "variant": "tonal",
                            "class": "mt-2",
                            "text": (
                                f"{gateway_text}。客户端需要连接此端口，不能继续连接 Emby 8096；"
                                "直链播放时视频数据由客户端直接从115获取。"
                            ),
                        },
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-4"},
                "content": [
                    {"component": "VCardTitle", "text": "STRM 与刮削进度"},
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "div",
                                "props": {"class": "mb-1"},
                                "text": (
                                    f"STRM：{strm_ready}/{active_resources} 已生成 · "
                                    f"{strm_waiting} 待处理 · {strm_failed} 失败 · "
                                    f"{strm_percent}%"
                                ),
                            },
                            {
                                "component": "VProgressLinear",
                                "props": {
                                    "model-value": strm_percent,
                                    "color": "success",
                                    "height": 12,
                                    "rounded": True,
                                    "class": "mb-4",
                                },
                            },
                            {
                                "component": "div",
                                "props": {"class": "mb-1"},
                                "text": (
                                    f"元数据刮削：{scrape_ready}/{active_resources} 完成 · "
                                    f"{scrape_unrecognized} 未识别但已生成 · "
                                    f"{scrape_waiting} 等待 · {scrape_blocked} 前序阻断 · "
                                    f"{scrape_failed} 失败 · {scrape_percent}%"
                                ),
                            },
                            {
                                "component": "VProgressLinear",
                                "props": {
                                    "model-value": scrape_percent,
                                    "color": "info",
                                    "height": 12,
                                    "rounded": True,
                                    "class": "mb-4",
                                },
                            },
                            {
                                "component": "div",
                                "props": {"class": "text-subtitle-1"},
                                "text": "当前处理",
                            },
                            {
                                "component": "VList",
                                "content": current_items
                                or [
                                    {
                                        "component": "VListItem",
                                        "props": {"title": "当前没有正在处理的资源"},
                                    }
                                ],
                            },
                        ],
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-4"},
                "content": [
                    {"component": "VCardTitle", "text": "工作表断点"},
                    {
                        "component": "VList",
                        "content": sheet_items
                        or [
                            {
                                "component": "VListItem",
                                "props": {"title": "尚未发现工作表"},
                            }
                        ],
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {"component": "VCardTitle", "text": "最近错误"},
                    {
                        "component": "VList",
                        "content": error_items
                        or [{"component": "VListItem", "props": {"title": "暂无错误"}}],
                    },
                ],
            },
        ]

    def stop_service(self) -> None:
        """通知进行中的同步或构建在当前小步骤后安全退出。"""
        self._search_bridge.uninstall()
        self._stop_event.set()
        self._pause_event.set()
        if self._direct_downloader:
            self._direct_downloader.stop_all()
        if self._gateway:
            self._gateway.stop()
