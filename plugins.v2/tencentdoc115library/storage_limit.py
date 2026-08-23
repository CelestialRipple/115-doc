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
