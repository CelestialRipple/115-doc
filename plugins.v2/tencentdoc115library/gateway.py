import asyncio
import json
import re
from pathlib import Path
from secrets import compare_digest
from threading import Lock, Thread
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

try:
    from app.sdk.logging import logger
except ImportError:
    from app.log import logger

from .resolver import ShareResolutionError, ShareResolver


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
    def _item_api_path(request_path: str, item_id: str) -> str:
        prefix = "/emby" if request_path.lower().startswith("/emby/") else ""
        return f"{prefix}/Items/{item_id}"

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
        if not self._session:
            return ""
        api_path = self._item_api_path(request_path, item_id)
        target_url = self._emby_url(config, api_path)
        headers = {"X-Emby-Token": str(config.get("emby_api_key") or "")}
        async with self._session.get(target_url, headers=headers) as response:
            if response.status != 200:
                return ""
            payload = await response.json(content_type=None)
        emby_path = str(payload.get("Path") or "")
        if not emby_path:
            for source in payload.get("MediaSources") or []:
                candidate = str(source.get("Path") or "")
                if candidate:
                    emby_path = candidate
                    break
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
        api_path = self._item_api_path(request.path, item_id)
        target_url = self._emby_url(config, api_path)
        async with self._session.get(
            target_url,
            headers=headers,
            params=query,
        ) as response:
            if response.status != 200:
                return ""
            payload = await response.json(content_type=None)
        emby_path = str(payload.get("Path") or "")
        if not emby_path:
            for source in payload.get("MediaSources") or []:
                candidate = str(source.get("Path") or "")
                if candidate:
                    emby_path = candidate
                    break
        if emby_path:
            self._item_paths[item_id] = emby_path
        return emby_path

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

    async def _direct_response(
        self,
        request: web.Request,
        item_id: str,
        config: Dict[str, Any],
    ) -> Optional[web.Response]:
        emby_path = await self._authorized_item_path(request, item_id, config)
        if not self._is_managed_path(emby_path, config):
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
    ) -> Dict[str, Any]:
        managed_item = self._is_managed_path(item_path, config)
        for source in payload.get("MediaSources") or []:
            emby_path = str(source.get("Path") or "")
            if not managed_item and not self._is_managed_path(emby_path, config):
                continue
            source["SupportsDirectPlay"] = False
            source["SupportsDirectStream"] = True
            source["SupportsTranscoding"] = False
            source["IsRemote"] = True
            source.pop("TranscodingUrl", None)
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
                    item_path = await self._item_path(
                        request.path,
                        playback_match.group(1),
                        config,
                    )
                    modified = self._modify_playback_info(
                        payload,
                        config,
                        item_path,
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
        self._session = ClientSession(timeout=timeout, trust_env=False)
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
