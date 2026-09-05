"""记录本插件实际写入的文件，清理时保留用户文件和后来修改过的内容。"""

import hashlib
import json
import os
from pathlib import Path
from threading import RLock

_lock = RLock()
MANIFEST = ".tencentdoc115-owned.json"


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_owned(root: Path) -> dict:
    path = root / MANIFEST
    if path.is_symlink():
        raise ValueError("文件归属清单不能是符号链接")
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("文件归属清单格式无效")
    return data


def record_owned(root: Path, paths) -> None:
    root = root.resolve()
    with _lock:
        owned = load_owned(root)
        for path in paths:
            path = Path(path)
            if (
                path.is_symlink()
                or not path.is_file()
                or root not in path.resolve().parents
            ):
                continue
            owned[str(path.resolve().relative_to(root))] = fingerprint(path)
        root.mkdir(parents=True, exist_ok=True)
        temporary = root / (MANIFEST + ".tmp")
        if temporary.is_symlink():
            raise ValueError("文件归属临时清单不能是符号链接")
        temporary.write_text(json.dumps(owned, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, root / MANIFEST)


def owned_unchanged(root: Path, path: Path, owned: dict) -> bool:
    if path.is_symlink() or root not in path.resolve().parents:
        return False
    expected = owned.get(str(path.relative_to(root)))
    return bool(expected and path.is_file() and fingerprint(path) == expected)
