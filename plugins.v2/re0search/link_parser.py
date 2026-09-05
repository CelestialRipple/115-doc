import re
from typing import Any, Dict, List, Optional


_LINK_PATTERNS = (
    re.compile(r"magnet:\?xt=urn:[^\s\"'<>]+", re.IGNORECASE),
    re.compile(r"ed2k://\|file\|.*?\|/", re.IGNORECASE),
    re.compile(
        r"https?://[^\s\"'<>]+/(?:s|share)/[^\s\"'<>]+",
        re.IGNORECASE,
    ),
)


def extract_resource_links(value: Any) -> List[str]:
    """从任意网页响应或可见文本中提取资源链接"""
    chunks: List[str] = []
    _collect_strings(value, chunks)
    result: List[str] = []
    seen = set()
    for chunk in chunks:
        for pattern in _LINK_PATTERNS:
            for match in pattern.finditer(chunk):
                link = _clean_link(match.group(0))
                if link and link not in seen:
                    result.append(link)
                    seen.add(link)
    return result


def resource_link_type(link: str) -> str:
    """判断资源链接类型"""
    lowered = str(link or "").lower()
    if lowered.startswith("magnet:"):
        return "magnet"
    if lowered.startswith("ed2k:"):
        return "ed2k"
    if lowered.startswith(("http://", "https://")):
        if "115" in lowered:
            return "115"
        return "http"
    return "unknown"


def _collect_strings(value: Any, output: List[str]) -> None:
    """递归收集对象内的字符串"""
    if isinstance(value, str):
        output.append(value)
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect_strings(item, output)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _collect_strings(item, output)


def _clean_link(link: str) -> str:
    """移除网页文本常见的尾部标点"""
    cleaned = str(link or "").strip().rstrip(".,;:，。；：)]}）】")
    if cleaned.startswith("ed2k://") and not cleaned.endswith("|/"):
        return ""
    return cleaned


def public_resource(resource: Dict[str, Any]) -> Dict[str, Any]:
    """过滤浏览器资源结果中的内部字段"""
    allowed = {
        "slug",
        "href",
        "title",
        "provider",
        "size",
        "resolution",
        "posted_at",
        "unlock_points",
        "is_free",
        "already_owned",
    }
    return {key: value for key, value in resource.items() if key in allowed}


def extract_resource_rows(value: Any) -> List[Dict[str, Any]]:
    """从嵌套响应中提取当前 RE0 资源对象"""
    found: List[Dict[str, Any]] = []
    seen = set()

    def visit(item: Any) -> None:
        """递归检查响应节点"""
        if isinstance(item, dict):
            normalized = _normalize_resource_row(item)
            if normalized:
                key = normalized["slug"]
                if key not in seen:
                    seen.add(key)
                    found.append(normalized)
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return found


def _normalize_resource_row(value: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把站点响应中的资源对象转成稳定字段"""
    raw_slug = str(value.get("slug") or "").strip()
    if not raw_slug or not re.fullmatch(r"[A-Za-z0-9._-]+", raw_slug):
        return None
    provider = str(value.get("website") or value.get("provider") or "").strip()
    resource_signals = {
        "unlock_points",
        "is_unlocked",
        "share_size",
        "website",
        "provider",
        "validate_status",
    }
    if not resource_signals.intersection(value):
        return None
    route_slug = f"{provider}/{raw_slug}" if provider in {"115", "123", "189"} else raw_slug
    raw_points = value.get("unlock_points")
    try:
        points = int(raw_points) if raw_points is not None else None
    except (TypeError, ValueError):
        points = None
    resolution = value.get("video_resolution") or value.get("resolution") or ""
    if isinstance(resolution, list):
        resolution = " / ".join(str(item) for item in resolution if item)
    return {
        "slug": route_slug,
        "href": f"/resource/{route_slug}",
        "title": str(value.get("title") or value.get("remark") or "").strip(),
        "provider": provider,
        "size": str(value.get("share_size") or value.get("size") or "").strip(),
        "resolution": str(resolution or "").strip(),
        "posted_at": str(value.get("created_at") or value.get("posted_at") or "").strip(),
        "unlock_points": points,
        "is_free": points == 0,
        "already_owned": bool(value.get("is_unlocked")),
    }
