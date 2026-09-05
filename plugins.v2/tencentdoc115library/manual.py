import hashlib
import re
from pathlib import Path, PurePosixPath
from threading import Event
from typing import Any, Callable, Dict, List, Optional, Tuple

from .resolver import ShareResolutionError, ShareResolver
from .source_link import (
    extract_source_links,
    is_offline_link,
    offline_file_hint,
    offline_info_hash,
    offline_title_year,
)
from .store import CatalogStore


YEAR_PATTERN = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
MANUAL_SHEET_PREFIX = "manual:"


class ManualImportError(ValueError):
    """手动资源输入格式错误。"""


class ManualLibraryImporter:
    """把用户粘贴的115分享、磁力或ED2K导入目录和构建队列。"""

    def __init__(
        self,
        store: CatalogStore,
        resolver: ShareResolver,
        stop_event: Event,
        pause_event: Optional[Event] = None,
    ) -> None:
        self.store = store
        self.resolver = resolver
        self.stop_event = stop_event
        self.pause_event = pause_event or Event()

    @staticmethod
    def _sheet_id(group_name: str, media_mode: str) -> str:
        identity = f"{group_name.strip()}::{media_mode}"
        digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:20]
        return f"{MANUAL_SHEET_PREFIX}{digest}"

    @staticmethod
    def _resource_id(sheet_id: str, share_code: str) -> str:
        identity = f"{sheet_id}::{share_code.strip().lower()}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _line_title(prefix: str) -> Tuple[str, str]:
        value = str(prefix or "").strip(" \t|-—:：")
        if not value:
            return "", ""
        parts = [item.strip() for item in value.split("|") if item.strip()]
        title = parts[0] if parts else value
        year = next(
            (match.group(1) for item in parts[1:] if (match := YEAR_PATTERN.search(item))),
            "",
        )
        if not year:
            match = YEAR_PATTERN.search(title)
            year = match.group(1) if match else ""
        if year:
            title = re.sub(rf"[（(]?{re.escape(year)}[）)]?", " ", title)
        title = re.sub(r"\s+", " ", title).strip(" -—|()（）")
        return title, year

    @classmethod
    def parse_entries(cls, raw: str) -> List[Dict[str, str]]:
        """解析 URL、标题|URL 或 标题|年份|URL，多链接时保持输入顺序。"""
        entries: List[Dict[str, str]] = []
        seen = set()
        for line in str(raw or "").splitlines():
            matches = extract_source_links(line)
            for index, match in enumerate(matches):
                url = str(match["url"])
                if is_offline_link(url):
                    identity = f"{match['kind']}:{offline_info_hash(url)}"
                    share_code = identity
                else:
                    try:
                        share_code, _ = ShareResolver.parse_share_url(url)
                    except ShareResolutionError:
                        continue
                    identity = share_code.lower()
                if identity in seen:
                    continue
                seen.add(identity)
                prefix = line[: int(match["start"])] if index == 0 else ""
                title, year = cls._line_title(prefix)
                if is_offline_link(url) and not title:
                    derived = offline_title_year(url)
                    title = derived["title"]
                    year = year or derived["year"]
                entries.append(
                    {
                        "share_url": url,
                        "share_code": share_code,
                        "title": title,
                        "year": year,
                    }
                )
        return entries

    @staticmethod
    def _derived_title(files: List[Dict[str, Any]]) -> Tuple[str, str]:
        """优先使用共同顶层目录，否则使用体积最大的媒体文件名。"""
        paths = [
            tuple(part for part in PurePosixPath(str(item.get("file_path") or "")).parts if part != "/")
            for item in files
        ]
        first_parts = {parts[0] for parts in paths if parts}
        candidate = ""
        if len(first_parts) == 1 and any(len(parts) > 1 for parts in paths):
            candidate = next(iter(first_parts))
        if not candidate:
            selected = max(files, key=lambda item: int(item.get("file_size") or 0))
            candidate = Path(str(selected.get("file_name") or "未命名")).stem
        candidate = re.sub(r"[._]+", " ", candidate)
        candidate = re.sub(r"\s+", " ", candidate).strip()
        match = YEAR_PATTERN.search(candidate)
        year = match.group(1) if match else ""
        if year:
            candidate = re.sub(
                rf"[（(]?{re.escape(year)}[）)]?",
                " ",
                candidate,
            )
            candidate = re.sub(r"\s+", " ", candidate).strip(" -—|()（）")
        return candidate or "未命名", year

    def import_links(
        self,
        raw: str,
        group_name: str,
        media_mode: str,
        maximum: int = 100,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """校验并保存资源，磁力和ED2K只登记、不在导入时离线下载。"""
        group_name = str(group_name or "").strip()
        if not group_name:
            raise ManualImportError("请填写自定义资源输出文件夹")
        media_mode = str(media_mode or "mixed").strip().lower()
        if media_mode not in {"movie", "tv", "mixed"}:
            raise ManualImportError("媒体类型必须是电影、剧集或自动识别")
        entries = self.parse_entries(raw)
        if not entries:
            raise ManualImportError("没有识别到有效的115分享、磁力或ED2K链接")
        if len(entries) > maximum:
            raise ManualImportError(f"一次最多导入 {maximum} 条资源链接")

        sheet_id = self._sheet_id(group_name, media_mode)
        media_type = {
            "movie": "电影",
            "tv": "电视剧",
            "mixed": "",
        }[media_mode]
        queued_ids: List[str] = []
        imported = 0
        unchanged = 0
        failed = 0
        errors: List[str] = []
        if progress_callback:
            progress_callback(
                {
                    "phase": "resolving",
                    "total": len(entries),
                    "current": 0,
                    "current_title": "",
                    "imported": 0,
                    "unchanged": 0,
                    "failed": 0,
                }
            )
        for index, entry in enumerate(entries, start=1):
            if self.stop_event.is_set() or self.pause_event.is_set():
                break
            resource_id = self._resource_id(sheet_id, entry["share_code"])
            fallback_title = entry["title"] or f"自定义资源 {entry['share_code']}"
            if progress_callback:
                progress_callback(
                    {
                        "phase": "resolving",
                        "total": len(entries),
                        "current": index,
                        "current_title": fallback_title,
                        "imported": imported,
                        "unchanged": unchanged,
                        "failed": failed,
                    }
                )
            try:
                offline = is_offline_link(entry["share_url"])
                if offline:
                    source_files = [
                        offline_file_hint(entry["share_url"], fallback_title)
                    ]
                    derived = offline_title_year(entry["share_url"])
                    derived_title = derived["title"]
                    derived_year = derived["year"]
                else:
                    source_files = self.resolver.list_video_files(entry["share_url"])
                    derived_title, derived_year = self._derived_title(source_files)
                title = entry["title"] or derived_title
                year = entry["year"] or derived_year
                queued = self.store.upsert_manual_resource(
                    sheet_id=sheet_id,
                    resource_id=resource_id,
                    title=title,
                    year=year,
                    share_url=entry["share_url"],
                    media_type=media_type,
                    group_name=group_name,
                    media_mode=media_mode,
                    status="pending",
                )
                if queued:
                    self.store.replace_resource_files(resource_id, source_files)
                    queued_ids.append(resource_id)
                    imported += 1
                else:
                    unchanged += 1
            except ShareResolutionError as error:
                status = "share_error" if error.retryable else "invalid_share"
                self.store.upsert_manual_resource(
                    sheet_id=sheet_id,
                    resource_id=resource_id,
                    title=fallback_title,
                    year=entry["year"],
                    share_url=entry["share_url"],
                    media_type=media_type,
                    group_name=group_name,
                    media_mode=media_mode,
                    status=status,
                    error=str(error),
                )
                failed += 1
                errors.append(f"{fallback_title}：{error}")
            except Exception as error:
                message = f"解析资源链接时发生临时错误：{error}"
                self.store.upsert_manual_resource(
                    sheet_id=sheet_id,
                    resource_id=resource_id,
                    title=fallback_title,
                    year=entry["year"],
                    share_url=entry["share_url"],
                    media_type=media_type,
                    group_name=group_name,
                    media_mode=media_mode,
                    status="share_error",
                    error=message,
                )
                failed += 1
                errors.append(f"{fallback_title}：{message}")
            if progress_callback:
                progress_callback(
                    {
                        "phase": "resolving",
                        "total": len(entries),
                        "current": index,
                        "current_title": fallback_title,
                        "imported": imported,
                        "unchanged": unchanged,
                        "failed": failed,
                    }
                )
        return {
            "total": len(entries),
            "imported": imported,
            "unchanged": unchanged,
            "failed": failed,
            "queued_ids": queued_ids,
            "errors": errors[:20],
            "interrupted": self.stop_event.is_set(),
            "paused": self.pause_event.is_set(),
        }
