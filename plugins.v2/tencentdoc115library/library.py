import os
import filecmp
import re
import shutil
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from inspect import signature
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from app.chain.media import MediaChain
from app.schemas.file import FileItem
from app.schemas.types import MediaType

try:
    from app.chain.scraping import ScrapingChain
except ImportError:
    ScrapingChain = MediaChain

try:
    from app.sdk.logging import logger
    from app.sdk.media import MetaInfo
except ImportError:
    from app.core.metainfo import MetaInfo
    from app.log import logger

from .resolver import ShareResolutionError, ShareResolver
from .source_link import is_offline_link, offline_file_hint
from .storage_limit import configured_limit_bytes, directory_size
from .store import CatalogStore

INVALID_PATH_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_PATH_SEGMENT_BYTES = 180
TV_TYPE_KEYWORDS = ("电视剧", "剧集", "连续剧", "tv", "番剧")
MOVIE_TYPE_KEYWORDS = ("电影", "movie", "影片")
GENERATED_METADATA_SUFFIXES = {
    ".strm",
    ".nfo",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".avif",
    ".bmp",
    ".gif",
    ".tbn",
    ".tmp",
}
CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
SEASON_PATTERNS = (
    re.compile(
        r"(?<![A-Za-z0-9])S(?P<season>\d{1,2})"
        r"(?:[ ._-]*E(?P<episode>\d{1,4}))?(?!\d)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z])Season[ ._-]*(?P<season>\d{1,2})(?!\d)",
        re.IGNORECASE,
    ),
    re.compile(r"第\s*(?P<season>[零〇一二三四五六七八九十百两\d]+)\s*季"),
)
EPISODE_PATTERNS = (
    re.compile(
        r"(?<![A-Za-z0-9])(?:E|EP|Episode)[ ._-]*(?P<episode>\d{1,4})(?!\d)",
        re.IGNORECASE,
    ),
    re.compile(r"第\s*(?P<episode>[零〇一二三四五六七八九十百两\d]+)\s*[集话]"),
)
DISC_EPISODE_PATTERNS = (
    re.compile(
        r"(?<![A-Za-z0-9])(?:D(?:ISC|ISK)?|CD|DVD|BD)"
        r"[ ._-]*(?P<episode>\d{1,3})(?!\d)",
        re.IGNORECASE,
    ),
    re.compile(r"第?\s*(?P<episode>\d{1,3})\s*[碟盘](?![A-Za-z0-9])"),
)


def chinese_number(value: str) -> Optional[int]:
    """把常见的中文集数、季数转换为整数。"""
    value = str(value or "").strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    if "百" in value:
        left, right = value.split("百", 1)
        hundreds = CHINESE_DIGITS.get(left, 1) if left else 1
        remainder = chinese_number(right) if right else 0
        return hundreds * 100 + (remainder or 0)
    if "十" in value:
        left, right = value.split("十", 1)
        tens = CHINESE_DIGITS.get(left, 1) if left else 1
        units = CHINESE_DIGITS.get(right, 0) if right else 0
        return tens * 10 + units
    digits = [CHINESE_DIGITS.get(character) for character in value]
    if any(digit is None for digit in digits):
        return None
    return int("".join(str(digit) for digit in digits))


def truncate_utf8(value: str, max_bytes: int) -> str:
    """按 UTF-8 字节安全截断，不留下半个多字节字符。"""
    encoded = str(value or "").encode("utf-8")
    if len(encoded) <= max_bytes:
        return str(value or "")
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .")


def safe_path_segment(value: str, fallback: str = "未命名") -> str:
    """
    把外部标题转换为安全的单个路径片段

    :param value (str): 原始标题
    :param fallback (str): 清理后为空时的替代名称

    :return str: 安全路径片段
    """
    result = INVALID_PATH_CHARACTERS.sub(" ", str(value or ""))
    result = re.sub(r"\s+", " ", result).strip(" .")
    result = truncate_utf8(result, MAX_PATH_SEGMENT_BYTES)
    if result:
        return result
    fallback_value = INVALID_PATH_CHARACTERS.sub(" ", str(fallback or ""))
    fallback_value = re.sub(r"\s+", " ", fallback_value).strip(" .")
    return truncate_utf8(fallback_value, MAX_PATH_SEGMENT_BYTES)


def strm_file_name(stem: str) -> str:
    """生成不会超过常见 NAS 单路径片段限制的 STRM 文件名。"""
    return f"{safe_path_segment(stem)}.strm"


def natural_path_key(value: str) -> Tuple[Tuple[int, Any], ...]:
    """按路径中的文字和数字自然排序，保证跨平台结果稳定。"""
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in re.split(r"(\d+)", str(value or ""))
    )


class LibraryBuildError(RuntimeError):
    """媒体识别、STRM 生成或本地刮削错误"""


class MediaNotRecognizedError(LibraryBuildError):
    """MoviePilot 未识别媒体；调用方可降级为仅生成 STRM。"""


