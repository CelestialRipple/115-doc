import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Generator, List, Optional, Tuple
from uuid import uuid4

SCHEMA_VERSION = 5


def utc_now() -> str:
    """返回可排序的 UTC 时间文本"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CatalogStore:
    """
    腾讯文档媒体目录的插件私有 SQLite 存储

    每个表格分页在一个事务内同时提交资源和检查点，确保进程中断后最多重做一页
    """

    def __init__(self, database_path: Path):
        """
        初始化目录存储

        :param database_path (Path): SQLite 数据库路径
        """
        self._database_path = database_path
        self._lock = RLock()
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        """
        创建开启外键约束的短连接

        :yields Generator: SQLite 连接
        """
        connection = sqlite3.connect(
            self._database_path,
            timeout=30,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS sheet_state (
                    sheet_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    source_title TEXT,
                    document_title TEXT,
                    file_id TEXT,
                    remote_sheet_id TEXT,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    column_count INTEGER NOT NULL DEFAULT 0,
                    used_row_count INTEGER NOT NULL DEFAULT 0,
                    used_column_count INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    group_name TEXT NOT NULL DEFAULT '',
                    checkpoint_row INTEGER NOT NULL DEFAULT 1,
                    scan_id TEXT,
                    scan_status TEXT NOT NULL DEFAULT 'idle',
                    header_json TEXT NOT NULL DEFAULT '{}',
                    processed_rows INTEGER NOT NULL DEFAULT 0,
                    last_sync_at TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS resource (
                    resource_id TEXT PRIMARY KEY,
                    sheet_id TEXT NOT NULL,
                    row_number INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    version TEXT,
                    share_url TEXT NOT NULL,
                    media_type TEXT,
                    rating TEXT,
                    year TEXT,
                    group_name TEXT NOT NULL,
                    row_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    strm_status TEXT NOT NULL DEFAULT 'pending',
                    scrape_status TEXT NOT NULL DEFAULT 'pending',
                    last_error TEXT,
                    media_source TEXT,
                    media_id TEXT,
                    tmdb_id TEXT,
                    strm_path TEXT,
                    resolved_file_id TEXT,
                    resolved_file_name TEXT,
                    resolved_at TEXT,
                    last_seen_scan TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(sheet_id) REFERENCES sheet_state(sheet_id)
                );

                CREATE INDEX IF NOT EXISTS idx_resource_sheet
                    ON resource(sheet_id, row_number);
                CREATE INDEX IF NOT EXISTS idx_resource_status
                    ON resource(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_resource_title
                    ON resource(title, year);

                CREATE TABLE IF NOT EXISTS resource_file (
                    resource_id TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_path TEXT,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    season INTEGER,
                    episode INTEGER,
                    strm_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(resource_id, file_id),
                    FOREIGN KEY(resource_id) REFERENCES resource(resource_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS sync_run (
                    run_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_sheet TEXT,
                    current_row INTEGER,
                    processed_pages INTEGER NOT NULL DEFAULT 0,
                    processed_rows INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS direct_download_task (
                    task_id TEXT PRIMARY KEY,
                    resource_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    download_dir TEXT NOT NULL,
                    episodes_json TEXT NOT NULL DEFAULT '[]',
                    content_path TEXT,
                    state TEXT NOT NULL DEFAULT 'queued',
                    total_size INTEGER NOT NULL DEFAULT 0,
                    downloaded_size INTEGER NOT NULL DEFAULT 0,
                    speed INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    organized INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(resource_id) REFERENCES resource(resource_id)
                );

                CREATE INDEX IF NOT EXISTS idx_direct_download_state
                    ON direct_download_task(state, organized, updated_at);
                """)
            download_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(direct_download_task)"
                ).fetchall()
            }
            if "episodes_json" not in download_columns:
                connection.execute(
                    "ALTER TABLE direct_download_task "
                    "ADD COLUMN episodes_json TEXT NOT NULL DEFAULT '[]'"
                )
            sheet_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(sheet_state)"
                ).fetchall()
            }
            for column_name in (
                "source_title",
                "document_title",
                "file_id",
                "remote_sheet_id",
            ):
                if column_name not in sheet_columns:
                    connection.execute(
                        f"ALTER TABLE sheet_state ADD COLUMN {column_name} TEXT"
                    )
            resource_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(resource)").fetchall()
            }
            if "strm_status" not in resource_columns:
                connection.execute(
                    "ALTER TABLE resource ADD COLUMN strm_status "
                    "TEXT NOT NULL DEFAULT 'pending'"
                )
            if "scrape_status" not in resource_columns:
                connection.execute(
                    "ALTER TABLE resource ADD COLUMN scrape_status "
                    "TEXT NOT NULL DEFAULT 'pending'"
                )
            connection.execute("""
                UPDATE resource
                SET strm_status = CASE
                        WHEN status = 'ready' OR strm_path IS NOT NULL THEN 'ready'
                        WHEN status IN ('invalid_share', 'share_error', 'build_error') THEN 'failed'
                        ELSE strm_status
                    END,
                    scrape_status = CASE
                        WHEN status = 'ready' THEN 'ready'
                        WHEN status = 'metadata_error' THEN 'failed'
                        WHEN status IN ('invalid_share', 'share_error', 'build_error') THEN 'blocked'
                        ELSE scrape_status
                    END
                WHERE strm_status = 'pending' AND scrape_status = 'pending'
                """)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.execute(
                "UPDATE sync_run SET status = 'interrupted', finished_at = ? "
                "WHERE status = 'running'",
                (utc_now(),),
            )
            connection.execute(
                "UPDATE sheet_state SET scan_status = 'paused' "
                "WHERE scan_status = 'running'"
            )
            connection.execute(
                "UPDATE direct_download_task SET state = 'paused', "
                "last_error = 'MoviePilot 重启，可重新点击下载以断点续传', "
                "updated_at = ? WHERE state IN ('queued', 'downloading')",
                (utc_now(),),
            )
            connection.execute(
                "UPDATE resource SET status = 'pending', "
                "strm_status = 'pending', scrape_status = 'pending', "
                "last_error = 'MoviePilot 重启，已恢复到待生成队列', updated_at = ? "
                "WHERE status = 'processing'",
                (utc_now(),),
            )
            connection.execute(
                """
                UPDATE resource
                SET status = 'invalid_share', updated_at = ?
                WHERE status = 'share_error'
                  AND (
                    last_error LIKE '%访问码错误%'
                    OR last_error LIKE '%密码错误%'
                    OR last_error LIKE '%分享已失效%'
                    OR last_error LIKE '%分享无效%'
                    OR last_error LIKE '%无法从资源链接中识别%'
                    OR last_error LIKE '%未发现可播放视频%'
                  )
                """,
                (utc_now(),),
            )

    def upsert_sheets(self, sheets: List[Dict[str, Any]]) -> None:
        """
        保存腾讯文档返回的工作表信息

        :param sheets (List): 工作表信息列表
        """
        now = utc_now()
        with self._lock, self.connection() as connection:
            for sheet in sheets:
                connection.execute(
                    """
                    INSERT INTO sheet_state (
                        sheet_id, title, source_title, document_title,
                        file_id, remote_sheet_id, row_count, column_count,
                        used_row_count, used_column_count, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(sheet_id) DO UPDATE SET
                        title = excluded.title,
                        source_title = excluded.source_title,
                        document_title = excluded.document_title,
                        file_id = excluded.file_id,
                        remote_sheet_id = excluded.remote_sheet_id,
                        row_count = excluded.row_count,
                        column_count = excluded.column_count,
                        used_row_count = excluded.used_row_count,
                        used_column_count = excluded.used_column_count,
                        updated_at = excluded.updated_at
                    """,
                    (
                        sheet["sheet_id"],
                        sheet["title"],
                        sheet.get("source_title") or sheet["title"],
                        sheet.get("document_title"),
                        sheet.get("file_id"),
                        sheet.get("remote_sheet_id") or sheet["sheet_id"],
                        int(sheet.get("row_count") or 0),
                        int(sheet.get("column_count") or 0),
                        int(sheet.get("used_row_count") or 0),
                        int(sheet.get("used_column_count") or 0),
                        now,
                    ),
                )

    def disable_missing_sheets(self, present_sheet_ids: set[str]) -> None:
        """停用已不在当前文档配置中的工作表，但保留其资源和历史。"""
        if not present_sheet_ids:
            return
        placeholders = ",".join("?" for _ in present_sheet_ids)
        with self._lock, self.connection() as connection:
            connection.execute(
                f"UPDATE sheet_state SET enabled = 0, updated_at = ? "
                f"WHERE sheet_id NOT IN ({placeholders})",
                (utc_now(), *sorted(present_sheet_ids)),
            )

    def configure_sheets(self, mappings: Dict[str, Dict[str, Any]]) -> None:
        """
        更新工作表启用状态和输出分组

        :param mappings (Dict): 以工作表 ID 为键的配置映射
        """
        with self._lock, self.connection() as connection:
            for sheet_id, mapping in mappings.items():
                group_name = str(mapping.get("group_name") or "").strip()
                current = connection.execute(
                    "SELECT group_name FROM sheet_state WHERE sheet_id = ?",
                    (sheet_id,),
                ).fetchone()
                connection.execute(
                    "UPDATE sheet_state SET enabled = ?, group_name = ?, updated_at = ? "
                    "WHERE sheet_id = ?",
                    (
                        1 if mapping.get("enabled") else 0,
                        group_name,
                        utc_now(),
                        sheet_id,
                    ),
                )
                if current and current["group_name"] != group_name:
                    connection.execute(
                        "UPDATE resource SET group_name = ?, status = 'pending', "
                        "strm_status = 'pending', scrape_status = 'pending', "
                        "strm_path = NULL, last_error = NULL, updated_at = ? "
                        "WHERE sheet_id = ? AND status <> 'removed'",
                        (group_name, utc_now(), sheet_id),
                    )

    def list_sheets(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        """
        查询工作表状态

        :param enabled_only (bool): 是否只返回已启用工作表

        :return List: 工作表状态列表
        """
        query = "SELECT * FROM sheet_state"
        parameters: Tuple[Any, ...] = ()
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY COALESCE(document_title, ''), title"
        with self.connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def get_sheet(self, sheet_id: str) -> Optional[Dict[str, Any]]:
        """
        查询单个工作表状态

        :param sheet_id (str): 工作表 ID

        :return Dict: 工作表状态，不存在时返回 None
        """
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM sheet_state WHERE sheet_id = ?",
                (sheet_id,),
            ).fetchone()
        return dict(row) if row else None

    def reset_sheet_scan(self, sheet_id: str) -> None:
        """
        清除指定工作表的扫描检查点

        :param sheet_id (str): 工作表 ID
        """
        with self._lock, self.connection() as connection:
            connection.execute(
                """
                UPDATE sheet_state
                SET checkpoint_row = 1, scan_id = NULL, scan_status = 'idle',
                    header_json = '{}', processed_rows = 0, last_error = NULL,
                    updated_at = ?
                WHERE sheet_id = ?
                """,
                (utc_now(), sheet_id),
            )

    def begin_sheet_scan(self, sheet_id: str) -> Dict[str, Any]:
        """
        开始或恢复工作表扫描

        :param sheet_id (str): 工作表 ID

        :return Dict: 包含扫描 ID、起始行和表头映射的检查点
        """
        with self._lock, self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM sheet_state WHERE sheet_id = ?",
                (sheet_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"工作表不存在：{sheet_id}")
            scan_id = row["scan_id"]
            if not scan_id or row["scan_status"] == "completed":
                scan_id = uuid4().hex
                checkpoint_row = 1
                header_json = "{}"
                processed_rows = 0
            else:
                checkpoint_row = max(int(row["checkpoint_row"] or 1), 1)
                header_json = row["header_json"] or "{}"
                processed_rows = int(row["processed_rows"] or 0)
            connection.execute(
                """
                UPDATE sheet_state
                SET scan_id = ?, checkpoint_row = ?, header_json = ?,
                    processed_rows = ?, scan_status = 'running',
                    last_error = NULL, updated_at = ?
                WHERE sheet_id = ?
                """,
                (
                    scan_id,
                    checkpoint_row,
                    header_json,
                    processed_rows,
                    utc_now(),
                    sheet_id,
                ),
            )
        return {
            "scan_id": scan_id,
            "checkpoint_row": checkpoint_row,
            "header_map": json.loads(header_json),
            "processed_rows": processed_rows,
        }

    def save_page(
        self,
        sheet_id: str,
        scan_id: str,
        next_row: int,
        header_map: Dict[str, int],
        resources: List[Dict[str, Any]],
        source_row_count: int,
    ) -> None:
        """
        原子保存一页资源和下一页检查点

        :param sheet_id (str): 工作表 ID
        :param scan_id (str): 当前扫描 ID
        :param next_row (int): 下一次读取的起始行
        :param header_map (Dict): 已识别的表头映射
        :param resources (List): 当前分页解析出的资源
        :param source_row_count (int): 当前分页包含的原始数据行数
        """
        now = utc_now()
        with self._lock, self.connection() as connection:
            for resource in resources:
                existing = connection.execute(
                    "SELECT row_hash, group_name, status FROM resource "
                    "WHERE resource_id = ?",
                    (resource["resource_id"],),
                ).fetchone()
                changed = bool(
                    existing
                    and (
                        existing["row_hash"] != resource["row_hash"]
                        or existing["group_name"] != resource["group_name"]
                        or existing["status"] == "removed"
                    )
                )
                connection.execute(
                    """
                    INSERT INTO resource (
                        resource_id, sheet_id, row_number, title, version,
                        share_url, media_type, rating, year, group_name,
                        row_hash, status, last_seen_scan, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    ON CONFLICT(resource_id) DO UPDATE SET
                        sheet_id = excluded.sheet_id,
                        row_number = excluded.row_number,
                        title = excluded.title,
                        version = excluded.version,
                        share_url = excluded.share_url,
                        media_type = excluded.media_type,
                        rating = excluded.rating,
                        year = excluded.year,
                        group_name = excluded.group_name,
                        row_hash = excluded.row_hash,
                        status = CASE WHEN ? THEN 'pending' ELSE resource.status END,
                        strm_status = CASE WHEN ? THEN 'pending' ELSE resource.strm_status END,
                        scrape_status = CASE WHEN ? THEN 'pending' ELSE resource.scrape_status END,
                        last_error = CASE WHEN ? THEN NULL ELSE resource.last_error END,
                        strm_path = CASE WHEN ? THEN NULL ELSE resource.strm_path END,
                        resolved_file_id = CASE WHEN ? THEN NULL ELSE resource.resolved_file_id END,
                        resolved_file_name = CASE WHEN ? THEN NULL ELSE resource.resolved_file_name END,
                        resolved_at = CASE WHEN ? THEN NULL ELSE resource.resolved_at END,
                        last_seen_scan = excluded.last_seen_scan,
                        updated_at = excluded.updated_at
                    """,
                    (
                        resource["resource_id"],
                        sheet_id,
                        int(resource["row_number"]),
                        resource["title"],
                        resource.get("version"),
                        resource["share_url"],
                        resource.get("media_type"),
                        resource.get("rating"),
                        resource.get("year"),
                        resource["group_name"],
                        resource["row_hash"],
                        scan_id,
                        now,
                        now,
                        changed,
                        changed,
                        changed,
                        changed,
                        changed,
                        changed,
                        changed,
                        changed,
                    ),
                )
                if changed:
                    connection.execute(
                        "DELETE FROM resource_file WHERE resource_id = ?",
                        (resource["resource_id"],),
                    )
            connection.execute(
                """
                UPDATE sheet_state
                SET checkpoint_row = ?, header_json = ?,
                    processed_rows = processed_rows + ?, updated_at = ?
                WHERE sheet_id = ? AND scan_id = ?
                """,
                (
                    next_row,
                    json.dumps(header_map, ensure_ascii=False),
                    source_row_count,
                    now,
                    sheet_id,
                    scan_id,
                ),
            )

    def pause_sheet_scan(self, sheet_id: str, error: Optional[str] = None) -> None:
        """
        暂停工作表扫描并保留检查点

        :param sheet_id (str): 工作表 ID
        :param error (str): 暂停原因
        """
        with self._lock, self.connection() as connection:
            connection.execute(
                "UPDATE sheet_state SET scan_status = 'paused', last_error = ?, "
                "updated_at = ? WHERE sheet_id = ?",
                (error, utc_now(), sheet_id),
            )

    def complete_sheet_scan(self, sheet_id: str, scan_id: str) -> None:
        """
        完成一次扫描并把未出现的旧资源标记为已移除

        :param sheet_id (str): 工作表 ID
        :param scan_id (str): 当前扫描 ID
        """
        now = utc_now()
        with self._lock, self.connection() as connection:
            connection.execute(
                """
                UPDATE resource SET status = 'removed', updated_at = ?
                WHERE sheet_id = ? AND last_seen_scan <> ?
                """,
                (now, sheet_id, scan_id),
            )
            connection.execute(
                """
                UPDATE sheet_state
                SET checkpoint_row = 1, scan_id = NULL, scan_status = 'completed',
                    header_json = '{}', processed_rows = 0,
                    last_sync_at = ?, last_error = NULL, updated_at = ?
                WHERE sheet_id = ? AND scan_id = ?
                """,
                (now, now, sheet_id, scan_id),
            )

    def create_sync_run(self, mode: str) -> str:
        """
        创建同步执行记录

        :param mode (str): 执行模式

        :return str: 执行记录 ID
        """
        run_id = uuid4().hex
        with self._lock, self.connection() as connection:
            connection.execute(
                "INSERT INTO sync_run (run_id, mode, status, started_at) "
                "VALUES (?, ?, 'running', ?)",
                (run_id, mode, utc_now()),
            )
        return run_id

    def update_sync_run(
        self,
        run_id: str,
        current_sheet: Optional[str],
        current_row: Optional[int],
        pages_delta: int = 0,
        rows_delta: int = 0,
    ) -> None:
        """
        更新同步执行进度

        :param run_id (str): 执行记录 ID
        :param current_sheet (str): 当前工作表
        :param current_row (int): 当前行号
        :param pages_delta (int): 新增分页数
        :param rows_delta (int): 新增行数
        """
        with self._lock, self.connection() as connection:
            connection.execute(
                """
                UPDATE sync_run
                SET current_sheet = ?, current_row = ?,
                    processed_pages = processed_pages + ?,
                    processed_rows = processed_rows + ?
                WHERE run_id = ?
                """,
                (
                    current_sheet,
                    current_row,
                    pages_delta,
                    rows_delta,
                    run_id,
                ),
            )

    def finish_sync_run(
        self,
        run_id: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        """
        完成同步执行记录

        :param run_id (str): 执行记录 ID
        :param status (str): 最终状态
        :param error (str): 错误原因
        """
        with self._lock, self.connection() as connection:
            connection.execute(
                "UPDATE sync_run SET status = ?, finished_at = ?, last_error = ? "
                "WHERE run_id = ?",
                (status, utc_now(), error, run_id),
            )

    def list_build_candidates(
        self,
        limit: int,
        retry_failed: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        查询待生成 STRM 的资源

        :param limit (int): 最大返回数量
        :param retry_failed (bool): 是否包含失败资源

        :return List: 待处理资源列表
        """
        statuses = ["pending"]
        if retry_failed:
            statuses.extend(["metadata_error", "share_error", "build_error"])
        placeholders = ",".join("?" for _ in statuses)
        query = (
            f"SELECT * FROM resource WHERE status IN ({placeholders}) "
            "ORDER BY updated_at, sheet_id, row_number LIMIT ?"
        )
        with self.connection() as connection:
            rows = connection.execute(query, (*statuses, int(limit))).fetchall()
        return [dict(row) for row in rows]

    def search_resources(
        self,
        keyword: str,
        limit: int = 50,
        offset: int = 0,
        ready_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """从本地镜像目录搜索资源，不访问腾讯文档或 115。"""
        escaped = (
            str(keyword or "")
            .strip()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        status_clause = (
            "AND status = 'ready'"
            if ready_only
            else ("AND status NOT IN ('removed', 'invalid_share', 'share_error')")
        )
        query = f"""
            SELECT * FROM resource
            WHERE (title LIKE ? ESCAPE '\\'
                   OR year LIKE ? ESCAPE '\\'
                   OR version LIKE ? ESCAPE '\\')
              {status_clause}
            ORDER BY CASE WHEN title = ? THEN 0 ELSE 1 END,
                     title, year, version
            LIMIT ? OFFSET ?
        """
        with self.connection() as connection:
            rows = connection.execute(
                query,
                (
                    pattern,
                    pattern,
                    pattern,
                    str(keyword or "").strip(),
                    min(max(int(limit), 1), 100),
                    max(int(offset), 0),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_download_task(self, task: Dict[str, Any]) -> None:
        """创建或重新排队一个直链下载任务。"""
        now = utc_now()
        with self._lock, self.connection() as connection:
            connection.execute(
                """
                INSERT INTO direct_download_task (
                    task_id, resource_id, title, download_dir, episodes_json,
                    content_path,
                    state, total_size, downloaded_size, speed, last_error,
                    organized, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, 0, 0, NULL, 0, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    title = excluded.title,
                    download_dir = excluded.download_dir,
                    episodes_json = excluded.episodes_json,
                    content_path = excluded.content_path,
                    state = 'queued',
                    speed = 0,
                    last_error = NULL,
                    organized = 0,
                    updated_at = excluded.updated_at
                """,
                (
                    task["task_id"],
                    task["resource_id"],
                    task["title"],
                    task["download_dir"],
                    task.get("episodes_json") or "[]",
                    task.get("content_path"),
                    now,
                    now,
                ),
            )

    def update_download_task(self, task_id: str, **fields: Any) -> None:
        """更新直链下载任务的白名单状态字段。"""
        allowed = {
            "content_path",
            "state",
            "total_size",
            "downloaded_size",
            "speed",
            "last_error",
            "organized",
        }
        updates = ["updated_at = ?"]
        parameters: List[Any] = [utc_now()]
        for name, value in fields.items():
            if name not in allowed:
                continue
            updates.append(f"{name} = ?")
            parameters.append(value)
        parameters.append(task_id)
        with self._lock, self.connection() as connection:
            connection.execute(
                f"UPDATE direct_download_task SET {', '.join(updates)} "
                "WHERE task_id = ?",
                parameters,
            )

    def list_download_tasks(
        self,
        states: Optional[List[str]] = None,
        task_ids: Optional[List[str]] = None,
        include_organized: bool = False,
    ) -> List[Dict[str, Any]]:
        """查询直链下载任务。"""
        clauses = []
        parameters: List[Any] = []
        if states:
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            parameters.extend(states)
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            clauses.append(f"task_id IN ({placeholders})")
            parameters.extend(task_ids)
        if not include_organized:
            clauses.append("organized = 0")
        query = "SELECT * FROM direct_download_task"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC"
        with self.connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def delete_download_tasks(self, task_ids: List[str]) -> int:
        """删除指定直链下载任务记录，不删除已经下载的文件。"""
        if not task_ids:
            return 0
        placeholders = ",".join("?" for _ in task_ids)
        with self._lock, self.connection() as connection:
            cursor = connection.execute(
                f"DELETE FROM direct_download_task "
                f"WHERE task_id IN ({placeholders})",
                task_ids,
            )
        return int(cursor.rowcount)

    def get_resource(self, resource_id: str) -> Optional[Dict[str, Any]]:
        """
        查询单条媒体资源

        :param resource_id (str): 资源 ID

        :return Dict: 资源信息，不存在时返回 None
        """
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM resource WHERE resource_id = ?",
                (resource_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_resource_status(
        self,
        resource_id: str,
        status: str,
        error: Optional[str] = None,
        **fields: Any,
    ) -> None:
        """
        更新资源处理状态和白名单字段

        :param resource_id (str): 资源 ID
        :param status (str): 新状态
        :param error (str): 错误信息
        :param fields (Any): 允许同步更新的资源字段
        """
        allowed_fields = {
            "media_source",
            "media_id",
            "tmdb_id",
            "strm_path",
            "resolved_file_id",
            "resolved_file_name",
            "resolved_at",
            "strm_status",
            "scrape_status",
        }
        updates = ["status = ?", "last_error = ?", "updated_at = ?"]
        parameters: List[Any] = [status, error, utc_now()]
        for name, value in fields.items():
            if name not in allowed_fields:
                continue
            updates.append(f"{name} = ?")
            parameters.append(value)
        parameters.append(resource_id)
        with self._lock, self.connection() as connection:
            connection.execute(
                f"UPDATE resource SET {', '.join(updates)} WHERE resource_id = ?",
                parameters,
            )

    def replace_resource_files(
        self,
        resource_id: str,
        files: List[Dict[str, Any]],
    ) -> None:
        """
        替换电视剧资源的文件展开结果

        :param resource_id (str): 资源 ID
        :param files (List): 分享文件信息列表
        """
        now = utc_now()
        with self._lock, self.connection() as connection:
            connection.execute(
                "DELETE FROM resource_file WHERE resource_id = ?",
                (resource_id,),
            )
            for item in files:
                connection.execute(
                    """
                    INSERT INTO resource_file (
                        resource_id, file_id, file_name, file_path, file_size,
                        season, episode, strm_path, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resource_id,
                        item["file_id"],
                        item["file_name"],
                        item.get("file_path"),
                        int(item.get("file_size") or 0),
                        item.get("season"),
                        item.get("episode"),
                        item.get("strm_path"),
                        now,
                        now,
                    ),
                )

    def get_resource_file(
        self,
        resource_id: str,
        file_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        查询展开后的单个分享文件

        :param resource_id (str): 资源 ID
        :param file_id (str): 115 文件 ID

        :return Dict: 文件信息，不存在时返回 None
        """
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM resource_file WHERE resource_id = ? AND file_id = ?",
                (resource_id, file_id),
            ).fetchone()
        return dict(row) if row else None

    def list_resource_files(self, resource_id: str) -> List[Dict[str, Any]]:
        """查询某个剧集资源已经展开的全部视频文件。"""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM resource_file WHERE resource_id = ? "
                "ORDER BY season, episode, file_name",
                (resource_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def retry_resources(self, resource_ids: List[str]) -> int:
        """
        把指定失败资源重新加入生成队列

        :param resource_ids (List): 资源 ID 列表

        :return int: 实际更新数量
        """
        placeholders = ",".join("?" for _ in resource_ids)
        with self._lock, self.connection() as connection:
            cursor = connection.execute(
                f"UPDATE resource SET status = 'pending', "
                f"strm_status = 'pending', scrape_status = 'pending', "
                f"last_error = NULL, "
                f"updated_at = ? WHERE resource_id IN ({placeholders}) "
                f"AND status <> 'invalid_share'",
                (utc_now(), *resource_ids),
            )
        return int(cursor.rowcount)

    def retry_all_failed_resources(self) -> int:
        """把全部生成、分享和刮削失败资源重新加入待生成队列。"""
        with self._lock, self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE resource
                SET status = 'pending', strm_status = 'pending',
                    scrape_status = 'pending', last_error = NULL, updated_at = ?
                WHERE status IN ('share_error', 'metadata_error', 'build_error')
                """,
                (utc_now(),),
            )
        return int(cursor.rowcount)

    def retry_errors_containing(self, message_fragment: str) -> int:
        """把由已知插件兼容错误造成的失败资源自动重新加入队列。"""
        fragment = str(message_fragment or "").strip()
        if not fragment:
            return 0
        with self._lock, self.connection() as connection:
            cursor = connection.execute(
                "UPDATE resource SET status = 'pending', "
                "strm_status = 'pending', scrape_status = 'pending', "
                "last_error = NULL, "
                "updated_at = ? WHERE status = 'build_error' AND last_error LIKE ?",
                (utc_now(), f"%{fragment}%"),
            )
        return int(cursor.rowcount)

    def status_snapshot(self) -> Dict[str, Any]:
        """
        生成插件状态摘要

        :return Dict: 工作表、资源计数和最近执行记录
        """
        with self.connection() as connection:
            resource_counts = {
                row["status"]: int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM resource GROUP BY status"
                ).fetchall()
            }
            total_resources = sum(resource_counts.values())
            active_resources = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM resource WHERE status <> 'removed'"
                ).fetchone()["count"]
            )
            strm_counts = {
                row["strm_status"]: int(row["count"])
                for row in connection.execute(
                    "SELECT strm_status, COUNT(*) AS count FROM resource "
                    "WHERE status <> 'removed' GROUP BY strm_status"
                ).fetchall()
            }
            scrape_counts = {
                row["scrape_status"]: int(row["count"])
                for row in connection.execute(
                    "SELECT scrape_status, COUNT(*) AS count FROM resource "
                    "WHERE status <> 'removed' GROUP BY scrape_status"
                ).fetchall()
            }
            current_resources = [dict(row) for row in connection.execute("""
                    SELECT resource_id, title, group_name, strm_status,
                           scrape_status, updated_at
                    FROM resource
                    WHERE status = 'processing'
                    ORDER BY updated_at DESC LIMIT 5
                    """).fetchall()]
            recent_runs = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM sync_run ORDER BY started_at DESC LIMIT 10"
                ).fetchall()
            ]
            recent_errors = [dict(row) for row in connection.execute("""
                    SELECT resource_id, title, year, status, strm_status,
                           scrape_status, last_error, updated_at
                    FROM resource
                    WHERE last_error IS NOT NULL AND last_error <> ''
                    ORDER BY updated_at DESC LIMIT 20
                    """).fetchall()]
        return {
            "total_resources": total_resources,
            "active_resources": active_resources,
            "resource_counts": resource_counts,
            "strm_counts": strm_counts,
            "scrape_counts": scrape_counts,
            "current_resources": current_resources,
            "sheets": self.list_sheets(),
            "recent_runs": recent_runs,
            "recent_errors": recent_errors,
        }
