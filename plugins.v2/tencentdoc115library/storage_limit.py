import os
from pathlib import Path
from typing import Any, Dict


GIB = 1024 ** 3


def configured_limit_bytes(config: Dict[str, Any]) -> int:
    """把用户配置的 GiB 上限转换为字节；0 表示不限制。"""
    try:
        value = float(config.get("output_size_limit_gb") or 0)
    except (TypeError, ValueError):
        value = 0
    return max(int(value * GIB), 0)


def directory_size(path: Path) -> int:
    """统计目录内普通文件大小，忽略软链接和暂时不可访问的文件。"""
    root = Path(path)
    if not root.exists():
        return 0
    if root.is_file():
        try:
            return int(root.stat().st_size)
        except OSError:
            return 0
    total = 0
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += int(entry.stat(follow_symlinks=False).st_size)
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def format_gib(size_bytes: int) -> str:
    """以便于配置页阅读的 GiB 文本显示字节数。"""
    return f"{max(int(size_bytes), 0) / GIB:.2f} GiB"


class DisplayStorageCache:
    """Non-blocking UI estimate; never used to enforce the build space limit."""

    def __init__(self):
        from threading import Lock
        self._lock = Lock()
        self._worker = None
        self._root = None
        self._usage = None
        self._updated = 0.0
        self._error = False

    def snapshot(self, config):
        import time
        from threading import Thread
        root = str(config.get("output_root") or "").strip()
        limit = configured_limit_bytes(config)
        with self._lock:
            if root != self._root:
                self._root, self._usage, self._updated, self._error = root, None, 0.0, False
            running = self._worker is not None and self._worker.is_alive()
            if root and not running and (not self._updated or time.monotonic() - self._updated >= 300):
                self._worker = Thread(target=self._refresh, args=(root,), daemon=True,
                                      name="115-storage-display")
                self._worker.start()
                running = True
            usage = self._usage if root else 0
            return {"output_root": root, "usage_bytes": usage,
                    "limit_bytes": limit,
                    "limit_reached": bool(limit and usage is not None and usage >= limit),
                    "usage_pending": usage is None, "usage_refreshing": running,
                    "usage_error": self._error}

    def _refresh(self, root):
        import time
        try:
            usage = directory_size(Path(root).expanduser())
        except Exception:
            with self._lock:
                if root == self._root:
                    self._updated, self._error = time.monotonic(), True
            return
        with self._lock:
            if root == self._root:
                self._usage, self._updated, self._error = usage, time.monotonic(), False
