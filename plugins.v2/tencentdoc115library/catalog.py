import hashlib
import json
import re
from threading import Event, Lock
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

try:
    from app.sdk.logging import logger
except ImportError:
    from app.log import logger

from .client import TencentDocumentClient, TencentDocumentError, looks_like_url
from .store import CatalogStore

HEADER_ALIASES = {
    "title": {"影片名称", "电影名称", "影视名称", "名称", "片名", "标题"},
    "version": {"资源版本", "版本", "画质", "规格"},
    "share_url": {"资源链接", "分享链接", "115链接", "链接", "地址"},
    "media_type": {"类型", "资源类型", "影视类型", "分类"},
    "rating": {"豆瓣评分", "评分", "豆瓣"},
    "year": {"年份", "年代", "上映年份"},
}

DEFAULT_COLUMN_INDEX = {
    "title": 0,
    "version": 1,
    "share_url": 2,
    "media_type": 3,
    "rating": 4,
    "year": 5,
}
SHEET_MEDIA_MODES = {"movie", "tv", "mixed"}
TV_ROW_KEYWORDS = ("剧集", "电视剧", "连续剧", "tv", "番剧")


def document_sources(config: Dict[str, Any]) -> List[Dict[str, str]]:
    """解析多行腾讯文档配置，支持“别名|链接”和单独链接。"""
    raw = str(config.get("document_urls") or "").strip()
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        legacy_url = str(config.get("document_url") or "").strip()
        lines = [legacy_url] if legacy_url else []
    sources: List[Dict[str, str]] = []
    seen_urls = set()
    for index, line in enumerate(lines, start=1):
        if "|" in line:
            alias, url = (part.strip() for part in line.split("|", 1))
        else:
            alias, url = f"文档{index}", line
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        sources.append({"name": alias or f"文档{index}", "url": url})
    return sources


def namespaced_sheet_id(file_id: str, remote_sheet_id: str) -> str:
    """生成跨文档不冲突且可稳定恢复断点的内部工作表 ID。"""
    identity = f"{file_id}:{remote_sheet_id}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def sheet_config_key(sheet_id: str) -> str:
    """
    生成可用于插件配置模型的工作表键

    :param sheet_id (str): 腾讯文档工作表 ID

    :return str: 配置键
    """
    return re.sub(r"[^a-zA-Z0-9_]", "_", str(sheet_id))


def default_group_for_title(title: str) -> str:
    """
    根据用户期望生成首次发现工作表时的默认分组

    :param title (str): 工作表标题

    :return str: 默认输出分组
    """
    normalized = str(title or "").strip()
    if any(keyword in normalized for keyword in ("剧集", "电视剧", "连续剧")):
        return "剧集"
    if "星火" in normalized:
        return "星火"
    if "蚂蚁" in normalized:
        return "蚂蚁"
    return "电影合集"


def default_media_mode_for_title(title: str) -> str:
    """
    根据工作表标题选择首次发现时的媒体类型模式

    :param title (str): 工作表标题

    :return str: movie、tv 或 mixed
    """
    normalized = str(title or "").strip().lower()
    if "星火" in normalized:
        return "mixed"
    if any(keyword in normalized for keyword in TV_ROW_KEYWORDS):
        return "tv"
    return "movie"


def normalize_media_mode(value: str, sheet_title: str = "") -> str:
    """
    标准化工作表媒体类型模式

    :param value (str): 配置中的媒体类型模式
    :param sheet_title (str): 用于缺省推断的工作表标题

    :return str: movie、tv 或 mixed
    """
    normalized = str(value or "").strip().lower()
    return (
        normalized
        if normalized in SHEET_MEDIA_MODES
        else default_media_mode_for_title(sheet_title)
    )


def output_group_for_row(group_name: str, media_mode: str, is_tv: bool) -> str:
    """
    计算当前表格行的输出分组

    :param group_name (str): 工作表基础输出分组
    :param media_mode (str): 工作表媒体类型模式
    :param is_tv (bool): 当前行是否为剧集

    :return str: 当前行实际输出分组
    """
    base_group = str(group_name or "").strip()
    if media_mode == "mixed" and is_tv:
        return base_group if base_group.endswith("-剧集") else f"{base_group}-剧集"
    return base_group


