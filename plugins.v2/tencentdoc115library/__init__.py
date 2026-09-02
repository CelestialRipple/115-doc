from concurrent.futures import Future
from secrets import compare_digest, token_urlsafe
from threading import Event, Lock
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from fastapi import Body, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response as HttpResponse

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
from .resolver import ShareResolutionError, ShareResolver
from .schemas import BuildActionRequest, ResourceRetryRequest, SyncActionRequest
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
    "client_id": "",
    "client_secret": "",
    "openid": "",
    "access_token": "",
    "refresh_token": "",
    "page_rows": 1000,
    "pages_per_run": 5,
    "max_columns": 10,
    "build_batch": 20,
    "output_root": "/media/tencentdoc115",
    "output_size_limit_gb": 0,
    "public_base_url": "http://127.0.0.1:3000",
    "playback_token": "",
    "scrape_metadata": True,
    "native_search_enabled": True,
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
    plugin_desc = "匿名校验 115 分享并识别电影、剧集，使用 MoviePilot 刮削和按需直链。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/src/assets/images/misc/u115.png"
    plugin_version = "0.8.1"
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
        self._task_lock = Lock()
        self._pipeline_lock = Lock()
        self._future: Optional[Future] = None
        self._store: Optional[CatalogStore] = None
        self._synchronizer: Optional[CatalogSynchronizer] = None
        self._resolver: Optional[ShareResolver] = None
        self._builder: Optional[LibraryBuilder] = None
        self._direct_downloader: Optional[DirectDownloadManager] = None
        self._gateway: Optional[DirectPlayGateway] = None
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
        self._config = {**DEFAULT_CONFIG, **(config or {})}
        if not self._config.get("playback_token"):
            self._config["playback_token"] = token_urlsafe(32)
            self.update_config(dict(self._config))
        self._enabled = bool(self._config.get("enabled"))
        data_path = self.get_data_path()
        self._store = CatalogStore(data_path / "catalog.db")
        retried = self._store.retry_errors_containing(
            "unexpected keyword argument 'mtype'"
        )
        if retried:
            logger.info(f"已自动重新排队 {retried} 条 V2 识别接口兼容失败资源")
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
        )
        self._builder = LibraryBuilder(
            store=self._store,
            resolver=self._resolver,
            config_provider=self._current_config,
            stop_event=self._stop_event,
        )
        self._direct_downloader = DirectDownloadManager(
            store=self._store,
            resolver=self._resolver,
            config_provider=self._current_config,
        )
        self._gateway.start_background()

    def _current_config(self) -> Dict[str, Any]:
        """返回当前配置的副本。"""
        return dict(self._config)

    def _replace_config(self, config: Dict[str, Any]) -> None:
        """保存同步器发现的工作表和文件 ID 配置。"""
        self._config = {**DEFAULT_CONFIG, **config}
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

    def _submit(self, name: str, function: Any, *args: Any, **kwargs: Any) -> Response:
        """把长任务提交到 MoviePilot 共享线程池。"""
        with self._task_lock:
            if self._future and not self._future.done():
                return Response(success=False, message="已有同步或构建任务正在运行")
            self._stop_event.clear()
            self._future = ThreadHelper().submit(function, *args, **kwargs)
            self._future.add_done_callback(lambda future: self._task_done(name, future))
        return Response(success=True, message=f"{name}已在后台启动")

    @staticmethod
    def _task_done(name: str, future: Future) -> None:
        """记录后台任务异常，但不泄露任何凭据。"""
        try:
            result = future.result()
            logger.info(f"{name}完成：{result}")
        except Exception as error:
            logger.error(f"{name}失败：{error}")

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def get_module(self) -> Dict[str, Any]:
        """把本地目录检索和 115 直链任务接入 MoviePilot 原生流程。"""
        if not self._config.get("native_search_enabled", True):
            return {}
        if not self._direct_downloader:
            return {"search_torrents": self.search_torrents}
        return {
            "search_torrents": self.search_torrents,
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
        raw_type = str(resource.get("media_type") or "").lower()
        group_name = str(resource.get("group_name") or "").lower()
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
        if not self._store or not str(keyword or "").strip():
            return []
        page_size = min(
            max(int(self._config.get("search_page_size") or 50), 1),
            100,
        )
        page_index = max(int(page or 0), 0)
        resources = self._store.search_resources(
            keyword=keyword,
            limit=page_size,
            offset=page_index * page_size,
            ready_only=bool(self._config.get("search_ready_only", True)),
        )
        results: List[TorrentInfo] = []
        for resource in resources:
            media_type = self._resource_media_type(resource)
            if mtype and media_type != mtype:
                continue
            title = str(resource.get("title") or "").strip()
            year = str(resource.get("year") or "").strip()
            version = str(resource.get("version") or "").strip()
            display_title = f"{title} ({year})" if year else title
            if version:
                display_title = f"{display_title} {version}"
            details = [
                str(resource.get("group_name") or "腾讯文档"),
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
                    size=0,
                    seeders=1,
                    labels=[
                        "腾讯文档",
                        "115分享",
                        str(resource.get("group_name") or "未分组"),
                    ],
                    pri_order=100,
                    category=media_type.value,
                )
            )
        return results

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
        while not self._stop_event.is_set():
            result = self._synchronizer.sync(mode="manual-all")
            totals["synced_pages"] += int(result.get("processed_pages") or 0)
            totals["synced_rows"] += int(result.get("processed_rows") or 0)
            sync_status = str(result.get("status") or "failed")
            self._set_pipeline_status(
                phase="syncing",
                message=str(result.get("message") or "正在同步"),
                **totals,
            )
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

        self._set_pipeline_status(
            phase="building",
            message="目录同步完成，正在排队识别、刮削并生成 STRM",
            **totals,
        )
        known_usage_bytes: Optional[int] = None
        while not self._stop_event.is_set():
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

    def stop_background_tasks(self) -> Response:
        """请求当前同步或生成任务在安全检查点停止。"""
        if not self._future or self._future.done():
            return Response(success=True, message="当前没有正在运行的后台任务")
        self._stop_event.set()
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
        )

    def retry_resources(self, payload: ResourceRetryRequest = Body(...)) -> Response:
        """将用户选定的失败资源重新加入生成队列。"""
        if not self._store:
            return Response(success=False, message="插件尚未初始化")
        count = self._store.retry_resources(payload.resource_ids)
        return Response(success=True, message=f"已重新排队 {count} 条资源")

    def _retry_all_failed_and_build(self) -> Dict[str, Any]:
        """重新排队全部失败资源，并连续按批次重试一次。"""
        if not self._store or not self._builder:
            return {"status": "failed", "message": "插件尚未初始化"}
        requeued = self._store.retry_all_failed_resources()
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
            message=f"已重新排队 {requeued} 条失败资源，正在按批次重试",
            **totals,
        )
        known_usage_bytes: Optional[int] = None
        while not self._stop_event.is_set():
            result = self._builder.build(known_usage_bytes=known_usage_bytes)
            processed = int(result.get("processed") or 0)
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
            if build_status == "space_limit":
                self._set_pipeline_status(
                    phase="space_limit",
                    message="重试已因输出空间上限停止，未处理资源仍为 pending",
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
                    phase="completed",
                    message=(
                        f"失败资源已完成一轮重试：{totals['success']} 成功 / "
                        f"{totals['failed']} 再次失败"
                    ),
                    **totals,
                )
                return {"status": "completed", "requeued": requeued, **totals}
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
        snapshot["task_running"] = bool(self._future and not self._future.done())
        snapshot["pipeline"] = self._pipeline_snapshot()
        snapshot["storage"] = self._builder.storage_snapshot() if self._builder else {}
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
        """校验 STRM 密钥，按需解析 115 临时地址并返回 307。"""
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
            return RedirectResponse(url=direct_url, status_code=307)
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
        sheets = self._store.list_sheets() if self._store else []
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
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "search_ready_only",
                                            "label": "只检索已生成STRM资源",
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
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
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
                                "props": {"cols": 12, "md": 3},
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
                                3,
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
        gateway = (
            self._gateway.status()
            if self._gateway
            else {"enabled": False, "state": "disabled", "port": 8097}
        )
        buttons = [
            ("发现工作表", "mdi-table-search", "discover", "primary"),
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
            ("停止后台任务", "mdi-stop-circle", "tasks/stop", "error"),
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
                                f"{pipeline_text} 输出目录占用：{usage_text} / {limit_text}。"
                                "“同步全部并生成”会在后台跑完已勾选工作表，再逐批处理 pending；"
                                "可随时安全停止，重新点击会从断点继续。重新打开本页可刷新状态。"
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
        self._stop_event.set()
        if self._direct_downloader:
            self._direct_downloader.stop_all()
        if self._gateway:
            self._gateway.stop()
