import base64
import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qs, unquote, urlsplit


HTTP_PATTERN = re.compile(r"https?://[^\s\]）)}>，]+", re.IGNORECASE)
MAGNET_PATTERN = re.compile(r"magnet:\?[^\s\]）)}>，]+", re.IGNORECASE)
ED2K_PATTERN = re.compile(r"ed2k://\|file\|.*?\|/", re.IGNORECASE)
YEAR_PATTERN = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")


def link_kind(value: str) -> str:
    """判断资源链接类型。"""
    normalized = str(value or "").strip().lower()
    if normalized.startswith("magnet:?"):
        return "magnet"
    if normalized.startswith("ed2k://"):
        return "ed2k"
    if normalized.startswith(("http://", "https://")):
        return "http"
    return ""


def is_offline_link(value: str) -> bool:
    """判断链接是否必须先通过115离线下载。"""
    return link_kind(value) in {"magnet", "ed2k"}


def supported_link(value: str) -> bool:
    """判断文本是否是插件支持的资源链接。"""
    return bool(link_kind(value))


def extract_source_links(value: str) -> List[Dict[str, Any]]:
    """按文本位置提取 HTTP、磁力和 ED2K 链接。"""
    text = str(value or "").replace("｜", "|")
    # 腾讯智能表的 URL 字段会把磁力链接包装成 https://magnet:?xt=...。
    # 这不是可访问网页，读取目录时恢复成原始协议，避免误当 HTTP 资源。
    text = re.sub(r"https?://(?=magnet:\?)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"https?://(?=ed2k://)", "", text, flags=re.IGNORECASE)
    matches: List[Dict[str, Any]] = []
    for kind, pattern in (
        ("ed2k", ED2K_PATTERN),
        ("magnet", MAGNET_PATTERN),
        ("http", HTTP_PATTERN),
    ):
        for match in pattern.finditer(text):
            link = match.group(0).rstrip(".。;；")
            matches.append(
                {
                    "kind": kind,
                    "url": link,
                    "start": match.start(),
                    "end": match.end(),
                }
            )
    matches.sort(key=lambda item: int(item["start"]))
    return matches


def _magnet_hash(value: str) -> str:
    query = parse_qs(urlsplit(value).query)
    for exact_topic in query.get("xt") or []:
        if not exact_topic.lower().startswith("urn:btih:"):
            continue
        info_hash = exact_topic.rsplit(":", 1)[-1].strip()
        if re.fullmatch(r"[0-9a-fA-F]{40}", info_hash):
            return info_hash.lower()
        if re.fullmatch(r"[A-Z2-7]{32}", info_hash, re.IGNORECASE):
            try:
                return base64.b32decode(info_hash.upper()).hex()
            except Exception:
                return info_hash.lower()
    return ""


def _ed2k_parts(value: str) -> List[str]:
    normalized = str(value or "").strip().replace("｜", "|")
    return normalized.split("|") if normalized.lower().startswith("ed2k://") else []


def offline_info_hash(value: str) -> str:
    """提取115离线任务可查询的哈希，不存在时返回稳定摘要。"""
    if link_kind(value) == "magnet":
        return _magnet_hash(value) or hashlib.sha1(value.encode("utf-8")).hexdigest()
    if link_kind(value) == "ed2k":
        parts = _ed2k_parts(value)
        if len(parts) > 4 and re.fullmatch(r"[0-9a-fA-F]{32}", parts[4].strip()):
            return parts[4].strip().lower()
    return hashlib.sha1(str(value or "").encode("utf-8")).hexdigest()


def offline_file_hint(value: str, fallback_title: str = "未命名") -> Dict[str, Any]:
    """从离线链接推断用于识别和生成 STRM 的占位文件。"""
    kind = link_kind(value)
    name = ""
    size = 0
    if kind == "ed2k":
        parts = _ed2k_parts(value)
        if len(parts) > 3:
            name = unquote(parts[2].strip())
            try:
                size = int(parts[3])
            except (TypeError, ValueError):
                size = 0
    elif kind == "magnet":
        query = parse_qs(urlsplit(value).query)
        name = unquote(str((query.get("dn") or [""])[0])).strip()
    if not name:
        name = str(fallback_title or "未命名").strip()
    if not Path(name).suffix:
        name = f"{name}.mkv"
    identity = offline_info_hash(value)
    return {
        "file_id": f"offline-{identity[:40]}",
        "file_name": name,
        "file_path": f"/{name}",
        "file_size": size,
        "is_dir": False,
    }


def offline_title_year(value: str) -> Dict[str, str]:
    """从磁力显示名或 ED2K 文件名推断标题与年份。"""
    name = Path(offline_file_hint(value)["file_name"]).stem
    leading_group = re.match(r"^\[([^\]]+)\]", name)
    candidate = leading_group.group(1) if leading_group else name
    candidate = re.sub(r"[._]+", " ", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip(" -—|()（）")
    match = YEAR_PATTERN.search(candidate)
    year = match.group(1) if match else ""
    if year:
        candidate = re.sub(rf"[（(]?{re.escape(year)}[）)]?", " ", candidate)
        candidate = re.sub(r"\s+", " ", candidate).strip(" -—|()（）")
    return {"title": candidate or "未命名", "year": year}
