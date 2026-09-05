"""Bounded, read-only PanSou and BT4G search clients."""

import base64
import hashlib
import re
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup


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


def normalize_pansou(payload, limit=100):
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
            raise ChallengeRequired(
                "BT4G 需要浏览器真人验证，请在插件搜索页打开 BT4G 并带回结果"
            )
        if not 200 <= response.status_code < 300:
            raise ProviderError(
                f"搜索服务返回 HTTP {response.status_code}，请检查地址、认证或稍后重试"
            )
        return text


class PanSouClient:
    def __init__(self, config):
        self.config = dict(config)

    def search(self, keyword):
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
            body = {"kw": keyword, "res": "merge", "src": "all"}
            for field in ("plugins", "cloud_types", "channels"):
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
        return normalize_pansou(data, int(self.config.get("limit", 100)))


def bt4g_search_url(base, keyword, page=0):
    return base.rstrip("/") + "/search?" + urlencode({"q": keyword, "p": page + 1})


def parse_size(text):
    match = re.search(
        r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(TiB|GiB|MiB|KiB|TB|GB|MB|KB|B)\b", text, re.I
    )
    if not match:
        return 0
    exponent = {"B": 0, "KB": 1, "MB": 2, "GB": 3, "TB": 4}
    return int(
        float(match[1].replace(",", ""))
        * 1024 ** exponent[match[2].upper().replace("I", "")]
    )


def parse_bt4g(html, base, limit=100):
    """Extract visible result links; never infer an infohash from a detail-page ID."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    detail_title = soup.select_one("h1.notion-detail-title")
    if detail_title:
        for anchor in soup.select(".notion-btn-group a[href]"):
            parsed = urlsplit(urljoin(base, anchor["href"]))
            match = re.fullmatch(r"/hash/([a-fA-F0-9]{40})", parsed.path)
            if parsed.hostname == "downloadtorrentfile.com" and match:
                title = detail_title.get_text(" ", strip=True)[:500]
                # This is the explicit hash in the site's download link, not its
                # opaque /magnet/ page identifier. No visit to the external host.
                magnet = clean_link(
                    "magnet:?" + urlencode({"xt": "urn:btih:" + match[1], "dn": title})
                )
                items.append(Resource(title, magnet, "BT4G", "magnet", page_url=base))
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        url = clean_link(href) if href.startswith("magnet:") else ""
        if not url:
            continue
        row = (
            anchor.find_parent(
                class_=re.compile(
                    r"(?:^|\s)(?:result|search-result|torrent|list-group-item|notion-list-item)(?:\s|$)"
                )
            )
            or anchor.find_parent("li")
            or anchor.parent
        )
        heading = row.find(["h3", "h4", "h5", "h6"]) if row else None
        dn = parse_qs(urlsplit(url).query).get("dn", [""])[0]
        title = (
            heading.get_text(" ", strip=True)
            if heading
            else dn or anchor.get_text(" ", strip=True)
        )[:500]
        page_url = base
        if heading and heading.find("a", href=True):
            candidate = urljoin(base, heading.find("a")["href"])
            if urlsplit(candidate).netloc == urlsplit(base).netloc:
                page_url = candidate
        items.append(
            Resource(
                title or "BT4G 资源",
                url,
                "BT4G",
                "magnet",
                size=parse_size(row.get_text(" ", strip=True)),
                page_url=page_url,
            )
        )
    # BT4G commonly exposes magnets on detail pages only. Preserve the verified
    # same-origin detail links as navigable results instead of fabricating hashes.
    for heading in soup.select(
        ".notion-list-item-title a[href], h3 a[href], h4 a[href], h5 a[href]"
    ):
        url = urljoin(base, heading["href"])
        parts = urlsplit(url)
        if parts.netloc != urlsplit(base).netloc or not re.fullmatch(
            r"/(?:magnet|hash|torrent)/[a-zA-Z0-9_-]+/?", parts.path
        ):
            continue
        title = heading.get_text(" ", strip=True)[:500]
        if title and not any(item.title == title for item in items):
            row = heading.find_parent(class_="notion-list-item")
            total = row.select_one(".red-pill") if row else None
            seeds = row.select_one(".notion-seeders") if row else None
            seeders = re.sub(r"\D", "", seeds.get_text()) if seeds else ""
            items.append(
                Resource(
                    title,
                    url,
                    "BT4G",
                    "bt4g",
                    size=parse_size(total.get_text()) if total else 0,
                    seeders=int(seeders[:8] or 0),
                    page_url=url,
                )
            )
    if not items and not any(
        text in soup.get_text(" ", strip=True).lower()
        for text in ("no results", "no result", "not found", "没有找到", "0 results")
    ):
        raise ProviderError(
            "BT4G 页面没有可识别的结果结构，请用浏览器打开并带回当前页结果"
        )
    return dedupe(items, limit)


class BT4GClient:
    def __init__(self, config):
        self.config = dict(config)

    def search(self, keyword, page=0):
        base = http_url(self.config.get("bt4g_url", "https://bt4gprx.com"))
        if not base:
            raise ProviderError("BT4G 地址无效")
        with requests.Session() as session:
            session.trust_env = False
            proxy = http_url(self.config.get("bt4g_proxy", ""))
            if proxy:
                session.proxies = {"http": proxy, "https": proxy}
            html = read_response(
                session,
                "GET",
                bt4g_search_url(base, keyword, page),
                timeout=int(self.config.get("timeout", 20)),
                same_origin_redirects=3,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
            )
        return parse_bt4g(html, base, int(self.config.get("limit", 100)))
