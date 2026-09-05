"""Bounded, read-only PanSou and BT4G search clients."""

import base64
import hashlib
import re
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import requests


class ProviderError(Exception):
    pass


class ChallengeRequired(ProviderError):
    pass


def http_url(value):
    try:
        parsed = urlsplit(str(value).strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        if parsed.username or parsed.password or any(ord(c) < 32 for c in value):
            return ""
        _ = parsed.port
        return parsed.geturl()
    except (ValueError, TypeError):
        return ""


def clean_link(value):
    value = str(value or "").strip()[:8192]
    if value.startswith("magnet:?"):
        query = parse_qs(urlsplit(value).query)
        for xt in query.get("xt", []):
            match = re.fullmatch(r"urn:btih:([a-fA-F0-9]{40}|[A-Z2-7a-z]{32})", xt)
            if match:
                infohash = match[1]
                if len(infohash) == 32:
                    infohash = base64.b32decode(infohash.upper()).hex()
                # Only retain the hash and display name, never remote file or webseed URLs.
                return "magnet:?" + urlencode(
                    {
                        "xt": "urn:btih:" + infohash.lower(),
                        "dn": query.get("dn", [""])[0][:500],
                    }
                )
        return ""
    if re.fullmatch(r"ed2k://\|file\|[^|\r\n]+\|\d+\|[a-fA-F0-9]{32}\|/?", value):
        return value
    return http_url(value)


def link_key(value):
    if value.startswith("magnet:?"):
        return parse_qs(urlsplit(value).query)["xt"][0].lower()
    return value


@dataclass(frozen=True)
class Resource:
    title: str
    url: str
    source: str
    cloud: str = "others"
    password: str = ""
    size: int = 0
    seeders: int = 0
    page_url: str = ""

    @property
    def id(self):
        return hashlib.sha256(link_key(self.url).encode()).hexdigest()[:32]

    def to_dict(self):
        return {**asdict(self), "id": self.id}


def dedupe(items, limit=100):
    unique = {}
    for item in items:
        if item.id not in unique:
            unique[item.id] = item
        if len(unique) >= limit:
            break
    return list(unique.values())


def normalize_pansou(payload, limit=100, allowed_clouds=None):
    if not isinstance(payload, dict):
        raise ProviderError("PanSou 返回格式不正确")
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]
    merged = payload.get("merged_by_type")
    entries = []
    if isinstance(merged, dict):
        for cloud, rows in merged.items():
            if isinstance(rows, list):
                entries.extend((str(cloud), row) for row in rows[:500])
    elif isinstance(payload.get("results"), list):
        for row in payload["results"][:500]:
            if not isinstance(row, dict):
                continue
            for link in (row.get("links") or [])[:100]:
                if isinstance(link, dict):
                    entries.append(
                        (
                            link.get("type", "others"),
                            {
                                **row,
                                **link,
                                "note": link.get("work_title") or row.get("title"),
                                "source": row.get("channel", ""),
                            },
                        )
                    )
    elif "total" not in payload:
        raise ProviderError("PanSou 响应缺少搜索结果字段，请检查 API 地址")
    items = []
    for cloud, row in entries:
        if not isinstance(row, dict):
            continue
        url = clean_link(row.get("url"))
        if not url:
            continue
        cloud = (
            "magnet"
            if url.startswith("magnet:")
            else "ed2k"
            if url.startswith("ed2k:")
            else cloud
        )
        if allowed_clouds is not None and cloud not in allowed_clouds:
            continue
        title = str(
            row.get("note") or row.get("title") or row.get("work_title") or "未命名资源"
        ).strip()[:500]
        items.append(
            Resource(
                title,
                url,
                "PanSou · " + str(row.get("source") or "聚合搜索")[:150],
                cloud,
                str(row.get("password") or "")[:64],
            )
        )
    return dedupe(items, limit)


