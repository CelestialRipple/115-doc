import os
import re
from pathlib import Path
from threading import Event, Lock
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from app.chain.media import MediaChain
from app.chain.scraping import ScrapingChain
from app.schemas.file import FileItem
from app.schemas.types import MediaType
from app.sdk.logging import logger
from app.sdk.media import MetaInfo

from .resolver import ShareResolutionError, ShareResolver
from .storage_limit import configured_limit_bytes, directory_size
from .store import CatalogStore


INVALID_PATH_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_path_segment(value: str, fallback: str = "未命名") -> str:
    """
    把外部标题转换为安全的单个路径片段

    :param value (str): 原始标题
    :param fallback (str): 清理后为空时的替代名称

    :return str: 安全路径片段
    """
    result = INVALID_PATH_CHARACTERS.sub(" ", str(value or ""))
    result = re.sub(r"\s+", " ", result).strip(" .")
    return result[:180] or fallback


class LibraryBuildError(RuntimeError):
    """媒体识别、STRM 生成或本地刮削错误"""


class LibraryBuilder:
    """
    把目录记录限量生成为本地 STRM 和 MoviePilot 元数据

    电影不会在生成阶段访问 115，电视剧为了建立集列表会在此阶段展开单条分享
    """

    def __init__(
        self,
        store: CatalogStore,
        resolver: ShareResolver,
        config_provider: Callable[[], Dict[str, Any]],
        stop_event: Event,
    ):
        """
        初始化媒体库生成器

        :param store (CatalogStore): 目录存储
        :param resolver (ShareResolver): 115 分享解析器
        :param config_provider (Callable): 插件配置读取函数
        :param stop_event (Event): 插件停止信号
        """
        self.store = store
        self.resolver = resolver
        self.config_provider = config_provider
        self.stop_event = stop_event
        self._run_lock = Lock()

    @staticmethod
    def _media_type(resource: Dict[str, Any]) -> MediaType:
        raw_type = str(resource.get("media_type") or "").lower()
        group_name = str(resource.get("group_name") or "").lower()
        if any(
            keyword in raw_type or keyword in group_name
            for keyword in ("电视剧", "剧集", "连续剧", "tv", "番剧")
        ):
            return MediaType.TV
        return MediaType.MOVIE

    @staticmethod
    def _recognize(resource: Dict[str, Any], media_type: MediaType) -> Tuple[Any, Any]:
        title = str(resource.get("title") or "").strip()
        version = str(resource.get("version") or "").strip()
        meta = MetaInfo(title=title, subtitle=version or None)
        meta.year = str(resource.get("year") or "").strip() or None
        meta.type = media_type
        mediainfo = MediaChain().recognize_by_meta(
            metainfo=meta,
            mtype=media_type,
            obtain_images=True,
        )
        if not mediainfo:
            raise LibraryBuildError(
                f"MoviePilot 未识别到媒体：{title}"
                + (f" ({meta.year})" if meta.year else "")
            )
        if mediainfo.type not in {MediaType.MOVIE, MediaType.TV}:
            raise LibraryBuildError(f"MoviePilot 返回了不支持的媒体类型：{mediainfo.type}")
        return meta, mediainfo

    @staticmethod
    def _media_directory_name(resource: Dict[str, Any], mediainfo: Any) -> str:
        title = str(mediainfo.title or resource.get("title") or "未命名").strip()
        year = str(mediainfo.year or resource.get("year") or "").strip()
        return safe_path_segment(f"{title} ({year})" if year else title)

    def _base_directory(
        self,
        resource: Dict[str, Any],
        mediainfo: Any,
    ) -> Path:
        config = self.config_provider()
        output_root = str(config.get("output_root") or "").strip()
        if not output_root:
            raise LibraryBuildError("未配置 STRM 输出目录")
        root = Path(output_root).expanduser().resolve()
        group = safe_path_segment(str(resource.get("group_name") or "未分组"))
        media_directory = self._media_directory_name(resource, mediainfo)
        target = (root / group / media_directory).resolve()
        if root != target and root not in target.parents:
            raise LibraryBuildError("生成路径超出 STRM 输出目录")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def storage_snapshot(self) -> Dict[str, Any]:
        """返回插件输出目录的当前占用和配置上限。"""
        config = self.config_provider()
        output_root = str(config.get("output_root") or "").strip()
        root = Path(output_root).expanduser().resolve() if output_root else None
        usage_bytes = directory_size(root) if root else 0
        limit_bytes = configured_limit_bytes(config)
        return {
            "output_root": str(root) if root else "",
            "usage_bytes": usage_bytes,
            "limit_bytes": limit_bytes,
            "limit_reached": bool(limit_bytes and usage_bytes >= limit_bytes),
        }

    def _play_url(self, resource_id: str, file_id: Optional[str] = None) -> str:
        public_base_url = str(
            self.config_provider().get("public_base_url") or ""
        ).strip()
        if not public_base_url:
            raise LibraryBuildError("未配置播放器可访问的 MoviePilot 地址")
        url = (
            f"{public_base_url.rstrip('/')}/api/v1/plugin/"
            f"TencentDoc115Library/play/{resource_id}"
        )
        query = {"token": str(self.config_provider().get("playback_token") or "")}
        if not query["token"]:
            raise LibraryBuildError("插件播放密钥尚未初始化")
        if file_id:
            query["file_id"] = file_id
        return f"{url}?{urlencode(query)}"

    @staticmethod
    def _write_strm(path: Path, url: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        expected = f"{url}\n"
        if path.exists() and path.read_text(encoding="utf-8") == expected:
            return
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(expected, encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def _file_item(path: Path) -> FileItem:
        stat = path.stat()
        return FileItem(
            storage="local",
            type="dir" if path.is_dir() else "file",
            path=str(path),
            name=path.name,
            basename=path.stem,
            extension=path.suffix[1:] if path.is_file() else None,
            size=stat.st_size if path.is_file() else None,
            modify_time=stat.st_mtime,
        )

    def _scrape(self, directory: Path, meta: Any, mediainfo: Any) -> None:
        if not self.config_provider().get("scrape_metadata", True):
            return
        success, message = ScrapingChain().scrape_metadata(
            fileitem=self._file_item(directory),
            meta=meta,
            mediainfo=mediainfo,
            init_folder=True,
            overwrite=False,
            recursive=True,
        )
        if not success:
            raise LibraryBuildError(f"MoviePilot 刮削失败：{message}")

    def _build_movie(
        self,
        resource: Dict[str, Any],
        meta: Any,
        mediainfo: Any,
        directory: Optional[Path] = None,
    ) -> str:
        directory = directory or self._base_directory(resource, mediainfo)
        base_name = self._media_directory_name(resource, mediainfo)
        version = safe_path_segment(str(resource.get("version") or ""), "")
        filename = f"{base_name} - {version}.strm" if version else f"{base_name}.strm"
        strm_path = directory / filename
        self._write_strm(strm_path, self._play_url(resource["resource_id"]))
        self._scrape(directory, meta, mediainfo)
        return str(strm_path)

    @staticmethod
    def _episode_identity(file_name: str) -> Tuple[Optional[int], Optional[int]]:
        meta = MetaInfo(file_name)
        season = meta.begin_season
        episode = meta.begin_episode
        if episode is None:
            match = re.search(
                r"(?:S(?P<season>\d{1,2})[ ._-]*)?E(?P<episode>\d{1,4})",
                file_name,
                re.IGNORECASE,
            )
            if match:
                season = int(match.group("season") or 1)
                episode = int(match.group("episode"))
        if episode is None:
            match = re.search(r"第\s*(\d{1,4})\s*[集话]", file_name)
            if match:
                season = season or 1
                episode = int(match.group(1))
        return season or (1 if episode is not None else None), episode

    def _build_tv(
        self,
        resource: Dict[str, Any],
        meta: Any,
        mediainfo: Any,
        directory: Optional[Path] = None,
    ) -> str:
        directory = directory or self._base_directory(resource, mediainfo)
        source_files = self.resolver.list_video_files(resource["share_url"])
        expanded_files: List[Dict[str, Any]] = []
        unrecognized_files = []
        media_name = self._media_directory_name(resource, mediainfo)
        for source_file in source_files:
            season, episode = self._episode_identity(source_file["file_name"])
            if episode is None:
                unrecognized_files.append(source_file["file_name"])
                continue
            season_directory = directory / f"Season {season:02d}"
            strm_path = season_directory / (
                f"{media_name} - S{season:02d}E{episode:02d}.strm"
            )
            if any(item.get("strm_path") == str(strm_path) for item in expanded_files):
                suffix = safe_path_segment(Path(source_file["file_name"]).stem)
                strm_path = season_directory / (
                    f"{media_name} - S{season:02d}E{episode:02d} - {suffix}.strm"
                )
            self._write_strm(
                strm_path,
                self._play_url(resource["resource_id"], source_file["file_id"]),
            )
            expanded_files.append(
                {
                    **source_file,
                    "season": season,
                    "episode": episode,
                    "strm_path": str(strm_path),
                }
            )
        if not expanded_files:
            example = "、".join(unrecognized_files[:3])
            raise LibraryBuildError(
                "分享中存在视频，但无法识别任何剧集编号"
                + (f"：{example}" if example else "")
            )
        self.store.replace_resource_files(resource["resource_id"], expanded_files)
        self._scrape(directory, meta, mediainfo)
        return str(directory)

    def build(
        self,
        limit: Optional[int] = None,
        retry_failed: bool = False,
        known_usage_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        限量处理待生成资源

        :param limit (int): 本次最大资源数
        :param retry_failed (bool): 是否包含失败资源
        :param known_usage_bytes (int): 连续批次沿用的已知目录占用，避免反复扫描

        :return Dict: 处理统计
        """
        if not self._run_lock.acquire(blocking=False):
            return {
                "status": "busy",
                "message": "已有媒体库生成任务正在运行",
                "processed": 0,
                "success": 0,
                "failed": 0,
            }
        processed = 0
        success_count = 0
        failed_count = 0
        space_limit_reached = False
        usage_bytes = 0
        limit_bytes = 0
        try:
            config = self.config_provider()
            limit_bytes = configured_limit_bytes(config)
            usage_bytes = (
                max(int(known_usage_bytes), 0)
                if known_usage_bytes is not None
                else int(self.storage_snapshot()["usage_bytes"])
            )
            if limit_bytes and usage_bytes >= limit_bytes:
                return {
                    "status": "space_limit",
                    "message": "输出目录已达到空间上限，剩余资源保留为 pending",
                    "processed": 0,
                    "success": 0,
                    "failed": 0,
                    "usage_bytes": usage_bytes,
                    "limit_bytes": limit_bytes,
                }
            batch_limit = limit or int(config.get("build_batch") or 20)
            batch_limit = min(max(int(batch_limit), 1), 500)
            resources = self.store.list_build_candidates(
                batch_limit,
                retry_failed=retry_failed,
            )
            for resource in resources:
                if self.stop_event.is_set():
                    break
                if limit_bytes and usage_bytes >= limit_bytes:
                    space_limit_reached = True
                    break
                processed += 1
                directory: Optional[Path] = None
                directory_size_before = 0
                try:
                    media_type = self._media_type(resource)
                    meta, mediainfo = self._recognize(resource, media_type)
                    directory = self._base_directory(resource, mediainfo)
                    directory_size_before = directory_size(directory)
                    if media_type == MediaType.TV:
                        output_path = self._build_tv(
                            resource,
                            meta,
                            mediainfo,
                            directory=directory,
                        )
                    else:
                        output_path = self._build_movie(
                            resource,
                            meta,
                            mediainfo,
                            directory=directory,
                        )
                    self.store.update_resource_status(
                        resource["resource_id"],
                        "ready",
                        media_source=str(mediainfo.media_source or ""),
                        media_id=str(mediainfo.media_id or ""),
                        tmdb_id=str(mediainfo.tmdb_id or ""),
                        strm_path=output_path,
                    )
                    success_count += 1
                    logger.info(f"STRM 资源生成完成：{resource['title']}")
                except ShareResolutionError as error:
                    self.store.update_resource_status(
                        resource["resource_id"],
                        "share_error",
                        str(error),
                    )
                    failed_count += 1
                    logger.warning(
                        f"电视剧分享展开失败：{resource['title']} - {str(error)}"
                    )
                except LibraryBuildError as error:
                    error_status = (
                        "metadata_error"
                        if "MoviePilot" in str(error)
                        else "build_error"
                    )
                    self.store.update_resource_status(
                        resource["resource_id"],
                        error_status,
                        str(error),
                    )
                    failed_count += 1
                    logger.warning(
                        f"STRM 资源生成失败：{resource['title']} - {str(error)}"
                    )
                except Exception as error:
                    self.store.update_resource_status(
                        resource["resource_id"],
                        "build_error",
                        str(error),
                    )
                    failed_count += 1
                    logger.error(
                        f"STRM 资源生成异常：{resource['title']} - {str(error)}",
                        exc_info=True,
                    )
                finally:
                    if directory:
                        usage_bytes += max(
                            directory_size(directory) - directory_size_before,
                            0,
                        )
                if limit_bytes and usage_bytes >= limit_bytes:
                    space_limit_reached = True
                    break
            if self.stop_event.is_set():
                status = "interrupted"
                message = "媒体库生成已停止，剩余资源保留为 pending"
            elif space_limit_reached:
                status = "space_limit"
                message = "已达到输出空间上限，剩余资源保留为 pending"
            else:
                status = "completed"
                message = "媒体库生成批次已结束"
            return {
                "status": status,
                "message": message,
                "processed": processed,
                "success": success_count,
                "failed": failed_count,
                "usage_bytes": usage_bytes,
                "limit_bytes": limit_bytes,
            }
        finally:
            self._run_lock.release()