class LibraryBuilder:
    """
    把目录记录限量生成为本地 STRM 和 MoviePilot 元数据

    115分享会匿名展开；磁力和ED2K只生成占位STRM，首次播放才离线下载
    """

    def __init__(
        self,
        store: CatalogStore,
        resolver: ShareResolver,
        config_provider: Callable[[], Dict[str, Any]],
        stop_event: Event,
        pause_event: Optional[Event] = None,
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
        self.pause_event = pause_event or Event()
        self._run_lock = Lock()
        self._metadata_lock = Lock()
        self._metadata_cache: Dict[Tuple[str, str], Path] = {}
        self._metadata_inflight: Dict[Tuple[str, str], Event] = {}

    @staticmethod
    def _effective_media_type(resource: Dict[str, Any]) -> str:
        save_mode = str(resource.get("save_media_mode") or "").strip().lower()
        if save_mode == "tv":
            return "电视剧"
        if save_mode == "movie":
            return "电影"
        return str(
            resource.get("detected_media_type")
            or resource.get("media_type")
            or ""
        )

    @staticmethod
    def _effective_group_name(resource: Dict[str, Any]) -> str:
        return str(
            resource.get("detected_group_name")
            or resource.get("save_group_name")
            or resource.get("group_name")
            or ""
        )

    def _is_mixed_resource(self, resource: Dict[str, Any]) -> bool:
        save_mode = str(resource.get("save_media_mode") or "").strip().lower()
        if save_mode:
            return save_mode == "mixed"
        sheet = self.store.get_sheet(str(resource.get("sheet_id") or "")) or {}
        return str(sheet.get("media_mode") or "").strip().lower() == "mixed"

    def _media_type(
        self,
        resource: Dict[str, Any],
        source_files: Optional[List[Dict[str, Any]]] = None,
    ) -> MediaType:
        save_mode = str(resource.get("save_media_mode") or "").strip().lower()
        if save_mode == "tv":
            return MediaType.TV
        if save_mode == "movie":
            return MediaType.MOVIE
        detected_type = str(resource.get("detected_media_type") or "").lower()
        if detected_type:
            if any(keyword in detected_type for keyword in TV_TYPE_KEYWORDS):
                return MediaType.TV
            if any(keyword in detected_type for keyword in MOVIE_TYPE_KEYWORDS):
                return MediaType.MOVIE
        raw_type = str(resource.get("media_type") or "").lower()
        group_name = str(resource.get("group_name") or "").lower()
        is_mixed = self._is_mixed_resource(resource)
        # 混合表中的类型标签只作提示。文件名/目录出现季集特征时优先按剧集处理。
        if is_mixed and source_files and any(
            self._looks_like_tv_path(
                str(source_file.get("file_path") or source_file.get("file_name") or "")
            )
            for source_file in source_files
        ):
            return MediaType.TV
        if save_mode == "mixed":
            return MediaType.MOVIE
        if any(keyword in raw_type for keyword in TV_TYPE_KEYWORDS):
            return MediaType.TV
        if any(keyword in raw_type for keyword in MOVIE_TYPE_KEYWORDS):
            return MediaType.MOVIE
        if any(keyword in group_name for keyword in TV_TYPE_KEYWORDS):
            return MediaType.TV
        if source_files and any(
            LibraryBuilder._looks_like_tv_path(
                str(source_file.get("file_path") or source_file.get("file_name") or "")
            )
            for source_file in source_files
        ):
            return MediaType.TV
        return MediaType.MOVIE

    def _save_detected_type(
        self,
        resource: Dict[str, Any],
        media_type: MediaType,
    ) -> None:
        """持久化混合表的最终类型，并把剧集稳定路由到“分组-剧集”。"""
        if not self._is_mixed_resource(resource):
            return
        sheet = self.store.get_sheet(str(resource.get("sheet_id") or "")) or {}
        base_group = str(
            resource.get("save_group_name")
            or sheet.get("group_name")
            or resource.get("group_name")
            or "未分组"
        )
        if base_group.endswith("-剧集"):
            base_group = base_group[:-3]
        detected_group = (
            f"{base_group}-剧集" if media_type == MediaType.TV else base_group
        )
        detected_type = getattr(media_type, "value", str(media_type))
        resource["detected_media_type"] = detected_type
        resource["detected_group_name"] = detected_group
        self.store.update_resource_status(
            str(resource.get("resource_id") or ""),
            "processing",
            detected_media_type=detected_type,
            detected_group_name=detected_group,
        )

    @staticmethod
    def _looks_like_tv_path(value: str) -> bool:
        """判断完整分享路径是否包含季、集标记。"""
        return any(pattern.search(value) for pattern in SEASON_PATTERNS) or any(
            pattern.search(value) for pattern in EPISODE_PATTERNS
        )

    @staticmethod
    def _recognize(
        resource: Dict[str, Any],
        media_type: MediaType,
        allow_type_correction: bool = False,
    ) -> Tuple[Any, Any]:
        title = str(resource.get("title") or "").strip()
        version = str(resource.get("version") or "").strip()
        meta = MetaInfo(title=title, subtitle=version or None)
        meta.year = str(resource.get("year") or "").strip() or None
        meta.type = media_type
        recognize_method = MediaChain().recognize_by_meta
        recognize_kwargs: Dict[str, Any] = {
            "metainfo": meta,
            "obtain_images": True,
        }
        try:
            if (
                not allow_type_correction
                and "mtype" in signature(recognize_method).parameters
            ):
                recognize_kwargs["mtype"] = media_type
        except (TypeError, ValueError):
            pass
        mediainfo = recognize_method(**recognize_kwargs)
        if not mediainfo:
            raise MediaNotRecognizedError(
                f"MoviePilot 未识别到媒体：{title}"
                + (f" ({meta.year})" if meta.year else "")
            )
        if mediainfo.type not in {MediaType.MOVIE, MediaType.TV}:
            raise LibraryBuildError(
                f"MoviePilot 返回了不支持的媒体类型：{mediainfo.type}"
            )
        if mediainfo.type != media_type and not allow_type_correction:
            expected_type = getattr(media_type, "value", str(media_type))
            actual_type = getattr(mediainfo.type, "value", str(mediainfo.type))
            raise LibraryBuildError(
                f"MoviePilot 媒体类型不匹配：要求 {expected_type}，"
                f"实际返回 {actual_type}；已拒绝写入错误元数据"
            )
        if mediainfo.type != media_type:
            meta.type = mediainfo.type
        return meta, mediainfo

    @staticmethod
    def _fallback_media(
        resource: Dict[str, Any], media_type: MediaType
    ) -> Tuple[Any, Any]:
        """构造仅用于目录和 STRM 命名的最小媒体信息，不伪造元数据 ID。"""
        title = str(resource.get("title") or "未命名").strip()
        year = str(resource.get("year") or "").strip()
        meta = SimpleNamespace(title=title, year=year or None, type=media_type)
        mediainfo = SimpleNamespace(
            title=title,
            year=year or None,
            type=media_type,
            media_source="",
            source="",
            media_id="",
            tmdb_id="",
            imdb_id="",
        )
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
        group = safe_path_segment(self._effective_group_name(resource) or "未分组")
        media_directory = self._media_directory_name(resource, mediainfo)
        target_parent = root / group
        if config.get("separate_source_folders") and not str(
            resource.get("sheet_id") or ""
        ).startswith("manual:"):
            sheet = self.store.get_sheet(str(resource.get("sheet_id") or "")) or {}
            source_name = safe_path_segment(
                str(sheet.get("title") or sheet.get("source_title") or "工作表")
            )
            target_parent = target_parent / source_name
        target = (target_parent / media_directory).resolve()
        if root != target and root not in target.parents:
            raise LibraryBuildError("生成路径超出 STRM 输出目录")
        target.mkdir(parents=True, exist_ok=True)
        return target

    @staticmethod
    def _metadata_key(resource: Dict[str, Any], mediainfo: Any) -> Tuple[str, str]:
        """生成跨工作表复用元数据的稳定媒体键。"""
        media_type = str(
            getattr(mediainfo, "type", None)
            or LibraryBuilder._effective_media_type(resource)
            or ""
        )
        identity = str(
            getattr(mediainfo, "tmdb_id", None)
            or getattr(mediainfo, "media_id", None)
            or ""
        ).strip()
        if not identity:
            title = str(getattr(mediainfo, "title", None) or resource.get("title") or "")
            year = str(getattr(mediainfo, "year", None) or resource.get("year") or "")
            identity = f"{title.strip().casefold()}::{year.strip()}"
        return media_type, identity

    @staticmethod
    def _metadata_directory_from_record(record: Dict[str, Any]) -> Optional[Path]:
        raw_path = str(record.get("strm_path") or "").strip()
        if not raw_path:
            return None
        path = Path(raw_path)
        # 电影记录保存的是单个 STRM 文件，电视剧记录保存的是剧集目录。
        if path.suffix.lower() == ".strm":
            path = path.parent
        return path if path.exists() and path.is_dir() else None

    def _metadata_source(
        self,
        resource: Dict[str, Any],
        mediainfo: Any,
        target_directory: Path,
    ) -> Optional[Path]:
        key = self._metadata_key(resource, mediainfo)
        with self._metadata_lock:
            cached = self._metadata_cache.get(key)
        if cached and cached.exists() and cached.resolve() != target_directory.resolve():
            return cached
        record = self.store.find_metadata_source(
            media_id=str(getattr(mediainfo, "media_id", None) or ""),
            tmdb_id=str(getattr(mediainfo, "tmdb_id", None) or ""),
            media_type=self._effective_media_type(resource),
            title=str(getattr(mediainfo, "title", None) or resource.get("title") or ""),
            year=str(getattr(mediainfo, "year", None) or resource.get("year") or ""),
            exclude_resource_id=str(resource.get("resource_id") or ""),
        )
        source = self._metadata_directory_from_record(record or {})
        if source and source.resolve() != target_directory.resolve():
            with self._metadata_lock:
                self._metadata_cache[key] = source
            return source
        return None

    @staticmethod
    def _copy_metadata(source: Path, target: Path) -> bool:
        """复制或硬链接 NFO、图片等元数据，避免重复调用刮削服务。"""
        copied = False
        metadata_suffixes = GENERATED_METADATA_SUFFIXES - {".strm", ".tmp"}
        for path in source.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in metadata_suffixes:
                continue
            relative = path.relative_to(source)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                continue
            try:
                os.link(path, destination)
            except OSError:
                shutil.copy2(path, destination)
            copied = True
        return copied

    def _remember_metadata(
        self,
        resource: Dict[str, Any],
        mediainfo: Any,
        directory: Path,
    ) -> None:
        with self._metadata_lock:
            self._metadata_cache[self._metadata_key(resource, mediainfo)] = directory

    def _scrape_with_reuse(
        self,
        resource: Dict[str, Any],
        directory: Path,
        meta: Any,
        mediainfo: Any,
    ) -> bool:
        """刮削前复用已完成结果，并协调并行任务避免同片重复刮削。"""
        key = self._metadata_key(resource, mediainfo)
        metadata_suffixes = GENERATED_METADATA_SUFFIXES - {".strm", ".tmp"}
        if directory.exists() and any(
            path.is_file() and path.suffix.lower() in metadata_suffixes
            for path in directory.rglob("*")
        ):
            self._remember_metadata(resource, mediainfo, directory)
            return True
        while True:
            source = self._metadata_source(resource, mediainfo, directory)
            if source and self._copy_metadata(source, directory):
                self._remember_metadata(resource, mediainfo, directory)
                return True
            with self._metadata_lock:
                event = self._metadata_inflight.get(key)
                if event is None:
                    event = Event()
                    self._metadata_inflight[key] = event
                    owner = True
                else:
                    owner = False
            if owner:
                break
            # 同一个媒体已有任务正在刮削，等待它写完后再建立本目录的
            # 硬链接/副本；若对方失败，下一轮会成为新的执行者重试。
            event.wait()
        try:
            self._scrape(directory, meta, mediainfo)
            self._remember_metadata(resource, mediainfo, directory)
            return False
        finally:
            with self._metadata_lock:
                if self._metadata_inflight.get(key) is event:
                    self._metadata_inflight.pop(key, None)
            event.set()

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

    def clear_generated_output(self) -> Dict[str, int]:
        """
        清除输出目录内的 STRM、NFO、图片和临时文件并保留其它文件

        :return Dict: 删除文件数、保留文件数和释放字节数

        :raises LibraryBuildError: 输出目录过宽或是符号链接时拒绝清空
        """
        raw_root = str(self.config_provider().get("output_root") or "").strip()
        if not raw_root:
            raise LibraryBuildError("未配置 STRM 输出目录")
        configured_root = Path(raw_root).expanduser()
        if configured_root.is_symlink():
            raise LibraryBuildError("输出目录是符号链接，为避免误删已拒绝清空")
        root = configured_root.resolve()
        forbidden_paths = {
            Path("/"),
            Path("/config"),
            Path("/data"),
            Path("/media"),
            Path("/mnt"),
            Path.home().resolve(),
        }
        if root in forbidden_paths or len(root.parts) < 3:
            raise LibraryBuildError(f"输出目录范围过大，已拒绝清空：{root}")
        if not root.exists():
            return {"deleted_files": 0, "retained_files": 0, "freed_bytes": 0}
        if not root.is_dir():
            raise LibraryBuildError(f"输出路径不是目录：{root}")
        deleted_files = 0
        retained_files = 0
        freed_bytes = 0
        directories: List[Path] = []
        for path in root.rglob("*"):
            if path.is_symlink():
                retained_files += 1
                continue
            if path.is_dir():
                directories.append(path)
                continue
            if path.suffix.lower() not in GENERATED_METADATA_SUFFIXES:
                retained_files += 1
                continue
            try:
                freed_bytes += path.stat().st_size
            except OSError:
                pass
            path.unlink()
            deleted_files += 1
        for directory in sorted(
            directories,
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        return {
            "deleted_files": deleted_files,
            "retained_files": retained_files,
            "freed_bytes": freed_bytes,
        }

    @staticmethod
    def _merge_move_directory(source: Path, target: Path) -> None:
        """移动媒体目录；目标已存在时安全合并，避免覆盖冲突文件。"""
        if source == target:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.move(str(source), str(target))
            return
        if not target.is_dir() or target.is_symlink():
            raise LibraryBuildError(f"迁移目标不是目录：{target}")
        for child in source.iterdir():
            if child.is_symlink():
                raise LibraryBuildError(f"源目录包含符号链接，已停止迁移：{child}")
            destination = target / child.name
            if child.is_dir():
                if destination.exists():
                    LibraryBuilder._merge_move_directory(child, destination)
                else:
                    shutil.move(str(child), str(destination))
                continue
            if destination.exists():
                if destination.is_symlink() or not destination.is_file():
                    raise LibraryBuildError(f"迁移目标存在冲突：{destination}")
                try:
                    identical = os.path.samefile(child, destination)
                except OSError:
                    identical = filecmp.cmp(child, destination, shallow=False)
                if not identical:
                    raise LibraryBuildError(f"迁移目标存在同名不同文件：{destination}")
                child.unlink()
            else:
                shutil.move(str(child), str(destination))
        try:
            source.rmdir()
        except OSError as error:
            raise LibraryBuildError(f"源目录未能完全迁移：{source}") from error

    @staticmethod
    def _move_generated_file(source: Path, target: Path) -> None:
        """安全移动单个生成文件；相同目标存在时只删除重复源文件。"""
        if source == target:
            return
        if source.is_symlink():
            raise LibraryBuildError(f"源文件是符号链接，已停止迁移：{source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise LibraryBuildError(f"迁移目标存在冲突：{target}")
            try:
                identical = os.path.samefile(source, target)
            except OSError:
                identical = filecmp.cmp(source, target, shallow=False)
            if not identical:
                raise LibraryBuildError(f"迁移目标存在同名不同文件：{target}")
            source.unlink()
            return
        shutil.move(str(source), str(target))

    def migrate_existing_output(self) -> Dict[str, Any]:
        """按当前工作表分目录设置搬迁已有 STRM 和元数据，不重新刮削。"""
        config = self.config_provider()
        if not config.get("separate_source_folders"):
            return {
                "status": "configuration_error",
                "message": "请先开启“按工作表分开文件夹”并保存配置",
                "moved": 0,
                "updated": 0,
                "skipped": 0,
                "failed": 0,
            }
        raw_root = str(config.get("output_root") or "").strip()
        if not raw_root:
            return {
                "status": "configuration_error",
                "message": "未配置 STRM 输出目录",
                "moved": 0,
                "updated": 0,
                "skipped": 0,
                "failed": 0,
            }
        configured_root = Path(raw_root).expanduser()
        if configured_root.is_symlink():
            return {
                "status": "configuration_error",
                "message": "输出目录是符号链接，为避免误搬迁已拒绝执行",
                "moved": 0,
                "updated": 0,
                "skipped": 0,
                "failed": 0,
            }
        root = configured_root.resolve()
        if root in {Path("/"), Path("/config"), Path("/data"), Path("/media"), Path("/mnt")} or len(root.parts) < 3:
            return {
                "status": "configuration_error",
                "message": f"输出目录范围过大，已拒绝迁移：{root}",
                "moved": 0,
                "updated": 0,
                "skipped": 0,
                "failed": 0,
            }
        if not root.exists():
            return {
                "status": "completed",
                "message": "输出目录不存在，没有需要迁移的内容",
                "moved": 0,
                "updated": 0,
                "skipped": 0,
                "failed": 0,
            }
        if not root.is_dir():
            return {
                "status": "configuration_error",
                "message": f"输出路径不是目录：{root}",
                "moved": 0,
                "updated": 0,
                "skipped": 0,
                "failed": 0,
            }

        migration_items: List[Dict[str, Any]] = []
        source_targets: Dict[Path, set[Path]] = {}
        for resource in self.store.list_migration_resources():
            raw_strm_path = str(resource.get("strm_path") or "").strip()
            if not raw_strm_path:
                continue
            recorded_path = Path(raw_strm_path).expanduser()
            source_directory = (
                recorded_path.parent
                if recorded_path.suffix.lower() == ".strm"
                else recorded_path
            ).resolve()
            sheet = self.store.get_sheet(str(resource.get("sheet_id") or "")) or {}
            source_name = safe_path_segment(
                str(sheet.get("title") or sheet.get("source_title") or "工作表")
            )
            group_name = safe_path_segment(
                self._effective_group_name(resource) or "未分组"
            )
            media_name = safe_path_segment(source_directory.name)
            target_directory = (root / group_name / source_name / media_name).resolve()
            migration_items.append(
                {
                    "resource": resource,
                    "recorded_path": recorded_path,
                    "source_directory": source_directory,
                    "target_directory": target_directory,
                }
            )
            source_targets.setdefault(source_directory, set()).add(target_directory)

        moved = 0
        updated = 0
        skipped = 0
        failed = 0
        moved_directories: set[Tuple[Path, Path]] = set()
        for item in migration_items:
            resource = item["resource"]
            if self.stop_event.is_set():
                return {
                    "status": "interrupted",
                    "message": "迁移已停止，已完成的路径更新会保留",
                    "moved": moved,
                    "updated": updated,
                    "skipped": skipped,
                    "failed": failed,
                }
            if self.pause_event.is_set():
                return {
                    "status": "paused",
                    "message": "迁移已暂停，点击恢复可继续",
                    "moved": moved,
                    "updated": updated,
                    "skipped": skipped,
                    "failed": failed,
                }
            recorded_path = item["recorded_path"]
            source_directory = item["source_directory"]
            target_directory = item["target_directory"]
            if source_directory != root and root not in source_directory.parents:
                failed += 1
                logger.warning(
                    f"跳过输出目录外路径迁移：{resource.get('title')} - {source_directory}"
                )
                continue
            if target_directory != root and root not in target_directory.parents:
                failed += 1
                continue
            if source_directory != target_directory and source_directory in target_directory.parents:
                failed += 1
                logger.warning(
                    f"迁移目标位于源目录内，已跳过：{resource.get('title')}"
                )
                continue
            directory_key = (source_directory, target_directory)
            try:
                resource_files = self.store.list_resource_files(
                    str(resource.get("resource_id") or "")
                )
                try:
                    relative_strm = recorded_path.resolve().relative_to(source_directory)
                except ValueError as error:
                    raise LibraryBuildError(
                        f"STRM 路径不在媒体目录内：{recorded_path}"
                    ) from error
                relative_file_paths: Dict[str, Path] = {}
                for file_item in resource_files:
                    raw_file_path = str(file_item.get("strm_path") or "").strip()
                    if not raw_file_path:
                        continue
                    file_path = Path(raw_file_path).expanduser()
                    try:
                        relative_file_paths[str(file_item.get("file_id") or "")] = (
                            file_path.resolve().relative_to(source_directory)
                        )
                    except ValueError:
                        continue

                if source_directory == target_directory:
                    skipped += 1
                elif len(source_targets.get(source_directory, set())) == 1:
                    if directory_key not in moved_directories:
                        if source_directory.exists():
                            self._merge_move_directory(source_directory, target_directory)
                            moved += 1
                        elif not target_directory.exists():
                            raise LibraryBuildError(
                                f"源目录不存在：{source_directory}"
                            )
                        moved_directories.add(directory_key)
                    elif not target_directory.exists():
                        raise LibraryBuildError(f"迁移目标不存在：{target_directory}")
                else:
                    # 旧布局中多个工作表可能共用同一媒体目录。此时不能把
                    # 整个目录交给第一条记录：各自移动 STRM，并为每个目标
                    # 建立元数据硬链接/副本。
                    target_directory.mkdir(parents=True, exist_ok=True)
                    if source_directory.exists():
                        self._copy_metadata(source_directory, target_directory)
                    candidate_paths = set(relative_file_paths.values())
                    if recorded_path.suffix.lower() == ".strm":
                        candidate_paths.add(relative_strm)
                    for relative_path in candidate_paths:
                        source_file = source_directory / relative_path
                        target_file = target_directory / relative_path
                        if source_file.exists():
                            self._move_generated_file(source_file, target_file)
                        elif not target_file.exists():
                            raise LibraryBuildError(f"STRM 文件不存在：{source_file}")
                    moved += 1

                new_strm_path = (
                    target_directory / relative_strm
                    if recorded_path.suffix.lower() == ".strm"
                    else target_directory
                )
                file_paths = {
                    file_id: str(target_directory / relative_path)
                    for file_id, relative_path in relative_file_paths.items()
                }
                self.store.update_resource_paths(
                    str(resource.get("resource_id") or ""),
                    str(new_strm_path),
                    file_paths,
                )
                updated += 1
            except Exception as error:
                failed += 1
                logger.warning(
                    f"迁移资源失败：{resource.get('title')} - {str(error)}"
                )
        status = "completed" if not failed else "completed_with_errors"
        return {
            "status": status,
            "message": (
                f"迁移完成：{moved} 个目录、更新 {updated} 条路径，"
                f"跳过 {skipped} 条，失败 {failed} 条"
            ),
            "moved": moved,
            "updated": updated,
            "skipped": skipped,
            "failed": failed,
        }

    def _play_url(
        self,
        resource_id: str,
        file_id: Optional[str] = None,
        file_name: str = "",
    ) -> str:
        public_base_url = str(
            self.config_provider().get("public_base_url") or ""
        ).strip()
        if not public_base_url:
            raise LibraryBuildError("未配置播放器可访问的 MoviePilot 地址")
        url = (
            f"{public_base_url.rstrip('/')}/api/v1/plugin/"
            f"TencentDoc115Library/play/{resource_id}"
        )
        name_hint = Path(str(file_name or "")).name
        if name_hint:
            url += "/" + quote(name_hint, safe="")
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
        result = ScrapingChain().scrape_metadata(
            fileitem=self._file_item(directory),
            meta=meta,
            mediainfo=mediainfo,
            init_folder=True,
            overwrite=False,
            recursive=True,
        )
        if isinstance(result, tuple):
            success, message = result
        else:
            success, message = result is not False, ""
        if not success:
            raise LibraryBuildError(f"MoviePilot 刮削失败：{message}")

    def _build_movie(
        self,
        resource: Dict[str, Any],
        meta: Any,
        mediainfo: Any,
        directory: Optional[Path] = None,
        source_files: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        if source_files is None:
            source_files = self.resolver.list_video_files(resource["share_url"])
        selected_file = self.resolver.choose_movie_file(source_files)
        directory = directory or self._base_directory(resource, mediainfo)
        base_name = self._media_directory_name(resource, mediainfo)
        version = safe_path_segment(str(resource.get("version") or ""), "")
        filename = strm_file_name(
            f"{base_name} - {version}" if version else base_name
        )
        strm_path = directory / filename
        self._write_strm(
            strm_path,
            self._play_url(
                resource["resource_id"],
                selected_file["file_id"],
                selected_file["file_name"],
            ),
        )
        self.store.replace_resource_files(
            resource["resource_id"],
            [{**selected_file, "strm_path": str(strm_path)}],
        )
        return str(strm_path)

    @staticmethod
    def _episode_identity(
        file_name: str,
        file_path: str = "",
    ) -> Tuple[Optional[int], Optional[int]]:
        meta = MetaInfo(file_name)
        season = meta.begin_season
        episode = meta.begin_episode
        identity_text = str(file_path or file_name)
        for pattern in SEASON_PATTERNS:
            match = pattern.search(identity_text)
            if not match:
                continue
            parsed_season = chinese_number(match.group("season"))
            season = parsed_season or season
            if match.groupdict().get("episode"):
                episode = int(match.group("episode"))
            break
        if episode is None:
            for pattern in EPISODE_PATTERNS:
                match = pattern.search(identity_text)
                if match:
                    episode = chinese_number(match.group("episode"))
                    break
        if episode is None and Path(file_name).suffix.lower() == ".iso":
            for pattern in DISC_EPISODE_PATTERNS:
                match = pattern.search(identity_text)
                if match:
                    episode = int(match.group("episode"))
                    break
            if episode is None:
                bare_disc = re.fullmatch(r"0*(\d{1,3})", Path(file_name).stem)
                if bare_disc:
                    episode = int(bare_disc.group(1))
        if episode is None and season is not None:
            bare_episode = re.fullmatch(
                r"(?:EP?|Episode)?[ ._-]*(\d{1,4})",
                Path(file_name).stem,
                re.IGNORECASE,
            )
            if bare_episode:
                episode = int(bare_episode.group(1))
        season_only_file = any(pattern.search(file_name) for pattern in SEASON_PATTERNS)
        if (
            episode is None
            and season is not None
            and (season_only_file or Path(file_name).suffix.lower() == ".iso")
        ):
            episode = 1
        return season or (1 if episode is not None else None), episode

    def _build_tv(
        self,
        resource: Dict[str, Any],
        meta: Any,
        mediainfo: Any,
        directory: Optional[Path] = None,
        source_files: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        directory = directory or self._base_directory(resource, mediainfo)
        if source_files is None:
            source_files = self.resolver.list_video_files(resource["share_url"])
        expanded_files: List[Dict[str, Any]] = []
        identified_files = []
        media_name = self._media_directory_name(resource, mediainfo)
        for source_file in source_files:
            season, episode = self._episode_identity(
                source_file["file_name"],
                str(source_file.get("file_path") or ""),
            )
            identified_files.append((source_file, season, episode))
        if not any(episode is not None for _, _, episode in identified_files):
            fallback_season = int(getattr(meta, "begin_season", None) or 1)
            identified_files = [
                (source_file, fallback_season, index)
                for index, source_file in enumerate(
                    sorted(
                        source_files,
                        key=lambda item: natural_path_key(
                            str(item.get("file_path") or item.get("file_name") or "")
                        ),
                    ),
                    start=1,
                )
            ]
        unrecognized_files = []
        for source_file, season, episode in identified_files:
            if episode is None:
                unrecognized_files.append(source_file["file_name"])
                continue
            season_directory = directory / f"Season {season:02d}"
            strm_path = season_directory / strm_file_name(
                f"{media_name} - S{season:02d}E{episode:02d}"
            )
            if any(item.get("strm_path") == str(strm_path) for item in expanded_files):
                suffix = safe_path_segment(Path(source_file["file_name"]).stem)
                strm_path = season_directory / strm_file_name(
                    f"{media_name} - S{season:02d}E{episode:02d} - {suffix}"
                )
            self._write_strm(
                strm_path,
                self._play_url(
                    resource["resource_id"],
                    source_file["file_id"],
                    source_file["file_name"],
                ),
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
        return str(directory)

    def build(
        self,
        limit: Optional[int] = None,
        retry_failed: bool = False,
        known_usage_bytes: Optional[int] = None,
        resource_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        限量处理待生成资源

        :param limit (int): 本次最大资源数
        :param retry_failed (bool): 是否包含失败资源
        :param known_usage_bytes (int): 连续批次沿用的已知目录占用，避免反复扫描
        :param resource_ids (List[str]): 只处理指定资源，供手动导入立即构建

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
        scrape_workers = 1
        scrape_executor: Optional[ThreadPoolExecutor] = None
        pending_scrapes: Dict[
            Future,
            Tuple[Dict[str, Any], Path, str, Any],
        ] = {}

        def finish_scrapes(done: Any) -> None:
            """收集并发刮削结果，逐条写回可恢复状态。"""
            nonlocal success_count, failed_count
            for future in done:
                resource, directory, output_path, mediainfo = pending_scrapes.pop(
                    future
                )
                try:
                    future.result()
                except Exception as error:
                    failed_count += 1
                    self.store.update_resource_status(
                        resource["resource_id"],
                        "metadata_error",
                        str(error),
                        strm_status="ready",
                        scrape_status="failed",
                        strm_path=output_path,
                    )
                    logger.warning(
                        f"并行刮削失败：{resource['title']} - {str(error)}"
                    )
                    continue
                self.store.update_resource_status(
                    resource["resource_id"],
                    "ready",
                    strm_status="ready",
                    scrape_status="ready",
                    media_source=str(
                        getattr(mediainfo, "media_source", None)
                        or getattr(mediainfo, "source", None)
                        or ""
                    ),
                    media_id=str(getattr(mediainfo, "media_id", None) or ""),
                    tmdb_id=str(getattr(mediainfo, "tmdb_id", None) or ""),
                    imdb_id=str(getattr(mediainfo, "imdb_id", None) or ""),
                    strm_path=output_path,
                )
                self._remember_metadata(resource, mediainfo, directory)
                success_count += 1
                logger.info(f"STRM 资源生成完成：{resource['title']}")
        try:
            config = self.config_provider()
            scrape_enabled = bool(config.get("scrape_metadata", True))
            try:
                scrape_workers = min(
                    max(int(config.get("scrape_workers") or 1), 1),
                    8,
                )
            except (TypeError, ValueError):
                scrape_workers = 1
            if scrape_enabled and scrape_workers > 1:
                scrape_executor = ThreadPoolExecutor(
                    max_workers=scrape_workers,
                    thread_name_prefix="TencentDoc115Scrape",
                )
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
                resource_ids=resource_ids,
            )
            for resource in resources:
                if scrape_executor and len(pending_scrapes) >= scrape_workers:
                    done, _ = wait(
                        pending_scrapes,
                        return_when=FIRST_COMPLETED,
                    )
                    finish_scrapes(done)
                if self.stop_event.is_set() or self.pause_event.is_set():
                    break
                if limit_bytes and usage_bytes >= limit_bytes:
                    space_limit_reached = True
                    break
                processed += 1
                directory: Optional[Path] = None
                directory_size_before = 0
                current_stage = "validating"
                try:
                    self.store.update_resource_status(
                        resource["resource_id"],
                        "processing",
                        strm_status="validating",
                        scrape_status="pending",
                    )
                    sheet = self.store.get_sheet(resource["sheet_id"]) or {}
                    prefetched_files = (
                        self.store.list_resource_files(resource["resource_id"])
                        if str(sheet.get("sheet_id") or "").startswith("manual:")
                        else []
                    )
                    if prefetched_files:
                        source_files = prefetched_files
                    elif is_offline_link(resource["share_url"]):
                        source_files = [
                            offline_file_hint(
                                resource["share_url"],
                                str(resource.get("title") or "未命名"),
                            )
                        ]
                        self.store.replace_resource_files(
                            resource["resource_id"], source_files
                        )
                    else:
                        source_files = self.resolver.list_video_files(
                            resource["share_url"]
                        )
                    media_type = self._media_type(resource, source_files)
                    mixed_resource = self._is_mixed_resource(resource)
                    current_stage = "recognizing"
                    self.store.update_resource_status(
                        resource["resource_id"],
                        "processing",
                        strm_status="validated",
                        scrape_status="recognizing",
                    )
                    recognition_error = ""
                    try:
                        meta, mediainfo = self._recognize(
                            resource,
                            media_type,
                            allow_type_correction=mixed_resource,
                        )
                        # MoviePilot 的识别结论优先于混合表格中不可靠的类型标签。
                        media_type = mediainfo.type
                    except MediaNotRecognizedError as error:
                        recognition_error = str(error)
                        meta, mediainfo = self._fallback_media(resource, media_type)
                    self._save_detected_type(resource, media_type)
                    current_stage = "generating"
                    self.store.update_resource_status(
                        resource["resource_id"],
                        "processing",
                        strm_status="generating",
                        scrape_status=(
                            "unrecognized" if recognition_error else "recognized"
                        ),
                    )
                    directory = self._base_directory(resource, mediainfo)
                    directory_size_before = directory_size(directory)
                    if media_type == MediaType.TV:
                        output_path = self._build_tv(
                            resource,
                            meta,
                            mediainfo,
                            directory=directory,
                            source_files=source_files,
                        )
                    else:
                        output_path = self._build_movie(
                            resource,
                            meta,
                            mediainfo,
                            directory=directory,
                            source_files=source_files,
                        )
                    deferred_scrape = False
                    metadata_reused = False
                    if scrape_enabled and not recognition_error:
                        current_stage = "scraping"
                        self.store.update_resource_status(
                            resource["resource_id"],
                            "processing",
                            strm_status="ready",
                            scrape_status="scraping",
                            strm_path=output_path,
                        )
                        if scrape_executor:
                            future = scrape_executor.submit(
                                self._scrape_with_reuse,
                                resource,
                                directory,
                                meta,
                                mediainfo,
                            )
                            pending_scrapes[future] = (
                                resource,
                                directory,
                                output_path,
                                mediainfo,
                            )
                            deferred_scrape = True
                        else:
                            metadata_reused = self._scrape_with_reuse(
                                resource,
                                directory,
                                meta,
                                mediainfo,
                            )
                            if metadata_reused:
                                logger.info(
                                    f"复用已有元数据完成：{resource['title']}"
                                )
                    if recognition_error:
                        self.store.update_resource_status(
                            resource["resource_id"],
                            "ready",
                            strm_status="ready",
                            scrape_status="unrecognized",
                            media_source="",
                            media_id="",
                            tmdb_id="",
                            imdb_id="",
                            strm_path=output_path,
                        )
                        success_count += 1
                        logger.warning(
                            f"MoviePilot 未识别，已降级生成 STRM："
                            f"{resource['title']}"
                        )
                    elif not deferred_scrape and not scrape_enabled:
                        self.store.update_resource_status(
                            resource["resource_id"],
                            "ready",
                            strm_status="ready",
                            scrape_status="skipped",
                            media_source=str(
                                getattr(mediainfo, "media_source", None)
                                or getattr(mediainfo, "source", None)
                                or ""
                            ),
                            media_id=str(getattr(mediainfo, "media_id", None) or ""),
                            tmdb_id=str(getattr(mediainfo, "tmdb_id", None) or ""),
                            imdb_id=str(getattr(mediainfo, "imdb_id", None) or ""),
                            strm_path=output_path,
                        )
                        success_count += 1
                        logger.info(f"STRM 资源生成完成：{resource['title']}")
                    elif not deferred_scrape and scrape_enabled:
                        self.store.update_resource_status(
                            resource["resource_id"],
                            "ready",
                            strm_status="ready",
                            scrape_status="ready",
                            media_source=str(
                                getattr(mediainfo, "media_source", None)
                                or getattr(mediainfo, "source", None)
                                or ""
                            ),
                            media_id=str(getattr(mediainfo, "media_id", None) or ""),
                            tmdb_id=str(getattr(mediainfo, "tmdb_id", None) or ""),
                            imdb_id=str(getattr(mediainfo, "imdb_id", None) or ""),
                            strm_path=output_path,
                        )
                        self._remember_metadata(resource, mediainfo, directory)
                        success_count += 1
                        logger.info(f"STRM 资源生成完成：{resource['title']}")
                except ShareResolutionError as error:
                    share_status = "share_error" if error.retryable else "invalid_share"
                    self.store.update_resource_status(
                        resource["resource_id"],
                        share_status,
                        str(error),
                        strm_status="failed",
                        scrape_status="blocked",
                    )
                    failed_count += 1
                    logger.warning(
                        f"115 分享校验失败：{resource['title']} - {str(error)}"
                    )
                except LibraryBuildError as error:
                    error_status = (
                        "metadata_error"
                        if current_stage in {"recognizing", "scraping"}
                        or "MoviePilot" in str(error)
                        else "build_error"
                    )
                    self.store.update_resource_status(
                        resource["resource_id"],
                        error_status,
                        str(error),
                        strm_status=(
                            "ready" if current_stage == "scraping" else "failed"
                        ),
                        scrape_status=(
                            "failed"
                            if current_stage in {"recognizing", "scraping"}
                            else "blocked"
                        ),
                    )
                    failed_count += 1
                    logger.warning(
                        f"STRM 资源生成失败：{resource['title']} - {str(error)}"
                    )
                except Exception as error:
                    error_status = (
                        "metadata_error"
                        if current_stage in {"recognizing", "scraping"}
                        else "build_error"
                    )
                    self.store.update_resource_status(
                        resource["resource_id"],
                        error_status,
                        str(error),
                        strm_status=(
                            "ready" if current_stage == "scraping" else "failed"
                        ),
                        scrape_status=(
                            "failed"
                            if current_stage in {"recognizing", "scraping"}
                            else "blocked"
                        ),
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
            while pending_scrapes:
                done, _ = wait(pending_scrapes, return_when=FIRST_COMPLETED)
                finish_scrapes(done)
            if scrape_executor:
                # 所有任务已收集，关闭线程池避免线程泄漏。
                scrape_executor.shutdown(wait=True)
                scrape_executor = None
            usage_bytes = max(usage_bytes, int(self.storage_snapshot()["usage_bytes"]))
            if self.stop_event.is_set():
                status = "interrupted"
                message = "媒体库生成已停止，剩余资源保留为 pending"
            elif self.pause_event.is_set():
                status = "paused"
                message = "媒体库生成已暂停，剩余资源保留为 pending"
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
            if scrape_executor:
                scrape_executor.shutdown(wait=True)
            self._run_lock.release()