def read_response(
    session, method, url, *, timeout=20, same_origin_redirects=0, **kwargs
):
    """Reject large responses and redirects before forwarding credentials."""
    import time

    deadline = time.monotonic() + timeout
    with session.request(
        method,
        url,
        timeout=(min(timeout, 8), timeout),
        stream=True,
        allow_redirects=False,
        **kwargs,
    ) as response:
        if (
            response.status_code in {301, 302, 303, 307, 308}
            and method == "GET"
            and same_origin_redirects
        ):
            target = http_url(urljoin(url, response.headers.get("Location", "")))
            if (
                target
                and target != url
                and urlsplit(target).netloc == urlsplit(url).netloc
                and urlsplit(target).scheme == urlsplit(url).scheme
            ):
                response.close()
                return read_response(
                    session,
                    method,
                    target,
                    timeout=timeout,
                    same_origin_redirects=same_origin_redirects - 1,
                    **kwargs,
                )
        content = bytearray()
        for chunk in response.iter_content(65536):
            if time.monotonic() > deadline:
                raise ProviderError("搜索来源响应超时，请稍后重试")
            content.extend(chunk)
            if len(content) > 4 * 1024 * 1024:
                raise ProviderError("搜索响应超过 4 MiB 限制")
        text = content.decode("utf-8", errors="replace")
        is_challenge = (
            response.headers.get("cf-mitigated") == "challenge"
            or bool(re.search(r"<title[^>]*>\s*(?:just a moment|请稍候)", text, re.I))
            or (response.status_code in {403, 503} and "cf-chl-" in text.lower())
        )
        if is_challenge:
            raise ChallengeRequired("搜索服务要求浏览器验证，请检查 PanSou 服务地址")
        if not 200 <= response.status_code < 300:
            raise ProviderError(
                f"搜索服务返回 HTTP {response.status_code}，请检查地址、认证或稍后重试"
            )
        return text


class PanSouClient:
    def __init__(self, config):
        self.config = dict(config)

    def search(self, keyword, refresh=False):
        import json

        base = http_url(self.config.get("pansou_url", ""))
        if not base:
            raise ProviderError("请配置有效的 PanSou 服务地址")
        base = base.rstrip("/")
        endpoint = (
            base
            if base.endswith("/api/search")
            else base + ("/search" if base.endswith("/api") else "/api/search")
        )
        headers = {"Accept": "application/json"}
        token = str(self.config.get("pansou_token") or "").strip()
        timeout = int(self.config.get("timeout", 20))
        with requests.Session() as session:
            session.trust_env = (
                False  # NAS API traffic must not inherit external proxies.
            )
            if not token and self.config.get("pansou_username"):
                login = endpoint.removesuffix("/search") + "/auth/login"
                data = json.loads(
                    read_response(
                        session,
                        "POST",
                        login,
                        timeout=timeout,
                        json={
                            "username": self.config["pansou_username"],
                            "password": self.config.get("pansou_password", ""),
                        },
                    )
                )
                token = str(data.get("token") or "")
                if not token:
                    raise ProviderError("PanSou 登录未返回访问令牌")
            if token:
                headers["Authorization"] = "Bearer " + token
            body = {
                "kw": keyword,
                "res": "merge",
                "src": "all",
                "cloud_types": ["115", "magnet"],
            }
            if refresh:
                body["refresh"] = True
            for field in ("plugins", "channels"):
                raw = self.config.get(field)
                values = (
                    raw
                    if isinstance(raw, list)
                    else re.split(r"[,，\s]+", str(raw or ""))
                )
                values = [str(value).strip() for value in values if str(value).strip()]
                if values:
                    body[field] = values[:50]
            data = json.loads(
                read_response(
                    session,
                    "POST",
                    endpoint,
                    timeout=timeout,
                    headers=headers,
                    json=body,
                )
            )
        return normalize_pansou(
            data, int(self.config.get("limit", 100)), {"115", "magnet"}
        )


def bt4g_search_url(base, keyword, page=0):
    return base.rstrip("/") + "/search?" + urlencode({"q": keyword, "p": page + 1})
