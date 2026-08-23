import hashlib
from pathlib import Path
from typing import Union
from urllib.parse import parse_qs, urlencode, urlsplit


MARKER_PARAMETER = "x.td115"


def build_download_marker(resource_id: str) -> str:
    """生成 MoviePilot 可以传入下载模块的磁力格式资源标记。"""
    info_hash = hashlib.sha1(
        f"tencentdoc115:{resource_id}".encode("utf-8")
    ).hexdigest()
    return (
        f"magnet:?xt=urn:btih:{info_hash}&"
        f"{urlencode({MARKER_PARAMETER: resource_id})}"
    )


def parse_download_marker(content: Union[Path, str, bytes]) -> str:
    """从插件专用磁力标记中提取资源 ID。"""
    if isinstance(content, bytes):
        try:
            value = content.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    else:
        value = str(content or "")
    if not value.startswith("magnet:"):
        return ""
    query = parse_qs(urlsplit(value).query)
    return str((query.get(MARKER_PARAMETER) or [""])[0]).strip()
