import asyncio
import gzip
import json
import re
import zlib
from pathlib import Path
from secrets import compare_digest
from threading import Lock, Thread
from time import monotonic
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlsplit

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

try:
    from app.sdk.logging import logger
except ImportError:
    from app.log import logger

from .resolver import ShareResolutionError, ShareResolver


# Emby MediaSourceId 查询流程参考 MediaWarp
# https://github.com/AkimioJR/MediaWarp
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
PLAY_PATH_PATTERN = re.compile(
    r"/api/v1/plugin/TencentDoc115Library/play/([^/?]+)",
    re.IGNORECASE,
)
STREAM_ITEM_PATTERN = re.compile(
    r"/(?:emby/)?(?:Videos|Items)/([^/]+)/(?:stream(?:\.[^/?]+)?|Download)$",
    re.IGNORECASE,
)
PLAYBACK_INFO_PATTERN = re.compile(
    r"/(?:emby/)?Items/([^/]+)/PlaybackInfo$",
    re.IGNORECASE,
)
PLAYBACK_PATH_CACHE_TTL_SECONDS = 10 * 60
PLAYBACK_PATH_CACHE_MAX_SIZE = 2048


class DirectPlayGateway:
    """
    在独立端口反向代理 Emby，并将本插件 STRM 播放改为115直链302

    Attributes:
        config_provider: 插件配置读取函数
        resolver: 115 分享临时直链解析器
    """

    def __init__(
        self,
        config_provider: Callable[[], Dict[str, Any]],
        resolver: ShareResolver,
    ) -> None:
        """
        初始化直链网关

        :param config_provider (Callable): 插件配置读取函数
        :param resolver (ShareResolver): 115 分享解析器
        """
        self.config_provider = config_provider
        self.resolver = resolver
        self._thread: Optional[Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._runner: Optional[web.AppRunner] = None
        self._session: Optional[ClientSession] = None
        self._lock = Lock()
        self._state = "disabled"
        self._last_error = ""
        self._item_paths: Dict[str, str] = {}
        self._unmanaged_items: Dict[str, str] = {}
        self._playback_paths: Dict[
            Tuple[str, str, str, str],
            Tuple[str, float],
        ] = {}
        self._cache_lock = Lock()

    @staticmethod
    def parse_path_mappings(value: str) -> List[Tuple[str, str]]:
        """
        解析 Emby 路径到 MoviePilot 路径的逐行映射

        :param value (str): 每行使用竖线分隔的路径映射

        :return List: 路径映射列表
        """
        mappings: List[Tuple[str, str]] = []
        for line in str(value or "").splitlines():
            source, separator, target = line.partition("|")
            if separator and source.strip() and target.strip():
                mappings.append(
                    (
                        source.strip().rstrip("/"),
                        target.strip().rstrip("/"),
                    )
                )
        return mappings

    @classmethod
    def local_strm_path(cls, emby_path: str, mapping_text: str) -> Path:
        """
        将 Emby 返回的 STRM 路径转换为 MoviePilot 容器内路径

        :param emby_path (str): Emby 媒体项路径
        :param mapping_text (str): 路径映射配置

        :return Path: MoviePilot 容器内 STRM 路径
        """
        for source, target in cls.parse_path_mappings(mapping_text):
            if emby_path == source or emby_path.startswith(source + "/"):
                suffix = emby_path[len(source) :].lstrip("/")
                return Path(target) / suffix
        return Path(emby_path)

    @staticmethod
    def _prefixes(config: Dict[str, Any]) -> List[str]:
        return [
            line.strip().rstrip("/")
            for line in str(config.get("emby_strm_paths") or "").splitlines()
            if line.strip()
        ]

    @classmethod
    def _is_managed_path(cls, emby_path: str, config: Dict[str, Any]) -> bool:
        if not emby_path.lower().endswith(".strm"):
            return False
        return any(
            emby_path == prefix or emby_path.startswith(prefix + "/")
            for prefix in cls._prefixes(config)
        )

    @staticmethod
    def _filtered_headers(headers: Any) -> Dict[str, str]:
        return {
            key: value
            for key, value in headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
        }

    @staticmethod
    def _emby_url(config: Dict[str, Any], path_qs: str) -> str:
        base_url = str(config.get("emby_internal_url") or "").strip().rstrip("/")
        if base_url.lower().endswith("/emby") and path_qs.lower().startswith("/emby/"):
            path_qs = path_qs[5:]
        return base_url + "/" + path_qs.lstrip("/")

    @staticmethod
    def _items_api_path(request_path: str) -> str:
        prefix = "/emby" if request_path.lower().startswith("/emby/") else ""
        return f"{prefix}/Items"

    @staticmethod
    def _normalize_media_source_id(value: str) -> str:
        return re.sub(
            r"^mediasource_",
            "",
            str(value or "").strip(),
            flags=re.IGNORECASE,
        )

    @classmethod
    def _media_source_id(cls, request: web.Request) -> str:
        """
        从请求参数中提取并规范化 Emby MediaSourceId

        :param request (Request): 客户端播放或下载请求

        :return str: 去掉 mediasource_ 前缀的媒体源 ID
        """
        for name, value in request.query.items():
            if name.lower() == "mediasourceid":
                return cls._normalize_media_source_id(value)
        return ""

    async def _query_item(
        self,
        request_path: str,
        lookup_id: str,
        config: Dict[str, Any],
        headers: Dict[str, str],
        auth_query: Optional[Dict[str, str]] = None,
    ) -> Tuple[Dict[str, Any], int]:
        """
        使用 MediaSourceId 从 Emby ItemsService 查询原始媒体项

        :param request_path (str): 客户端请求路径
        :param lookup_id (str): 媒体源 ID 或媒体项 ID
        :param config (Dict): 插件配置
        :param headers (Dict): Emby 身份认证请求头
        :param auth_query (Dict): Emby 身份认证查询参数

        :return Tuple: 媒体项响应与 HTTP 状态码
        """
        if not self._session or not lookup_id:
            return {}, 0
        target_url = self._emby_url(
            config,
            self._items_api_path(request_path),
        )
        query = dict(auth_query or {})
        query.update(
            {
                "Ids": lookup_id,
                "Limit": "1",
                "Fields": "Path,MediaSources",
                "Recursive": "true",
            }
        )
        request_headers = dict(headers)
        request_headers["Accept-Encoding"] = "identity"
        async with self._session.get(
            target_url,
            headers=request_headers,
            params=query,
        ) as response:
            status = response.status
            if status != 200:
                return {}, status
            body = await response.read()
            content_encoding = str(
                response.headers.get("Content-Encoding") or ""
            ).lower()
            if content_encoding == "gzip":
                body = gzip.decompress(body)
            elif content_encoding == "deflate":
                try:
                    body = zlib.decompress(body)
                except zlib.error:
                    body = zlib.decompress(body, -zlib.MAX_WBITS)
            payload = json.loads(body)
        items = payload.get("Items") if isinstance(payload, dict) else None
        if isinstance(items, list) and items:
            return items[0], status
        if isinstance(payload, dict) and (
            payload.get("Path") or payload.get("MediaSources")
        ):
            return payload, status
        return {}, status

    @classmethod
    def _select_item_path(
        cls,
        payload: Dict[str, Any],
        config: Dict[str, Any],
    ) -> str:
        """
        从媒体项及媒体源中选择原始 STRM 路径

        优先返回符合已配置媒体库前缀的 STRM，避免 Emby 顶层 Path
        是播放地址时忽略 MediaSources 中的原始文件路径

        :param payload (Dict): Emby 媒体项响应
        :param config (Dict): 插件配置

        :return str: 最合适的媒体路径，找不到时返回空字符串
        """
        candidates: List[str] = []
        item_path = str(payload.get("Path") or "").strip()
        if item_path:
            candidates.append(item_path)
        for source in payload.get("MediaSources") or []:
            candidate = str(source.get("Path") or "").strip()
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        for candidate in candidates:
            if cls._is_managed_path(candidate, config):
                return candidate
        for candidate in candidates:
            if candidate.lower().endswith(".strm"):
                return candidate
        return candidates[0] if candidates else ""

    def _log_unmanaged_item(
        self,
        item_id: str,
        emby_path: str,
        config: Dict[str, Any],
    ) -> None:
        """
        首次遇到未接管媒体项时记录路径诊断信息

        :param item_id (str): Emby 媒体项 ID
        :param emby_path (str): Emby 返回的媒体路径
        :param config (Dict): 插件配置
        """
        signature = f"{emby_path}|{'|'.join(self._prefixes(config))}"
        if self._unmanaged_items.get(item_id) == signature:
            return
        if len(self._unmanaged_items) >= 200:
            self._unmanaged_items.pop(next(iter(self._unmanaged_items)))
        self._unmanaged_items[item_id] = signature
        logger.warning(
            f"直链网关未接管 Emby 媒体项 {item_id}："
            f"媒体路径={emby_path or '未取得'}，"
            f"配置前缀={self._prefixes(config)}"
        )

    def _validate(self, config: Dict[str, Any]) -> None:
        internal_url = str(config.get("emby_internal_url") or "").strip()
        if not internal_url.startswith(("http://", "https://")):
            raise ValueError("Emby 内部地址必须以 http:// 或 https:// 开头")
        if not str(config.get("emby_api_key") or "").strip():
            raise ValueError("未配置 Emby API Key")
        if not self._prefixes(config):
            raise ValueError("未配置 Emby STRM 媒体库路径")
        port = int(config.get("direct_gateway_port") or 8097)
        if port < 1 or port > 65535:
            raise ValueError("直链网关端口必须在 1 到 65535 之间")

    async def _item_path(
        self,
        request_path: str,
        item_id: str,
        config: Dict[str, Any],
    ) -> str:
        cached = self._item_paths.get(item_id)
        if cached:
            return cached
        headers = {"X-Emby-Token": str(config.get("emby_api_key") or "")}
        payload, status = await self._query_item(
            request_path,
            item_id,
            config,
            headers,
        )
        if status != 200:
            logger.warning(
                f"直链网关使用配置密钥查询 Emby 媒体项 {item_id} "
                f"失败：HTTP {status}"
            )
            return ""
        emby_path = self._select_item_path(payload, config)
        if emby_path:
            self._item_paths[item_id] = emby_path
        return emby_path

    @staticmethod
    def _client_auth(request: web.Request) -> Tuple[Dict[str, str], Dict[str, str]]:
        headers: Dict[str, str] = {}
        for name in ("Authorization", "X-Emby-Authorization", "X-Emby-Token"):
            value = str(request.headers.get(name) or "").strip()
            if value:
                headers[name] = value
        query: Dict[str, str] = {}
        for name in ("api_key", "X-Emby-Token"):
            value = str(request.query.get(name) or "").strip()
            if value:
                query[name] = value
        return headers, query

    @staticmethod
    def _playback_client_key(request: web.Request) -> Tuple[str, str]:
        """
        生成 PlaybackInfo 与后续媒体请求的客户端关联键

        :param request (Request): 当前网关请求

        :return Tuple: 客户端 IP 与 User-Agent
        """
        forwarded_for = str(request.headers.get("X-Forwarded-For") or "").strip()
        real_ip = str(request.headers.get("X-Real-IP") or "").strip()
        peer_ip = str(request.remote or "").strip()
        client_ip = (
            forwarded_for.split(",", 1)[0].strip()
            if forwarded_for
            else real_ip or peer_ip
        )
        return client_ip, str(request.headers.get("User-Agent") or "")

    def _cache_playback_paths(
        self,
        request: web.Request,
        item_id: str,
        source_paths: Dict[str, str],
        item_path: str,
        config: Dict[str, Any],
    ) -> None:
        """
        缓存已认证 PlaybackInfo 对应的 STRM 路径

        Infuse 的后续媒体请求可能不重复携带 Emby Token，因此仅把已经通过
        PlaybackInfo 认证的设备和媒体项短期关联起来

        :param request (Request): PlaybackInfo 请求
        :param item_id (str): Emby 媒体项 ID
        :param source_paths (Dict): MediaSourceId 到原始路径的映射
        :param item_path (str): 媒体项原始路径
        :param config (Dict): 网关配置
        """
        headers, query = self._client_auth(request)
        if not headers and not query:
            return
        client_ip, user_agent = self._playback_client_key(request)
        managed_paths = [
            (
                self._normalize_media_source_id(source_id),
                path,
            )
            for source_id, path in source_paths.items()
            if self._is_managed_path(path, config)
        ]
        if self._is_managed_path(item_path, config):
            managed_paths.append(("", item_path))
        if not managed_paths:
            return
        if not any(source_id == "" for source_id, _ in managed_paths):
            managed_paths.append(("", managed_paths[0][1]))
        expires_at = monotonic() + PLAYBACK_PATH_CACHE_TTL_SECONDS
        with self._cache_lock:
            now = monotonic()
            expired_keys = [
                key
                for key, (_, expiry) in self._playback_paths.items()
                if expiry <= now
            ]
            for key in expired_keys:
                self._playback_paths.pop(key, None)
            for source_id, path in managed_paths:
                cache_key = (client_ip, user_agent, item_id, source_id)
                self._playback_paths[cache_key] = (path, expires_at)
            while len(self._playback_paths) > PLAYBACK_PATH_CACHE_MAX_SIZE:
                self._playback_paths.pop(next(iter(self._playback_paths)))

    def _cached_playback_path(
        self,
        request: web.Request,
        item_id: str,
    ) -> str:
        """
        查询同一设备已认证 PlaybackInfo 的短期 STRM 路径

        :param request (Request): 后续媒体请求
        :param item_id (str): Emby 媒体项 ID

        :return str: 命中的 STRM 路径，未命中返回空字符串
        """
        client_ip, user_agent = self._playback_client_key(request)
        source_id = self._media_source_id(request)
        keys = [
            (client_ip, user_agent, item_id, source_id),
            (client_ip, user_agent, item_id, ""),
        ]
        now = monotonic()
        with self._cache_lock:
            for key in keys:
                cached = self._playback_paths.get(key)
                if not cached:
                    continue
                path, expires_at = cached
                if expires_at > now:
                    return path
                self._playback_paths.pop(key, None)
        return ""

    async def _authorized_item_path(
        self,
        request: web.Request,
        item_id: str,
        config: Dict[str, Any],
    ) -> str:
        if not self._session:
            return ""
        headers, query = self._client_auth(request)
        if not headers and not query:
            return ""
        lookup_id = self._media_source_id(request) or item_id
        payload, status = await self._query_item(
            request.path,
            lookup_id,
            config,
            headers,
            query,
        )
        if status != 200:
            logger.warning(
                f"直链网关使用客户端身份查询 Emby 媒体源 {lookup_id} "
                f"失败：HTTP {status}"
            )
            return ""
        emby_path = self._select_item_path(payload, config)
        if emby_path:
            self._item_paths[lookup_id] = emby_path
        return emby_path

    async def _playback_source_paths(
        self,
        request_path: str,
        payload: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Dict[str, str]:
        """
        按 MediaWarp 的 MediaSourceId 查询流程获取播放源原始路径

        :param request_path (str): PlaybackInfo 请求路径
        :param payload (Dict): Emby PlaybackInfo 响应
        :param config (Dict): 插件配置

        :return Dict: MediaSourceId 到原始媒体路径的映射
        """
        source_paths: Dict[str, str] = {}
        for source in payload.get("MediaSources") or []:
            source_id = str(source.get("Id") or "").strip()
            lookup_id = self._normalize_media_source_id(source_id)
            if not lookup_id:
                continue
            source_paths[source_id] = await self._item_path(
                request_path,
                lookup_id,
                config,
            )
        return source_paths

    def _strm_target(self, emby_path: str, config: Dict[str, Any]) -> Tuple[str, str]:
        local_path = self.local_strm_path(
            emby_path,
            str(config.get("emby_path_mappings") or ""),
        )
        if not local_path.is_file():
            raise FileNotFoundError(f"MoviePilot 无法读取 STRM：{local_path}")
        content = local_path.read_text(encoding="utf-8-sig").strip()
        parsed = urlsplit(content)
        match = PLAY_PATH_PATTERN.search(parsed.path)
        if not match:
            raise ValueError("STRM 不是由腾讯文档115媒体库生成")
        query = parse_qs(parsed.query)
        expected_token = str(config.get("playback_token") or "")
        actual_token = str((query.get("token") or [""])[0])
        if not expected_token or not compare_digest(actual_token, expected_token):
            raise ValueError("STRM 播放密钥无效")
        file_id = str((query.get("file_id") or [""])[0])
        return match.group(1), file_id

    def _strm_media_details(
        self,
        emby_path: str,
        config: Dict[str, Any],
    ) -> Tuple[str, int]:
        """
        查询 STRM 背后的真实容器和文件大小

        Emby 扫描本插件的播放入口时只能看到 ``.strm`` 或无扩展名 URL，
        Infuse 播放 ISO 等光盘镜像前需要从 PlaybackInfo 得知真实容器。

        :param emby_path (str): Emby 中的 STRM 路径
        :param config (Dict): 网关配置

        :return Tuple: 小写扩展名（不含点）与字节数
        """
        try:
            resource_id, file_id = self._strm_target(emby_path, config)
            store = getattr(self.resolver, "store", None)
            if store is None:
                return "", 0
            resource = store.get_resource(resource_id) or {}
            target_file_id = file_id or str(
                resource.get("resolved_file_id") or ""
            ).strip()
            resource_file = (
                store.get_resource_file(resource_id, target_file_id)
                if target_file_id
                else None
            ) or {}
            file_name = str(
                resource_file.get("file_name")
                or resource.get("resolved_file_name")
                or ""
            ).strip()
            container = Path(file_name).suffix.lower().lstrip(".")
            file_size = int(resource_file.get("file_size") or 0)
            return container, max(file_size, 0)
        except Exception as error:
            logger.warning(f"读取 STRM 真实媒体信息失败：{emby_path} - {error}")
            return "", 0

    async def _direct_response(
        self,
        request: web.Request,
        item_id: str,
        config: Dict[str, Any],
    ) -> Optional[web.Response]:
        try:
            emby_path = await self._authorized_item_path(request, item_id, config)
        except Exception as error:
            logger.error(
                f"直链网关查询 Emby 媒体项 {item_id} 失败：{error}",
                exc_info=True,
            )
            return web.json_response(
                {"success": False, "message": f"查询 Emby 媒体项失败：{error}"},
                status=502,
            )
        if not emby_path:
            emby_path = self._cached_playback_path(request, item_id)
        if not self._is_managed_path(emby_path, config):
            self._log_unmanaged_item(item_id, emby_path, config)
            return None
        try:
            resource_id, file_id = self._strm_target(emby_path, config)
            direct_url = await asyncio.to_thread(
                self.resolver.resolve,
                resource_id,
                file_id or None,
                request.headers.get("user-agent", ""),
            )
            logger.info(f"直链网关已为 Emby 媒体项 {item_id} 返回115直链")
            return web.Response(status=302, headers={"Location": direct_url})
        except ShareResolutionError as error:
            return web.json_response(
                {"success": False, "message": str(error)},
                status=error.status_code,
            )
        except Exception as error:
            logger.error(f"直链网关解析 Emby 媒体项 {item_id} 失败：{error}")
            return web.json_response(
                {"success": False, "message": str(error)},
                status=502,
            )

    def _modify_playback_info(
        self,
        payload: Dict[str, Any],
        config: Dict[str, Any],
        item_path: str = "",
        source_paths: Optional[Dict[str, str]] = None,
        item_id: str = "",
    ) -> Dict[str, Any]:
        managed_item = self._is_managed_path(item_path, config)
        for source in payload.get("MediaSources") or []:
            emby_path = str(source.get("Path") or "")
            source_id = str(source.get("Id") or "")
            original_path = str((source_paths or {}).get(source_id) or "")
            if (
                not managed_item
                and not self._is_managed_path(original_path, config)
                and not self._is_managed_path(emby_path, config)
            ):
                continue
            managed_path = next(
                (
                    candidate
                    for candidate in (original_path, emby_path, item_path)
                    if self._is_managed_path(candidate, config)
                ),
                "",
            )
            container, file_size = self._strm_media_details(
                managed_path,
                config,
            )
            source["SupportsDirectPlay"] = True
            source["SupportsDirectStream"] = True
            source["SupportsTranscoding"] = False
            if container:
                source["Container"] = container
            if file_size:
                source["Size"] = file_size
            for key in (
                "TranscodingUrl",
                "TranscodingContainer",
                "TranscodingSubProtocol",
                "DirectStreamUrl",
            ):
                source.pop(key, None)
            if item_id and source_id:
                media_route = (
                    "audio"
                    if str(source.get("Type") or "").lower() == "audio"
                    else "videos"
                )
                stream_name = "stream"
                if container and re.fullmatch(r"[a-z0-9]+", container):
                    stream_name += f".{container}"
                source["DirectStreamUrl"] = (
                    f"/{media_route}/{quote(item_id, safe='')}/{stream_name}"
                    f"?Static=true&MediaSourceId={quote(source_id, safe='')}"
                )
        return payload

    async def _proxy_websocket(
        self,
        request: web.Request,
        target_url: str,
    ) -> web.WebSocketResponse:
        if not self._session:
            raise web.HTTPServiceUnavailable(text="直链网关尚未初始化")
        downstream = web.WebSocketResponse()
        await downstream.prepare(request)
        headers = self._filtered_headers(request.headers)
        async with self._session.ws_connect(target_url, headers=headers) as upstream:
            async def downstream_to_upstream() -> None:
                async for message in downstream:
                    if message.type == WSMsgType.TEXT:
                        await upstream.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await upstream.send_bytes(message.data)
                    elif message.type == WSMsgType.CLOSE:
                        await upstream.close()

            async def upstream_to_downstream() -> None:
                async for message in upstream:
                    if message.type == WSMsgType.TEXT:
                        await downstream.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await downstream.send_bytes(message.data)
                    elif message.type == WSMsgType.CLOSE:
                        await downstream.close()

            await asyncio.gather(
                downstream_to_upstream(),
                upstream_to_downstream(),
            )
        return downstream

    async def _proxy(self, request: web.Request) -> web.StreamResponse:
        config = self.config_provider()
        target_url = self._emby_url(config, request.path_qs)
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await self._proxy_websocket(request, target_url)
        stream_match = STREAM_ITEM_PATTERN.search(request.path)
        if stream_match and request.method in {"GET", "HEAD"}:
            direct_response = await self._direct_response(
                request,
                stream_match.group(1),
                config,
            )
            if direct_response is not None:
                return direct_response
        if not self._session:
            raise web.HTTPServiceUnavailable(text="直链网关尚未初始化")
        headers = self._filtered_headers(request.headers)
        headers = {
            key: value
            for key, value in headers.items()
            if key.lower() != "accept-encoding"
        }
        headers["Accept-Encoding"] = "identity"
        request_body = await request.read()
        async with self._session.request(
            request.method,
            target_url,
            headers=headers,
            data=request_body or None,
            allow_redirects=False,
        ) as upstream:
            response_headers = self._filtered_headers(upstream.headers)
            playback_match = PLAYBACK_INFO_PATTERN.search(request.path)
            if playback_match and upstream.status == 200:
                upstream_body = await upstream.read()
                try:
                    payload = json.loads(upstream_body)
                    source_paths = await self._playback_source_paths(
                        request.path,
                        payload,
                        config,
                    )
                    item_path = ""
                    if not any(source_paths.values()):
                        item_path = await self._item_path(
                            request.path,
                            playback_match.group(1),
                            config,
                        )
                    self._cache_playback_paths(
                        request,
                        playback_match.group(1),
                        source_paths,
                        item_path,
                        config,
                    )
                    modified = self._modify_playback_info(
                        payload,
                        config,
                        item_path,
                        source_paths,
                        playback_match.group(1),
                    )
                    return web.json_response(
                        modified,
                        status=upstream.status,
                        headers={
                            key: value
                            for key, value in response_headers.items()
                            if key.lower() not in {"content-length", "content-type"}
                        },
                    )
                except Exception as error:
                    logger.warning(f"修改 Emby PlaybackInfo 失败，返回原响应：{error}")
                    return web.Response(
                        body=upstream_body,
                        status=upstream.status,
                        headers=response_headers,
                    )
            response = web.StreamResponse(
                status=upstream.status,
                reason=upstream.reason,
                headers=response_headers,
            )
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(256 * 1024):
                await response.write(chunk)
            await response.write_eof()
            return response

    async def _serve(self) -> None:
        config = self.config_provider()
        self._validate(config)
        timeout = ClientTimeout(total=None, connect=15, sock_read=None)
        self._session = ClientSession(
            timeout=timeout,
            trust_env=False,
            auto_decompress=False,
        )
        application = web.Application(client_max_size=64 * 1024 * 1024)
        application.router.add_route("*", "/{path:.*}", self._proxy)
        self._runner = web.AppRunner(application, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner,
            host="0.0.0.0",
            port=int(config.get("direct_gateway_port") or 8097),
        )
        await site.start()
        self._state = "running"
        logger.info(
            f"内置 Emby 直链网关已启动，端口 {int(config.get('direct_gateway_port') or 8097)}"
        )

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
            loop.run_forever()
        except Exception as error:
            self._state = "error"
            self._last_error = str(error)
            logger.error(f"内置 Emby 直链网关启动失败：{error}", exc_info=True)
        finally:
            loop.run_until_complete(self._cleanup())
            loop.close()
            self._loop = None

    async def _cleanup(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._session:
            await self._session.close()
            self._session = None

    def start_background(self) -> None:
        """在后台启动内置 Emby 直链网关"""
        config = self.config_provider()
        if not config.get("direct_gateway_enabled"):
            self._state = "disabled"
            return
        try:
            self._validate(config)
        except Exception as error:
            self._state = "error"
            self._last_error = str(error)
            logger.error(f"内置 Emby 直链网关配置无效：{error}")
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._state = "starting"
            self._last_error = ""
            self._thread = Thread(
                target=self._run,
                name="TencentDoc115DirectGateway",
                daemon=True,
            )
            self._thread.start()

    def restart_background(self) -> None:
        """停止现有服务并在后台重新启动内置 Emby 直链网关"""
        self.stop()
        self.start_background()

    def clear_cache(self) -> None:
        """清空媒体项路径和未接管诊断缓存"""
        with self._cache_lock:
            self._item_paths.clear()
            self._unmanaged_items.clear()
            self._playback_paths.clear()

    def stop(self) -> None:
        """停止内置 Emby 直链网关"""
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
        self._thread = None
        self._state = (
            "disabled"
            if not self.config_provider().get("direct_gateway_enabled")
            else "stopped"
        )

    def status(self) -> Dict[str, Any]:
        """
        返回内置 Emby 直链网关状态

        :return Dict: 启用状态、运行状态、端口和最近错误
        """
        config = self.config_provider()
        return {
            "enabled": bool(config.get("direct_gateway_enabled")),
            "state": self._state,
            "port": int(config.get("direct_gateway_port") or 8097),
            "last_error": self._last_error,
        }
