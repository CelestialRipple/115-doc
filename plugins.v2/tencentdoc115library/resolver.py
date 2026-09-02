import hashlib
import re
from pathlib import Path
from threading import Lock, RLock
from time import monotonic, sleep
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

try:
    from app.sdk.config import settings
    from app.sdk.logging import logger
    from app.sdk.network import RequestUtils
    from app.sdk.plugins import PluginManager
except ImportError:
    from app.core.config import settings
    from app.core.plugin import PluginManager
    from app.log import logger
    from app.utils.http import RequestUtils

from .store import CatalogStore, utc_now

# 115 分享临时下载地址通常约两小时有效，留出足够的提前量重新获取
DIRECT_URL_CACHE_TTL_SECONDS = 100 * 60
DIRECT_URL_CACHE_MAX_SIZE = 2048

DEFAULT_VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".flv",
    ".iso",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".rmvb",
    ".ts",
    ".webm",
    ".wmv",
}


class ShareResolutionError(RuntimeError):
    """
    可直接映射到播放 HTTP 响应的 115 分享解析错误

    Attributes:
        status_code: 建议返回给播放器的 HTTP 状态码
        retryable: 是否适合稍后自动重试
    """

    def __init__(self, message: str, status_code: int = 502, retryable: bool = True):
        """
        初始化分享解析错误

        :param message (str): 用户可理解的错误信息
        :param status_code (int): HTTP 状态码
        :param retryable (bool): 是否适合重试
        """
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class ShareResolver:
    """
    播放时按需遍历 115 分享并获取临时下载地址

    电影只缓存选中的文件 ID，临时下载地址每次播放重新获取，避免持久化过期 URL
    """

    def __init__(
        self,
        store: CatalogStore,
        config_provider: Callable[[], Dict[str, Any]],
    ):
        """
        初始化分享解析器

        :param store (CatalogStore): 目录存储
        :param config_provider (Callable): 插件配置读取函数
        """
        self.store = store
        self.config_provider = config_provider
        self._client = None
        self._client_cookie_hash = ""
        self._client_lock = RLock()
        self._rate_lock = Lock()
        self._last_request_at = 0.0
        self._resource_locks: Dict[str, Lock] = {}
        self._resource_locks_guard = Lock()
        self._url_cache: Dict[Tuple[str, str, str], Tuple[str, float]] = {}
        self._url_cache_lock = Lock()
        self._anonymous_request = RequestUtils(
            proxies=settings.PROXY,
            timeout=30,
        )

    def _cookie(self) -> str:
        config = self.config_provider()
        cookie = ""
        if config.get("reuse_p115_cookie", True):
            p115_config = PluginManager().get_plugin_config("P115StrmHelper") or {}
            cookie = str(p115_config.get("cookies") or "").strip()
        if not cookie:
            cookie = str(config.get("p115_cookie") or "").strip()
        if not cookie:
            raise ShareResolutionError(
                "未配置 115 Cookie，且未能复用 115网盘STRM助手配置",
                status_code=401,
                retryable=False,
            )
        return cookie

    def _get_client(self) -> Any:
        """获取仅用于生成下载地址的账号客户端。"""
        cookie = self._cookie()
        cookie_hash = hashlib.sha256(cookie.encode("utf-8")).hexdigest()
        with self._client_lock:
            if self._client is not None and cookie_hash == self._client_cookie_hash:
                return self._client
            try:
                from p115client import P115Client

                self._client = P115Client(cookie)
            except Exception as error:
                raise ShareResolutionError(
                    f"创建 115 客户端失败：{str(error)}",
                    status_code=401,
                    retryable=False,
                ) from error
            self._client_cookie_hash = cookie_hash
            return self._client

    def _anonymous_share_snap(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """通过公开分享接口枚举文件，不携带用户 Cookie。"""
        response = self._anonymous_request.get_res(
            url="https://webapi.115.com/share/snap",
            params=payload,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 MoviePilot TencentDoc115Library",
            },
        )
        if not response:
            raise ShareResolutionError(
                "115 公开分享目录接口无响应",
                status_code=502,
                retryable=True,
            )
        try:
            if response.status_code == 429:
                raise ShareResolutionError(
                    "115 公开分享目录接口请求过于频繁",
                    status_code=429,
                    retryable=True,
                )
            if response.status_code != 200:
                raise ShareResolutionError(
                    f"115 公开分享目录接口异常：HTTP {response.status_code}",
                    status_code=502,
                    retryable=response.status_code >= 500,
                )
            payload_data = response.json()
        except ValueError as error:
            raise ShareResolutionError(
                "115 公开分享目录接口返回了无效数据",
                status_code=502,
                retryable=True,
            ) from error
        finally:
            response.close()
        if not isinstance(payload_data, dict):
            raise ShareResolutionError(
                "115 公开分享目录接口返回格式错误",
                status_code=502,
                retryable=True,
            )
        return payload_data

    def _call_with_retry(self, operation: Callable[[], Any]) -> Any:
        """按用户配置串行限速调用 115，并仅重试临时错误。"""
        config = self.config_provider()
        interval = min(max(float(config.get("request_interval") or 0.5), 0.1), 30)
        retry_count = min(max(int(config.get("request_retries") or 4), 0), 10)
        last_error: Optional[ShareResolutionError] = None
        for attempt in range(retry_count + 1):
            with self._rate_lock:
                remaining = interval - (monotonic() - self._last_request_at)
                if remaining > 0:
                    sleep(remaining)
                try:
                    response = operation()
                except ShareResolutionError as error:
                    response = None
                    last_error = error
                    if not error.retryable:
                        raise
                except Exception as error:
                    response = None
                    last_error = ShareResolutionError(
                        f"115 请求失败：{str(error)}",
                        status_code=502,
                        retryable=True,
                    )
                finally:
                    self._last_request_at = monotonic()
            if response is not None:
                if not isinstance(response, dict) or response.get("state", True):
                    return response
                last_error = self._error_from_response(response)
                if not last_error.retryable:
                    raise last_error
            if attempt < retry_count:
                sleep(min(2**attempt, 8))
        raise last_error or ShareResolutionError("115 请求失败")

    @staticmethod
    def parse_share_url(share_url: str) -> Tuple[str, str]:
        """
        解析 115 分享码和访问码

        :param share_url (str): 115 分享地址

        :return Tuple: 分享码和访问码
        """
        parsed = urlsplit(str(share_url or ""))
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ShareResolutionError(
                "无法从资源链接中识别 115 分享码",
                status_code=422,
                retryable=False,
            )
        try:
            from p115client.util import share_extract_payload

            payload = share_extract_payload(share_url)
            share_code = str(payload.get("share_code") or "").strip()
            receive_code = str(payload.get("receive_code") or "").strip()
            if share_code:
                return share_code, receive_code
        except Exception:
            pass
        match = re.search(r"/(?:s|share)/([^/?#&]+)", parsed.path, re.IGNORECASE)
        share_code = match.group(1) if match else ""
        query = parse_qs(parsed.query)
        receive_code = str(
            (query.get("password") or query.get("receive_code") or [""])[0]
        ).strip()
        if not share_code:
            raise ShareResolutionError(
                "无法从资源链接中识别 115 分享码",
                status_code=422,
                retryable=False,
            )
        return share_code, receive_code

    @staticmethod
    def _error_from_response(response: Dict[str, Any]) -> ShareResolutionError:
        message = str(
            response.get("error")
            or response.get("message")
            or response.get("msg")
            or "115 分享接口返回失败"
        )
        lowered = message.lower()
        if any(keyword in message for keyword in ("提取码", "访问码", "密码")):
            return ShareResolutionError(
                f"115 分享访问码错误：{message}",
                status_code=403,
                retryable=False,
            )
        if any(keyword in message for keyword in ("取消", "失效", "不存在", "过期")):
            return ShareResolutionError(
                f"115 分享已失效：{message}",
                status_code=410,
                retryable=False,
            )
        if "405" in lowered or "频繁" in message or "限流" in message:
            return ShareResolutionError(
                f"115 请求过于频繁：{message}",
                status_code=429,
                retryable=True,
            )
        return ShareResolutionError(
            f"115 分享解析失败：{message}",
            status_code=502,
            retryable=True,
        )

    @staticmethod
    def _normalize_item(item: Dict[str, Any], parent_path: str) -> Dict[str, Any]:
        normalized: Dict[str, Any] = {}
        try:
            from p115client.tool.attr import normalize_attr

            normalized = dict(normalize_attr(dict(item)))
        except Exception:
            normalized = dict(item)
        file_id = str(
            normalized.get("id")
            or normalized.get("file_id")
            or item.get("fid")
            or item.get("cid")
            or ""
        )
        name = str(
            normalized.get("name") or normalized.get("file_name") or item.get("n") or ""
        )
        size_value = (
            normalized.get("size") or normalized.get("file_size") or item.get("s") or 0
        )
        try:
            size = int(size_value)
        except (TypeError, ValueError):
            size = 0
        is_dir = bool(normalized.get("is_dir"))
        if "fc" in item:
            is_dir = str(item.get("fc")) == "0"
        elif item.get("fid"):
            is_dir = False
        elif item.get("cid") and not item.get("sha"):
            is_dir = True
        path = f"{parent_path}/{name}" if parent_path else f"/{name}"
        return {
            "file_id": file_id,
            "file_name": name,
            "file_path": path,
            "file_size": size,
            "is_dir": is_dir,
        }

    def list_video_files(self, share_url: str) -> List[Dict[str, Any]]:
        """
        递归列出分享中的视频文件

        :param share_url (str): 115 分享地址

        :return List: 标准化视频文件列表
        """
        config = self.config_provider()
        extensions = {
            (
                extension.strip().lower()
                if extension.strip().startswith(".")
                else f".{extension.strip().lower()}"
            )
            for extension in str(
                config.get("video_extensions")
                or ",".join(sorted(DEFAULT_VIDEO_EXTENSIONS))
            )
            .replace("，", ",")
            .split(",")
            if extension.strip()
        }
        max_files = min(max(int(config.get("share_max_files") or 5000), 1), 50000)
        max_depth = min(max(int(config.get("share_max_depth") or 20), 1), 100)
        page_size = min(max(int(config.get("share_page_size") or 1000), 1), 1000)
        share_code, receive_code = self.parse_share_url(share_url)
        pending: List[Tuple[int, str, int]] = [(0, "", 0)]
        videos: List[Dict[str, Any]] = []
        visited_directories = set()
        while pending:
            directory_id, parent_path, depth = pending.pop(0)
            if directory_id in visited_directories:
                continue
            visited_directories.add(directory_id)
            if depth > max_depth:
                raise ShareResolutionError(
                    f"115 分享目录层级超过限制 {max_depth}",
                    status_code=422,
                    retryable=False,
                )
            offset = 0
            while True:
                response = self._call_with_retry(
                    lambda: self._anonymous_share_snap(
                        {
                            "share_code": share_code,
                            "receive_code": receive_code,
                            "cid": directory_id,
                            "limit": page_size,
                            "offset": offset,
                        }
                    )
                )
                if not isinstance(response, dict) or not response.get("state"):
                    raise self._error_from_response(
                        response if isinstance(response, dict) else {}
                    )
                data = response.get("data") or {}
                items = data.get("list") or []
                total = int(data.get("count") or len(items))
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    normalized = self._normalize_item(item, parent_path)
                    if not normalized["file_id"] or not normalized["file_name"]:
                        continue
                    if normalized["is_dir"]:
                        pending.append(
                            (
                                int(normalized["file_id"]),
                                normalized["file_path"],
                                depth + 1,
                            )
                        )
                        continue
                    if Path(normalized["file_name"]).suffix.lower() in extensions:
                        videos.append(normalized)
                        if len(videos) > max_files:
                            raise ShareResolutionError(
                                f"115 分享视频数量超过限制 {max_files}",
                                status_code=422,
                                retryable=False,
                            )
                offset += len(items)
                if not items or offset >= total:
                    break
        if not videos:
            raise ShareResolutionError(
                "115 分享目录内未发现可播放视频",
                status_code=404,
                retryable=False,
            )
        return videos

    @staticmethod
    def choose_movie_file(files: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        按文件体积选择电影主视频

        :param files (List): 分享视频文件列表

        :return Dict: 选中的主视频
        """
        if not files:
            raise ShareResolutionError(
                "115 分享目录内未发现可播放视频",
                status_code=404,
                retryable=False,
            )
        return max(files, key=lambda item: int(item.get("file_size") or 0))

    def _download_url(
        self,
        share_url: str,
        file_id: str,
        user_agent: str = "",
    ) -> str:
        share_code, receive_code = self.parse_share_url(share_url)
        client = self._get_client()
        try:
            request_kwargs: Dict[str, Any] = {"app": "android"}
            if user_agent:
                request_kwargs["headers"] = {"User-Agent": user_agent}
            result = self._call_with_retry(
                lambda: client.share_download_url(
                    {
                        "share_code": share_code,
                        "receive_code": receive_code,
                        "file_id": int(file_id),
                    },
                    **request_kwargs,
                )
            )
        except Exception as error:
            message = str(error)
            if any(keyword in message for keyword in ("提取码", "访问码", "密码")):
                raise ShareResolutionError(
                    f"115 分享访问码错误：{message}",
                    status_code=403,
                    retryable=False,
                ) from error
            raise ShareResolutionError(
                f"获取 115 分享下载地址失败：{message}",
                status_code=502,
                retryable=True,
            ) from error
        url = str(result or "").strip()
        if not url:
            raise ShareResolutionError(
                "115 分享下载接口未返回可播放地址",
                status_code=502,
                retryable=True,
            )
        return url

    def _url_cache_ttl(self) -> float:
        """读取直链缓存有效期，0 表示禁用缓存。"""
        raw = self.config_provider().get("direct_url_cache_ttl")
        if raw is None or raw == "":
            return float(DIRECT_URL_CACHE_TTL_SECONDS)
        try:
            ttl = float(raw)
        except (TypeError, ValueError):
            ttl = float(DIRECT_URL_CACHE_TTL_SECONDS)
        return min(max(ttl, 0), 2 * 60 * 60)

    def _cached_url(
        self,
        share_url: str,
        file_id: str,
        user_agent: str,
    ) -> str:
        if self._url_cache_ttl() <= 0:
            return ""
        key = (share_url, str(file_id), user_agent)
        now = monotonic()
        with self._url_cache_lock:
            cached = self._url_cache.get(key)
            if cached and cached[1] > now:
                return cached[0]
            if cached:
                self._url_cache.pop(key, None)
        return ""

    def _store_url(
        self,
        share_url: str,
        file_id: str,
        user_agent: str,
        url: str,
    ) -> None:
        ttl = self._url_cache_ttl()
        if ttl <= 0 or not url:
            return
        now = monotonic()
        with self._url_cache_lock:
            expired_keys = [
                key
                for key, (_, expires_at) in self._url_cache.items()
                if expires_at <= now
            ]
            for key in expired_keys:
                self._url_cache.pop(key, None)
            self._url_cache[(share_url, str(file_id), user_agent)] = (
                url,
                now + ttl,
            )
            while len(self._url_cache) > DIRECT_URL_CACHE_MAX_SIZE:
                self._url_cache.pop(next(iter(self._url_cache)))

    def invalidate_file_url(
        self,
        share_url: str,
        file_id: str,
        user_agent: str = "",
    ) -> None:
        """丢弃某个文件的直链缓存。"""
        with self._url_cache_lock:
            if user_agent:
                self._url_cache.pop((share_url, str(file_id), user_agent), None)
                return
            for key in [
                key
                for key in self._url_cache
                if key[0] == share_url and key[1] == str(file_id)
            ]:
                self._url_cache.pop(key, None)

    def clear_url_cache(self) -> None:
        """清空全部 115 临时直链缓存。"""
        with self._url_cache_lock:
            self._url_cache.clear()

    def resolve_file_url(
        self,
        share_url: str,
        file_id: str,
        user_agent: str = "",
    ) -> str:
        """使用账户 Cookie 为一个已知分享文件生成临时下载地址。"""
        cached = self._cached_url(share_url, file_id, user_agent)
        if cached:
            return cached
        url = self._download_url(share_url, file_id, user_agent)
        self._store_url(share_url, file_id, user_agent, url)
        return url

    def resolve(
        self,
        resource_id: str,
        file_id: Optional[str] = None,
        user_agent: str = "",
    ) -> str:
        """
        解析资源并返回临时播放地址

        :param resource_id (str): 插件资源 ID
        :param file_id (str): 电视剧已展开的 115 文件 ID
        :param user_agent (str): 播放器 User-Agent

        :return str: 115 临时下载地址
        """
        resource = self.store.get_resource(resource_id)
        if not resource or resource.get("status") == "removed":
            raise ShareResolutionError(
                "资源不存在或已从腾讯文档移除",
                status_code=404,
                retryable=False,
            )
        known_file_id = (
            str(file_id or "").strip()
            or str(resource.get("resolved_file_id") or "").strip()
        )
        if known_file_id:
            cached = self._cached_url(
                resource["share_url"],
                known_file_id,
                user_agent,
            )
            if cached:
                return cached
        with self._resource_locks_guard:
            lock = self._resource_locks.setdefault(resource_id, Lock())
        with lock:
            try:
                target_file_id = str(file_id or "").strip()
                if target_file_id:
                    resource_file = self.store.get_resource_file(
                        resource_id,
                        target_file_id,
                    )
                    if not resource_file:
                        raise ShareResolutionError(
                            "STRM 指定的剧集文件不存在",
                            status_code=404,
                            retryable=False,
                        )
                else:
                    target_file_id = str(resource.get("resolved_file_id") or "").strip()
                    if not target_file_id:
                        selected = self.choose_movie_file(
                            self.list_video_files(resource["share_url"])
                        )
                        target_file_id = selected["file_id"]
                        self.store.update_resource_status(
                            resource_id,
                            resource.get("status") or "ready",
                            resolved_file_id=target_file_id,
                            resolved_file_name=selected["file_name"],
                            resolved_at=utc_now(),
                        )
                return self.resolve_file_url(
                    resource["share_url"],
                    target_file_id,
                    user_agent,
                )
            except ShareResolutionError as error:
                status = "invalid_share" if not error.retryable else "ready"
                self.store.update_resource_status(resource_id, status, str(error))
                logger.warning(f"115 分享资源解析失败：{resource_id} - {str(error)}")
                raise
