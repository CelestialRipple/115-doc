import hashlib
import json
import os
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from concurrent.futures import wait
from requests.exceptions import RequestException
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from app.schemas import DownloaderTorrent
from app.schemas.types import TorrentStatus

try:
    from app.runtime.thread import ThreadHelper
except ImportError:
    from app.helper.thread import ThreadHelper

try:
    from app.sdk.config import settings
    from app.sdk.logging import logger
    from app.sdk.network import RequestUtils
except ImportError:
    from app.core.config import settings
    from app.log import logger
    from app.utils.http import RequestUtils

from .download_marker import parse_download_marker
from .library import safe_path_segment, resource_suffix, LibraryBuilder, truncate_utf8
from .resolver import ShareResolutionError, ShareResolver
from .source_link import is_offline_link, offline_file_hint
from .store import CatalogStore


DIRECT_DOWNLOADER_NAME = "腾讯文档115直链"


class DirectDownloadManager:
    """以 MoviePilot 下载器模块形式管理 115 分享直链下载任务。"""

    def __init__(
        self,
        store: CatalogStore,
        resolver: ShareResolver,
        config_provider: Callable[[], Dict[str, Any]],
    ):
        """初始化直链下载管理器。"""
        self.store = store
        self.resolver = resolver
        self.config_provider = config_provider
        self._request = RequestUtils(proxies=settings.PROXY, timeout=60)
        self._task_events: Dict[str, Event] = {}
        self._task_events_lock = Lock()
        self._task_futures = {}
        self._restart_tasks = set()
        self._closing = False

    @staticmethod
    def _task_id(resource_id: str, download_dir: Path) -> str:
        identity = f"{resource_id}:{download_dir.resolve()}"
        return hashlib.sha1(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_tv(resource: Dict[str, Any]) -> bool:
        media_type = str(
            resource.get("detected_media_type") or resource.get("media_type") or ""
        ).lower()
        group_name = str(
            resource.get("detected_group_name") or resource.get("group_name") or ""
        ).lower()
        return any(
            keyword in media_type or keyword in group_name
            for keyword in ("电视剧", "剧集", "连续剧", "tv", "番剧")
        )

    def download(
        self,
        content: Union[Path, str, bytes],
        download_dir: Path,
        cookie: str,
        episodes: Optional[Set[int]] = None,
        category: Optional[str] = None,
        label: Optional[str] = None,
        downloader: Optional[str] = None,
    ) -> Optional[Tuple[Optional[str], Optional[str], Optional[str], str]]:
        """接管插件检索结果并立即创建后台直链下载任务。"""
        resource_id = parse_download_marker(content)
        if not resource_id:
            return None
        if self.config_provider().get("browser_download_enabled", True):
            return (
                DIRECT_DOWNLOADER_NAME,
                None,
                None,
                "已启用浏览器下载，请使用浏览器直链入口（原生按钮需安装配套前端适配）",
            )
        resource = self.store.get_resource(resource_id)
        if not resource or resource.get("status") == "removed":
            return DIRECT_DOWNLOADER_NAME, None, None, "资源不存在或已移除"
        target_dir = Path(download_dir).expanduser().resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        task_id = self._task_id(resource_id, target_dir)
        selected_episodes = sorted(int(item) for item in (episodes or set()))
        self.store.upsert_download_task(
            {
                "task_id": task_id,
                "resource_id": resource_id,
                "title": str(resource.get("title") or resource_id),
                "download_dir": str(target_dir),
                "episodes_json": json.dumps(selected_episodes),
            }
        )
        self._start_task(task_id)
        return DIRECT_DOWNLOADER_NAME, task_id, "NoSubfolder", "直链下载任务已创建"

    def _start_task(self, task_id: str) -> None:
        with self._task_events_lock:
            current = self._task_events.get(task_id)
            if self._closing:
                return
            if current is not None:
                if current.is_set():
                    self._restart_tasks.add(task_id)
                return
            stop_event = Event()
            self._task_events[task_id] = stop_event
            future = ThreadHelper().submit(self._run_task, task_id, stop_event)
            self._task_futures[task_id] = future

        def finished(done):
            with self._task_events_lock:
                if self._task_futures.get(task_id) is done:
                    self._task_futures.pop(task_id, None)

        future.add_done_callback(finished)

    def _select_files(
        self,
        resource: Dict[str, Any],
        selected_episodes: Set[int],
    ) -> List[Dict[str, Any]]:
        if is_offline_link(str(resource.get("share_url") or "")):
            self.resolver.resolve(
                resource["resource_id"],
                user_agent="MoviePilot TencentDoc115Library",
            )
            record = self.store.get_offline_playback(resource["resource_id"]) or {}
            if record.get("pick_code"):
                return [
                    {
                        "file_id": record.get("owned_file_id") or "offline",
                        "file_name": record.get("file_name") or "video.mkv",
                        "file_path": f"/{record.get('file_name') or 'video.mkv'}",
                        "file_size": int(record.get("file_size") or 0),
                    }
                ]
            return [
                offline_file_hint(
                    resource["share_url"],
                    str(resource.get("title") or "未命名"),
                )
            ]
        if self._is_tv(resource):
            files = self.store.list_resource_files(resource["resource_id"])
            if not files:
                files = self.resolver.list_video_files(resource["share_url"])
            files = [
                {
                    **item,
                    "season": item.get("season")
                    or LibraryBuilder._episode_identity(
                        item["file_name"], str(item.get("file_path") or "")
                    )[0],
                    "episode": item.get("episode")
                    or LibraryBuilder._episode_identity(
                        item["file_name"], str(item.get("file_path") or "")
                    )[1],
                }
                for item in files
            ]
            if selected_episodes:
                files = [
                    item
                    for item in files
                    if int(item.get("episode") or 0) in selected_episodes
                ]
            if not files:
                raise ShareResolutionError(
                    "没有匹配到要下载的剧集文件",
                    status_code=404,
                    retryable=False,
                )
            return files
        return [
            self.resolver.choose_movie_file(
                self.resolver.list_video_files(resource["share_url"])
            )
        ]

    def _target_path(
        self,
        resource: Dict[str, Any],
        source_file: Dict[str, Any],
        download_dir: Path,
        multiple: bool,
    ) -> Path:
        source_name = safe_path_segment(str(source_file.get("file_name") or "video"))
        extension = Path(source_name).suffix
        if not self._is_tv(resource):
            title = safe_path_segment(str(resource.get("title") or "未命名"))
            year = str(resource.get("year") or "").strip()
            version = safe_path_segment(str(resource.get("version") or ""), "")
            stem = f"{title} ({year})" if year else title
            if version:
                stem = f"{stem} - {version}"
            stem = truncate_utf8(safe_path_segment(stem), 150)
            return (
                download_dir
                / f"{stem} - {resource_suffix(resource)}{extension or '.mkv'}"
            )
        title = truncate_utf8(
            safe_path_segment(str(resource.get("title") or "未命名剧集")), 150
        )
        season = int(source_file.get("season") or 1)
        base = (
            download_dir
            / f"{title} - {resource_suffix(resource)}"
            / f"Season {season:02d}"
        )
        suffix = hashlib.sha256(
            str(source_file.get("file_id") or source_name).encode()
        ).hexdigest()[:12]
        target = (
            base
            / f"{truncate_utf8(Path(source_name).stem, 150)} - {suffix}{extension or '.mkv'}"
        )
        if download_dir.resolve() not in target.resolve().parents:
            raise RuntimeError("下载路径超出指定目录")
        return target

    def _download_one(
        self,
        task_id: str,
        resource: Dict[str, Any],
        source_file: Dict[str, Any],
        target: Path,
        stop_event: Event,
        aggregate_completed: int,
        aggregate_total: int,
    ) -> int:
        config = self.config_provider()
        retries = min(max(int(config.get("download_retries", 4)), 0), 10)
        chunk_size = min(
            max(int(config.get("download_chunk_size") or 1024 * 1024), 64 * 1024),
            8 * 1024 * 1024,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(f"{target.suffix}.part")
        if target.is_symlink() or partial.is_symlink():
            raise RuntimeError("下载目标不能是符号链接")
        for attempt in range(retries + 1):
            if stop_event.is_set():
                raise InterruptedError("下载已暂停")
            expected_size = int(source_file.get("file_size") or 0)
            if target.exists():
                if expected_size and target.stat().st_size == expected_size:
                    return expected_size
                raise RuntimeError("目标文件已存在且无法确认完整性，已保留原文件")
            existing = partial.stat().st_size if partial.exists() else 0
            headers = {"User-Agent": "MoviePilot TencentDoc115Library"}
            if existing:
                headers["Range"] = f"bytes={existing}-"
            if is_offline_link(str(resource.get("share_url") or "")):
                direct_url = self.resolver.resolve(
                    resource["resource_id"],
                    user_agent=headers["User-Agent"],
                )
            else:
                direct_url = self.resolver.resolve_file_url(
                    resource["share_url"],
                    str(source_file["file_id"]),
                    headers["User-Agent"],
                    force_refresh=attempt > 0,
                )
            response = self._request.get_res(
                url=direct_url,
                headers=headers,
                stream=True,
                allow_redirects=True,
            )
            if response is None:
                if attempt < retries:
                    continue
                raise RuntimeError("115 直链下载无响应")
            try:
                if response.status_code not in (200, 206):
                    if attempt < retries and response.status_code in (
                        403,
                        429,
                        500,
                        502,
                        503,
                        504,
                    ):
                        if stop_event.wait(min(2**attempt, 8)):
                            raise InterruptedError("下载已暂停")
                        self.resolver.clear_url_cache()
                        continue
                    raise RuntimeError(f"115 直链下载失败：HTTP {response.status_code}")
                append = existing > 0 and response.status_code == 206
                if response.status_code == 206 and not str(
                    response.headers.get("Content-Range") or ""
                ).startswith(f"bytes {existing}-"):
                    raise RuntimeError("续传响应范围不匹配，已保留临时文件")
                if not append:
                    existing = 0
                started = monotonic()
                current = existing
                with partial.open("ab" if append else "wb") as output:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if stop_event.is_set():
                            raise InterruptedError("下载已暂停")
                        if not chunk:
                            continue
                        output.write(chunk)
                        current += len(chunk)
                        elapsed = max(monotonic() - started, 0.001)
                        self.store.update_download_task(
                            task_id,
                            state="downloading",
                            downloaded_size=aggregate_completed + current,
                            total_size=aggregate_total,
                            speed=int((current - existing) / elapsed),
                        )
                if expected_size and current != expected_size:
                    raise ConnectionError(f"文件大小不完整：{current}/{expected_size}")
                # Atomic no-clobber publication; another writer cannot replace an existing video.
                os.link(partial, target)
                partial.unlink()
                return current
            except (RequestException, ConnectionError, TimeoutError) as error:
                if attempt >= retries:
                    raise RuntimeError("下载中断，重试次数已用尽") from error
                if stop_event.wait(min(2**attempt, 8)):
                    raise InterruptedError("下载已暂停")
            finally:
                response.close()
        raise RuntimeError("115 直链下载重试次数已用尽")

    def _run_task(self, task_id: str, stop_event: Event) -> None:
        try:
            self._execute_task(task_id, stop_event)
        finally:
            with self._task_events_lock:
                self._task_events.pop(task_id, None)
                restart = task_id in self._restart_tasks and not self._closing
                self._restart_tasks.discard(task_id)
            if restart:
                self._start_task(task_id)

    def _execute_task(self, task_id: str, stop_event: Event) -> None:
        tasks = self.store.list_download_tasks(
            task_ids=[task_id],
            include_organized=True,
        )
        if not tasks:
            return
        task = tasks[0]
        resource = self.store.get_resource(task["resource_id"])
        if not resource:
            self.store.update_download_task(
                task_id,
                state="failed",
                last_error="资源不存在",
            )
            return
        try:
            episodes = {
                int(item) for item in json.loads(task.get("episodes_json") or "[]")
            }
            files = self._select_files(resource, episodes)
            total_size = sum(int(item.get("file_size") or 0) for item in files)
            download_dir = Path(task["download_dir"])
            completed = 0
            content_paths = []
            self.store.update_download_task(
                task_id,
                state="downloading",
                total_size=total_size,
                downloaded_size=0,
                speed=0,
            )
            for source_file in files:
                if stop_event.is_set():
                    raise InterruptedError("下载已暂停")
                target = self._target_path(
                    resource,
                    source_file,
                    download_dir,
                    multiple=len(files) > 1,
                )
                completed += self._download_one(
                    task_id,
                    resource,
                    source_file,
                    target,
                    stop_event,
                    completed,
                    total_size,
                )
                content_paths.append(target)
            content_path = (
                str(content_paths[0])
                if len(content_paths) == 1
                else str(
                    download_dir
                    / f"{truncate_utf8(safe_path_segment(resource['title']), 150)} - {resource_suffix(resource)}"
                )
            )
            self.store.update_download_task(
                task_id,
                state="completed",
                content_path=content_path,
                downloaded_size=completed,
                total_size=max(total_size, completed),
                speed=0,
                last_error=None,
            )
            logger.info(f"115 直链下载完成：{resource['title']}")
        except InterruptedError as error:
            self.store.update_download_task(
                task_id,
                state="paused",
                speed=0,
                last_error=str(error),
            )
        except Exception as error:
            self.store.update_download_task(
                task_id,
                state="failed",
                speed=0,
                last_error=str(error),
            )
            logger.error(f"115 直链下载失败：{resource['title']} - {error}")

    @staticmethod
    def _task_states(status: Any) -> Optional[List[str]]:
        value = getattr(status, "value", status)
        if value in (TorrentStatus.DOWNLOADING.value, "downloading", "下载中"):
            return ["queued", "downloading", "paused", "failed"]
        if value in (TorrentStatus.TRANSFER.value, "transfer", "可转移"):
            return ["completed"]
        return None

    def list_torrents(
        self,
        status: Any = None,
        hashs: Optional[Union[List[str], str]] = None,
        downloader: Optional[str] = None,
        include_all_tags: bool = False,
    ) -> Optional[List[DownloaderTorrent]]:
        """向 MoviePilot 报告直链任务进度和可整理状态。"""
        if downloader and downloader != DIRECT_DOWNLOADER_NAME:
            return None
        task_ids = [hashs] if isinstance(hashs, str) else list(hashs or [])
        tasks = self.store.list_download_tasks(
            states=self._task_states(status),
            task_ids=task_ids or None,
        )
        results = []
        for task in tasks:
            total = int(task.get("total_size") or 0)
            downloaded = int(task.get("downloaded_size") or 0)
            progress = downloaded / total * 100 if total else 0
            results.append(
                DownloaderTorrent(
                    downloader=DIRECT_DOWNLOADER_NAME,
                    hash=task["task_id"],
                    title=task["title"],
                    name=task["title"],
                    path=Path(task.get("content_path") or task["download_dir"]),
                    save_path=task["download_dir"],
                    content_path=task.get("content_path") or task["download_dir"],
                    size=total,
                    progress=progress,
                    state=task["state"],
                    dlspeed=f"{int(task.get('speed') or 0) / 1024 / 1024:.1f} MB/s",
                    tags="腾讯文档115",
                )
            )
        return results

    def stop_torrents(
        self,
        hashs: Union[List[str], str],
        downloader: Optional[str] = None,
    ) -> Optional[bool]:
        """暂停直链下载并保留 `.part` 文件。"""
        if downloader and downloader != DIRECT_DOWNLOADER_NAME:
            return None
        task_ids = [hashs] if isinstance(hashs, str) else list(hashs)
        with self._task_events_lock:
            for task_id in task_ids:
                event = self._task_events.get(task_id)
                if event:
                    event.set()
                self.store.update_download_task(task_id, state="paused", speed=0)
        return True

    def has_active_tasks(self) -> bool:
        with self._task_events_lock:
            return bool(self._task_events)

    def stop_all(self) -> None:
        """插件停用或重载时请求所有本插件下载线程安全暂停。"""
        with self._task_events_lock:
            self._closing = True
            task_ids = list(self._task_events)
        if task_ids:
            self.stop_torrents(task_ids, DIRECT_DOWNLOADER_NAME)
        with self._task_events_lock:
            futures = list(self._task_futures.values())
        if futures and wait(futures, timeout=10).not_done:
            raise RuntimeError("下载任务仍在停止，请稍后重新保存配置")

    def start_torrents(
        self,
        hashs: Union[List[str], str],
        downloader: Optional[str] = None,
    ) -> Optional[bool]:
        """从 `.part` 文件恢复直链下载。"""
        if self.config_provider().get("browser_download_enabled", True):
            return False
        if downloader and downloader != DIRECT_DOWNLOADER_NAME:
            return None
        task_ids = [hashs] if isinstance(hashs, str) else list(hashs)
        for task_id in task_ids:
            self.store.update_download_task(
                task_id,
                state="queued",
                last_error=None,
                organized=0,
            )
            self._start_task(task_id)
        return True

    def remove_torrents(
        self,
        hashs: Union[List[str], str],
        delete_file: bool = False,
        downloader: Optional[str] = None,
    ) -> Optional[bool]:
        """仅移除任务记录；为安全起见不会通过该入口删除媒体文件。"""
        if downloader and downloader != DIRECT_DOWNLOADER_NAME:
            return None
        self.stop_torrents(hashs, downloader)
        task_ids = [hashs] if isinstance(hashs, str) else list(hashs)
        self.store.delete_download_tasks(task_ids)
        return True

    def set_torrents_tag(
        self,
        hashs: Union[List[str], str],
        tags: Union[List[str], str],
        downloader: Optional[str] = None,
    ) -> Optional[bool]:
        """接收 MoviePilot 的“已整理”标记，避免重复进入整理队列。"""
        if downloader and downloader != DIRECT_DOWNLOADER_NAME:
            return None
        tag_values = [tags] if isinstance(tags, str) else list(tags)
        if "已整理" not in tag_values:
            return True
        task_ids = [hashs] if isinstance(hashs, str) else list(hashs)
        for task_id in task_ids:
            self.store.update_download_task(task_id, organized=1)
        return True

    def transfer_completed(
        self,
        hashs: Union[List[str], str],
        downloader: Optional[str] = None,
    ) -> Optional[bool]:
        """接收 MoviePilot 整理完成回调并从待整理任务中隐藏。"""
        if downloader and downloader != DIRECT_DOWNLOADER_NAME:
            return None
        task_ids = [hashs] if isinstance(hashs, str) else list(hashs)
        for task_id in task_ids:
            self.store.update_download_task(task_id, organized=1)
        return True