class CatalogParser:
    """把腾讯文档表格行转换为稳定的媒体目录记录"""

    @staticmethod
    def normalize_header(value: str) -> str:
        """
        标准化表头文本

        :param value (str): 原始表头

        :return str: 移除空白和标点后的表头
        """
        return re.sub(r"[\s:：()（）_\-]", "", str(value or "")).lower()

    @classmethod
    def identify_headers(cls, row: List[str]) -> Dict[str, int]:
        """
        识别表头字段所在列

        :param row (List): 第一行单元格文本

        :return Dict: 标准字段到列下标的映射
        """
        normalized_aliases = {
            field: {cls.normalize_header(alias) for alias in aliases}
            for field, aliases in HEADER_ALIASES.items()
        }
        result: Dict[str, int] = {}
        for index, value in enumerate(row):
            normalized = cls.normalize_header(value)
            for field, aliases in normalized_aliases.items():
                if normalized in aliases and field not in result:
                    result[field] = index
        return result

    @staticmethod
    def _value(row: List[str], header_map: Dict[str, int], field: str) -> str:
        index = header_map.get(field, DEFAULT_COLUMN_INDEX[field])
        if index < 0 or index >= len(row):
            return ""
        return str(row[index] or "").strip()

    @staticmethod
    def _extract_share_url(value: str, row: List[str]) -> str:
        candidates = [value, *row]
        urls = []
        for candidate in candidates:
            urls.extend(
                match.group(0).rstrip(".。")
                for match in re.finditer(
                    r"https?://[^\s\]）)}>,，]+",
                    str(candidate or ""),
                    re.IGNORECASE,
                )
            )
        for url in urls:
            if re.search(r"/(?:s|share)/[^/?#&]+", urlsplit(url).path, re.IGNORECASE):
                return url
        return urls[0] if urls else ""

    @staticmethod
    def _resource_id(sheet_id: str, title: str, version: str, share_url: str) -> str:
        identity = json.dumps(
            [sheet_id, title.strip(), version.strip(), share_url.strip()],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _row_hash(values: List[str], group_name: str) -> str:
        payload = json.dumps(
            [*values, group_name],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def parse_row(
        cls,
        sheet_id: str,
        sheet_title: str,
        group_name: str,
        row_number: int,
        row: List[str],
        header_map: Dict[str, int],
        media_mode: str = "auto",
    ) -> Optional[Dict[str, Any]]:
        """
        解析一行媒体资源

        :param sheet_id (str): 工作表 ID
        :param sheet_title (str): 工作表标题
        :param group_name (str): 输出分组
        :param row_number (int): 表格行号
        :param row (List): 单元格文本列表
        :param header_map (Dict): 表头映射
        :param media_mode (str): movie、tv、mixed 或兼容旧行为的 auto

        :return Dict: 标准资源，非资源行返回 None
        """
        title = cls._value(row, header_map, "title")
        version = cls._value(row, header_map, "version")
        raw_link = cls._value(row, header_map, "share_url")
        share_url = cls._extract_share_url(raw_link, row)
        if not title or not share_url or not looks_like_url(share_url):
            return None
        raw_media_type = cls._value(row, header_map, "media_type")
        normalized_mode = str(media_mode or "auto").strip().lower()
        if normalized_mode == "tv":
            media_type = "电视剧"
            actual_group_name = output_group_for_row(group_name, normalized_mode, True)
        elif normalized_mode == "movie":
            media_type = "电影"
            actual_group_name = output_group_for_row(group_name, normalized_mode, False)
        elif normalized_mode == "mixed":
            is_tv = any(
                keyword in raw_media_type.lower() for keyword in TV_ROW_KEYWORDS
            )
            media_type = "电视剧" if is_tv else "电影"
            actual_group_name = output_group_for_row(
                group_name,
                normalized_mode,
                is_tv,
            )
        else:
            media_type = raw_media_type
            if not media_type and any(
                keyword in sheet_title for keyword in ("剧集", "电视剧", "连续剧")
            ):
                media_type = "电视剧"
            actual_group_name = group_name
        values = [
            title,
            version,
            share_url,
            media_type,
            cls._value(row, header_map, "rating"),
            cls._value(row, header_map, "year"),
        ]
        return {
            "resource_id": cls._resource_id(
                sheet_id,
                title,
                version,
                share_url,
            ),
            "row_number": row_number,
            "title": title,
            "version": version,
            "share_url": share_url,
            "media_type": media_type,
            "rating": values[4],
            "year": values[5],
            "group_name": actual_group_name,
            "row_hash": cls._row_hash(values, actual_group_name),
        }


class CatalogSynchronizer:
    """
    可分页暂停和断点恢复的腾讯文档目录同步器

    同步器只保存表格目录，不访问 115、不进行元数据识别，也不生成 STRM
    """

    def __init__(
        self,
        store: CatalogStore,
        client_factory: Callable[[], TencentDocumentClient],
        config_provider: Callable[[], Dict[str, Any]],
        config_updater: Callable[[Dict[str, Any]], None],
        stop_event: Event,
    ):
        """
        初始化目录同步器

        :param store (CatalogStore): 目录存储
        :param client_factory (Callable): 腾讯文档客户端工厂
        :param config_provider (Callable): 插件配置读取函数
        :param config_updater (Callable): 插件配置更新函数
        :param stop_event (Event): 插件停止信号
        """
        self.store = store
        self.client_factory = client_factory
        self.config_provider = config_provider
        self.config_updater = config_updater
        self.stop_event = stop_event
        self._run_lock = Lock()

    @staticmethod
    def _sheet_mappings(
        config: Dict[str, Any],
        sheets: List[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        mappings = {}
        for sheet in sheets:
            key = sheet_config_key(sheet["sheet_id"])
            mappings[sheet["sheet_id"]] = {
                "enabled": bool(config.get(f"sheet_{key}_enabled", False)),
                "group_name": str(
                    config.get(f"sheet_{key}_group")
                    or default_group_for_title(sheet["title"])
                ).strip(),
                "media_mode": normalize_media_mode(
                    str(config.get(f"sheet_{key}_media_mode") or ""),
                    str(sheet["title"]),
                ),
            }
        return mappings

    def discover_sheets(self) -> List[Dict[str, Any]]:
        """
        发现工作表但不读取任何资源行

        :return List: 工作表信息列表
        """
        config = dict(self.config_provider())
        client = self.client_factory()
        sources = document_sources(config)
        if not sources:
            raise TencentDocumentError("尚未配置腾讯文档链接")
        sheets: List[Dict[str, Any]] = []
        legacy_file_id = str(config.get("file_id") or "").strip()
        legacy_url = str(config.get("document_url") or "").strip()
        for source in sources:
            file_id = (
                legacy_file_id
                if len(sources) == 1 and source["url"] == legacy_url and legacy_file_id
                else client.convert_file_id(source["url"])
            )
            for remote_sheet in client.get_sheets(file_id):
                remote_sheet_id = str(remote_sheet["sheet_id"])
                source_title = str(remote_sheet["title"])
                sheets.append(
                    {
                        **remote_sheet,
                        "sheet_id": namespaced_sheet_id(file_id, remote_sheet_id),
                        "remote_sheet_id": remote_sheet_id,
                        "file_id": file_id,
                        "document_title": source["name"],
                        "source_title": source_title,
                        "title": f"{source['name']}（{source_title}）",
                    }
                )
        self.store.upsert_sheets(sheets)
        self.store.disable_missing_sheets({str(sheet["sheet_id"]) for sheet in sheets})
        config_changed = False
        for sheet in sheets:
            key = sheet_config_key(sheet["sheet_id"])
            enabled_key = f"sheet_{key}_enabled"
            group_key = f"sheet_{key}_group"
            media_mode_key = f"sheet_{key}_media_mode"
            if enabled_key not in config:
                config[enabled_key] = False
                config_changed = True
            if not config.get(group_key):
                config[group_key] = default_group_for_title(sheet["title"])
                config_changed = True
            if media_mode_key not in config:
                config[media_mode_key] = default_media_mode_for_title(sheet["title"])
                config_changed = True
        if config_changed:
            self.config_updater(config)
        self.store.configure_sheets(self._sheet_mappings(config, sheets))
        return self.store.list_sheets()

    def sync(
        self,
        reset: bool = False,
        max_pages: Optional[int] = None,
        mode: str = "manual",
    ) -> Dict[str, Any]:
        """
        分页同步已启用工作表并在达到页数限制时主动暂停

        :param reset (bool): 是否清除已有检查点
        :param max_pages (int): 本次最多处理分页数
        :param mode (str): 执行来源

        :return Dict: 本次同步摘要
        """
        if not self._run_lock.acquire(blocking=False):
            return {
                "status": "busy",
                "message": "已有目录同步任务正在运行",
                "processed_pages": 0,
                "processed_rows": 0,
            }
        run_id = self.store.create_sync_run(mode)
        processed_pages = 0
        processed_rows = 0
        current_sheet_id: Optional[str] = None
        try:
            config = dict(self.config_provider())
            if not self.store.list_sheets():
                self.discover_sheets()
                config = dict(self.config_provider())
            self.store.configure_sheets(
                self._sheet_mappings(config, self.store.list_sheets())
            )
            sheets = self.store.list_sheets(enabled_only=True)
            if not sheets:
                raise TencentDocumentError("尚未启用任何工作表")
            if reset or all(
                sheet.get("scan_status") == "completed" for sheet in sheets
            ):
                for sheet in sheets:
                    self.store.reset_sheet_scan(sheet["sheet_id"])
                sheets = self.store.list_sheets(enabled_only=True)
            else:
                sheets = [
                    sheet for sheet in sheets if sheet.get("scan_status") != "completed"
                ]
            client = self.client_factory()
            page_limit = max_pages or int(config.get("pages_per_run") or 5)
            page_limit = min(max(page_limit, 1), 100)
            requested_rows = int(config.get("page_rows") or 1000)
            max_columns = min(max(int(config.get("max_columns") or 10), 1), 200)
            for sheet in sheets:
                current_sheet_id = sheet["sheet_id"]
                checkpoint = self.store.begin_sheet_scan(current_sheet_id)
                scan_id = checkpoint["scan_id"]
                current_row = int(checkpoint["checkpoint_row"])
                header_map = dict(checkpoint["header_map"])
                total_rows = int(
                    sheet.get("used_row_count") or sheet.get("row_count") or 0
                )
                column_count = int(
                    sheet.get("used_column_count") or sheet.get("column_count") or 6
                )
                column_count = min(max(column_count, 1), max_columns)
                while not self.stop_event.is_set():
                    if processed_pages >= page_limit:
                        self.store.pause_sheet_scan(current_sheet_id)
                        self.store.finish_sync_run(run_id, "paused")
                        return {
                            "status": "paused",
                            "message": "已达到本次分页上限，可继续同步",
                            "processed_pages": processed_pages,
                            "processed_rows": processed_rows,
                        }
                    page = client.get_range(
                        file_id=str(
                            sheet.get("file_id") or config.get("file_id") or ""
                        ),
                        sheet_id=str(sheet.get("remote_sheet_id") or current_sheet_id),
                        start_row=current_row,
                        row_count=requested_rows,
                        column_count=column_count,
                    )
                    rows = page["rows"]
                    if current_row == 1 and rows:
                        identified_headers = CatalogParser.identify_headers(rows[0])
                        if identified_headers:
                            header_map = identified_headers
                    resources = []
                    for offset, row in enumerate(rows):
                        row_number = current_row + offset
                        if row_number == 1:
                            continue
                        resource = CatalogParser.parse_row(
                            sheet_id=current_sheet_id,
                            sheet_title=sheet["title"],
                            group_name=sheet["group_name"],
                            row_number=row_number,
                            row=row,
                            header_map=header_map,
                            media_mode=str(sheet.get("media_mode") or "movie"),
                        )
                        if resource:
                            resources.append(resource)
                    next_row = current_row + int(page["requested_rows"])
                    source_row_count = max(
                        len(rows) - (1 if current_row == 1 else 0), 0
                    )
                    self.store.save_page(
                        sheet_id=current_sheet_id,
                        scan_id=scan_id,
                        next_row=next_row,
                        header_map=header_map,
                        resources=resources,
                        source_row_count=source_row_count,
                    )
                    processed_pages += 1
                    processed_rows += source_row_count
                    self.store.update_sync_run(
                        run_id=run_id,
                        current_sheet=sheet["title"],
                        current_row=next_row,
                        pages_delta=1,
                        rows_delta=source_row_count,
                    )
                    logger.info(
                        f"腾讯文档目录同步：{sheet['title']} 已提交到第 {next_row - 1} 行"
                    )
                    reached_known_end = bool(total_rows and next_row > total_rows)
                    reached_short_page = len(rows) < int(page["requested_rows"])
                    if reached_known_end or reached_short_page or not rows:
                        self.store.complete_sheet_scan(current_sheet_id, scan_id)
                        break
                    current_row = next_row
                if self.stop_event.is_set():
                    self.store.pause_sheet_scan(current_sheet_id, "插件正在停止")
                    self.store.finish_sync_run(run_id, "interrupted", "插件正在停止")
                    return {
                        "status": "interrupted",
                        "message": "插件停止，检查点已经保存",
                        "processed_pages": processed_pages,
                        "processed_rows": processed_rows,
                    }
            self.store.finish_sync_run(run_id, "completed")
            return {
                "status": "completed",
                "message": "已完成所有启用工作表的本轮扫描",
                "processed_pages": processed_pages,
                "processed_rows": processed_rows,
            }
        except Exception as error:
            message = str(error)
            if current_sheet_id:
                self.store.pause_sheet_scan(current_sheet_id, message)
            self.store.finish_sync_run(run_id, "failed", message)
            logger.error(f"腾讯文档目录同步失败：{message}", exc_info=True)
            return {
                "status": "failed",
                "message": message,
                "processed_pages": processed_pages,
                "processed_rows": processed_rows,
            }
        finally:
            self._run_lock.release()
