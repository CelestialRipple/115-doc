"""PanSou + BT4G resource aggregation for MoviePilot."""

import asyncio
import hashlib
import hmac
import html
import re
import secrets
import time
from types import SimpleNamespace
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from starlette.responses import HTMLResponse

from app.log import logger
from app.plugins import _PluginBase

try:
    from app.domain.context import TorrentInfo
except ImportError:
    from app.core.context import TorrentInfo

from .bridge import MARKER, SearchBridge
from .downloads import DownloadService, download_response, library_plugin, share_url
from .engine import SearchEngine
from .providers import bt4g_search_url
from .ui import SEARCH_HTML
from .shortcuts import DEFAULT_WEB_SEARCHES


class PanSouAggregate(_PluginBase):
    plugin_name = "PanSou聚合搜索"
    plugin_desc = "原生聚合115和磁力，支持浏览器下载、按类型保存影视库及自定义网页入口"
    plugin_icon = "https://raw.githubusercontent.com/CelestialRipple/115-doc/main/docs/pansouaggregate/icon.svg"
    plugin_version = "0.3.3"
    plugin_author = "CelestialRipple"
    author_url = "https://github.com/CelestialRipple"
    plugin_config_prefix = "pansouaggregate_"
    plugin_order = 36
    auth_level = 1

    def init_plugin(self, config=None):
        self.stop_service()
        defaults = {
            "enabled": False,
            "pansou_enabled": True,
            "web_searches": DEFAULT_WEB_SEARCHES,
            "bt4g_enabled": True,
            "pansou_url": "",
            "bt4g_url": "https://bt4gprx.com",
            "timeout": 20,
            "limit": 100,
            "cache_seconds": 120,
        }
        self._config = {**defaults, **(config or {})}
        for name, low, high in (
            ("timeout", 5, 45),
            ("limit", 1, 200),
            ("cache_seconds", 15, 600),
        ):
            try:
                self._config[name] = min(high, max(low, int(self._config[name])))
            except (TypeError, ValueError):
                self._config[name] = defaults[name]
        if not self._config.get("ui_key"):
            self._config["ui_key"] = secrets.token_urlsafe(32)
            self.update_config(self._config)
        self._engine = SearchEngine(self._config)
        self._downloads = DownloadService(lambda: self.get_data_path())
        self._bridge = SearchBridge(self)
        self._bridge_status = "未启用"
        if self.get_state():
            try:
                self._bridge_status = self._bridge.install()
            except (ImportError, AttributeError):
                self._bridge_status = "原生入口不可用，请使用插件搜索页"
                logger.warning("PanSou聚合搜索：当前 MoviePilot 模块布局不支持兼容入口")

    def get_state(self):
        return bool(getattr(self, "_config", {}).get("enabled"))

    def stop_service(self):
        if getattr(self, "_bridge", None):
            self._bridge.uninstall()
        if getattr(self, "_engine", None):
            self._engine.stop()

    def get_module(self):
        return {
            "search_torrents": self.search_torrents,
            "async_search_torrents": self.async_search_torrents,
        }

    def search_torrents(self, site, keyword, mtype=None, page=0):
        if (
            not self.get_state()
            or (site and not site.get(MARKER))
            or int(page or 0) != 0
        ):
            return []
        keyword = self._keyword(keyword)
        if not keyword:
            return []
        # IMDb IDs are not titles. Ask MoviePilot metadata to translate when available.
        if re.fullmatch(r"tt\d+", keyword, re.I):
            try:
                from app.modules.themoviedb.tmdbv3api import Find

                found = Find().find_by_imdb_id(keyword) or {}
                candidates = (
                    found.get(
                        "tv_results"
                        if getattr(mtype, "value", "") == "电视剧"
                        else "movie_results"
                    )
                    or found.get("movie_results")
                    or found.get("tv_results")
                    or []
                )
                if candidates:
                    keyword = str(
                        candidates[0].get("title")
                        or candidates[0].get("name")
                        or keyword
                    )
            except Exception:
                pass
        items, errors = self._engine.search(keyword)
        for name, message in errors.items():
            logger.warning(f"PanSou聚合搜索 {name}：{message}")
        return [
            TorrentInfo(
                site_name=(
                    "BT4G网页搜索"
                    if item.cloud == "bt4g"
                    else "聚合网页搜索"
                    if item.cloud == "web"
                    else "PanSou聚合搜索"
                ),
                site_order=0,
                title=item.title,
                description=f"{item.source} · {item.cloud} · "
                + (
                    "点击打开网页搜索"
                    if item.cloud in {"bt4g", "web"}
                    else "点击下载 · ⓘ保存影视库"
                ),
                enclosure="pansou://" + item.id,
                page_url=self._resource_url(item.id)
                + "#mp-pansou="
                + urlencode(
                    {
                        "url": self._resource_url(item.id).replace(
                            "/resource/", "/download/"
                        )
                    }
                ),
                size=item.size,
                seeders=item.seeders,
                labels=[item.cloud, "ⓘ资源操作"],
                pri_order=50,
                category=getattr(mtype, "value", None),
            )
            for item in items
        ]

    async def async_search_torrents(self, site, keyword, mtype=None, page=0):
        return await asyncio.to_thread(self.search_torrents, site, keyword, mtype, page)

    @staticmethod
    def _keyword(value):
        return " ".join(str(value or "").split())[:200]

    def _require(self, request):
        supplied = request.headers.get("x-pansou-key") or request.query_params.get(
            "key", ""
        )
        key = str(getattr(self, "_config", {}).get("ui_key") or "")
        if not key or not hmac.compare_digest(supplied, key):
            raise HTTPException(401, "请从插件详情页重新打开搜索页面")
        if not self.get_state():
            raise HTTPException(409, "请先启用插件")

    def _sign(self, rid, expiry):
        return hmac.new(
            self._config["ui_key"].encode(),
            f"resource:{rid}:{expiry}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def _resource_url(self, rid):
        expiry = int(time.time()) + 3600
        return f"/api/v1/plugin/PanSouAggregate/resource/{rid}?" + urlencode(
            {"expires": expiry, "sig": self._sign(rid, expiry)}
        )

    def _resource(self, rid, request):
        expiry = request.query_params.get("expires", "")
        sig = request.query_params.get("sig", "")
        if (
            not self.get_state()
            or not re.fullmatch(r"\d{1,12}", expiry)
            or int(expiry) < time.time()
            or not hmac.compare_digest(sig, self._sign(rid, expiry))
        ):
            raise HTTPException(401, "资源操作链接已过期，请重新搜索")
        item = self._engine.get(rid)
        if item is None:
            raise HTTPException(404, "搜索结果已过期，请重新搜索")
        return item

    async def search_ui(self, request: Request):
        self._require(request)
        return HTMLResponse(SEARCH_HTML, headers=self._headers())

    async def search_api(self, request: Request):
        self._require(request)
        keyword = self._keyword(request.query_params.get("keyword"))
        if not keyword:
            raise HTTPException(400, "请输入搜索关键词")
        items, errors = await asyncio.to_thread(
            self._engine.search, keyword, request.query_params.get("refresh") == "1"
        )
        return {
            "items": [
                {**item.to_dict(), "action_url": self._resource_url(item.id)}
                for item in items
            ],
            "errors": errors,
            "keyword": keyword,
            "bt4g_url": bt4g_search_url(self._config["bt4g_url"], keyword),
        }

    async def resource_page(self, rid: str, request: Request):
        item = self._resource(rid, request)
        if item.cloud in {"bt4g", "web"}:
            from starlette.responses import RedirectResponse

            return RedirectResponse(item.url, status_code=302, headers=self._headers())
        escape = html.escape
        download = escape(
            request.url.path.replace("/resource/", "/download/")
            + "?"
            + request.url.query,
            quote=True,
        )
        supported = item.cloud in {"115", "magnet", "ed2k"}
        form = (
            '<form method="post"><label>直属文件夹 <input name="group" value="聚合搜索" maxlength="60"></label><label>类型 <select name="media_mode"><option value="mixed" selected>混合（自动识别）</option><option value="movie">电影</option><option value="tv">电视剧</option></select></label><button>保存影视库</button><p>此操作提交后台导入和刮削；磁力或 ED2K 首次播放时才进行115离线。</p></form>'
            if supported
            else ""
        )
        # POST is same URL with its short, resource-scoped signature. No global key.
        content = f'<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>{escape(item.title)}</title><style>body{{font:17px system-ui;max-width:800px;margin:3em auto;padding:1em;line-height:1.7}}input,select,button{{font:inherit;padding:.5em;margin:.5em}}textarea{{width:100%;height:100px}}</style><h1>{escape(item.title)}</h1><p>{escape(item.source)} · {escape(item.cloud)}</p><p>提取码：{escape(item.password or "无")}</p><p><a href="{escape(item.url, quote=True)}" target="_blank" rel="noopener noreferrer">打开原始资源</a></p><textarea readonly>{escape(item.url)}</textarea>{form}<p><a href="{download}" target="_blank" rel="noopener noreferrer">浏览器下载 / 打开网盘</a></p><p>115 分享可选视频文件后下载；磁力和 ED2K 通过115离线解析后下载。其他网盘打开原始分享页。</p>'
        return HTMLResponse(content, headers=self._headers())

    async def download(self, rid: str, request: Request):
        item = self._resource(rid, request)
        return await asyncio.to_thread(
            download_response, item, request, self._headers(), self._downloads
        )

    async def import_library(self, rid: str, request: Request):
        item = self._resource(rid, request)
        from urllib.parse import parse_qs

        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(413, "表单过大")
        form = parse_qs(body.decode(errors="replace"))
        group = form.get("group", ["聚合搜索"])[0].strip()
        mode = form.get("media_mode", ["mixed"])[0]
        if mode not in {"movie", "tv", "mixed"}:
            raise HTTPException(400, "类型须选择电影、电视剧或混合")
        if (
            not group
            or len(group) > 60
            or group in {".", ".."}
            or any(c in group for c in "/\\\r\n\0")
        ):
            raise HTTPException(400, "文件夹名称无效")
        if item.cloud not in {"115", "magnet", "ed2k"}:
            raise HTTPException(400, "该类型不支持115媒体库导入")
        try:
            target = library_plugin()
            url = share_url(item)
            title = item.title.replace("|", " ").replace("\n", " ").replace("\r", " ")
            result = await asyncio.to_thread(
                target.import_manual_resources,
                SimpleNamespace(
                    links=f"{title}|{url}", group_name=group, media_mode=mode
                ),
            )
            return HTMLResponse(
                '<meta charset="utf-8"><p>'
                + html.escape(str(getattr(result, "message", "请求已提交")))
                + '</p><a href="javascript:history.back()">返回</a>',
                headers=self._headers(),
            )
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(502, "媒体库导入失败，请查看目标插件状态")

    @staticmethod
    def _headers():
        return {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "SAMEORIGIN",
        }

    def get_api(self):
        return [
            {
                "path": "/ui",
                "endpoint": self.search_ui,
                "methods": ["GET"],
                "allow_anonymous": True,
                "summary": "打开聚合搜索页",
            },
            {
                "path": "/search",
                "endpoint": self.search_api,
                "methods": ["GET"],
                "allow_anonymous": True,
                "summary": "搜索聚合资源",
            },
            {
                "path": "/download/{rid}",
                "endpoint": self.download,
                "methods": ["GET"],
                "allow_anonymous": True,
                "summary": "浏览器下载或打开网页搜索",
            },
            {
                "path": "/resource/{rid}",
                "endpoint": self.resource_page,
                "methods": ["GET"],
                "allow_anonymous": True,
                "summary": "资源操作页",
            },
            {
                "path": "/resource/{rid}",
                "endpoint": self.import_library,
                "methods": ["POST"],
                "allow_anonymous": True,
                "summary": "确认导入115媒体库",
            },
        ]

    def get_page(self):
        url = "/api/v1/plugin/PanSouAggregate/ui?" + urlencode(
            {"key": getattr(self, "_config", {}).get("ui_key", "")}
        )
        return [
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal"},
                "text": f"搜索入口：{getattr(self, '_bridge_status', '未初始化')}。PanSou 仅显示115和磁力。原生结果点击下载，ⓘ中选择电影／电视剧／混合后保存；网页入口点击后在新标签页打开。",
            },
            {
                "component": "VBtn",
                "props": {"href": url, "target": "_blank", "color": "primary"},
                "text": "打开聚合搜索",
            },
        ]

    def get_form(self):
        fields = [
            ("enabled", "启用插件", "VSwitch", {}),
            ("pansou_enabled", "聚合 PanSou", "VSwitch", {}),
            (
                "pansou_url",
                "PanSou 服务地址（如 http://群晖IP:端口）",
                "VTextField",
                {},
            ),
            ("pansou_username", "PanSou 用户名（开启认证时填写）", "VTextField", {}),
            ("pansou_password", "PanSou 密码", "VTextField", {"type": "password"}),
            (
                "pansou_token",
                "PanSou Bearer Token（优先于账号密码）",
                "VTextField",
                {"type": "password"},
            ),
            ("plugins", "PanSou 搜索插件（逗号分隔，留空全部）", "VTextField", {}),
            ("channels", "TG 频道（逗号分隔，留空使用服务默认）", "VTextField", {}),
            ("bt4g_enabled", "显示 BT4G 网页搜索入口", "VSwitch", {}),
            ("bt4g_url", "BT4G 地址", "VTextField", {}),
            (
                "web_searches",
                "网页入口（每行：名称|网址，{keyword} 替换为搜索词；清空则不显示）",
                "VTextarea",
                {"rows": 5},
            ),
            ("timeout", "单来源请求超时秒数（5–45）", "VTextField", {"type": "number"}),
            ("limit", "每次最多结果数（1–200）", "VTextField", {"type": "number"}),
            (
                "cache_seconds",
                "搜索缓存秒数（15–600）",
                "VTextField",
                {"type": "number"},
            ),
        ]
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": widget,
                        "props": {"model": name, "label": label, **props},
                    }
                    for name, label, widget, props in fields
                ],
            }
        ], {
            "enabled": False,
            "pansou_enabled": True,
            "web_searches": DEFAULT_WEB_SEARCHES,
            "bt4g_enabled": True,
            "bt4g_url": "https://bt4gprx.com",
            "timeout": 20,
            "limit": 100,
            "cache_seconds": 120,
        }

    @staticmethod
    def get_command():
        return []

    def get_service(self):
        if not self.get_state():
            return []
        from apscheduler.triggers.interval import IntervalTrigger

        return [
            {
                "id": "PanSouOfflineCleanup",
                "name": "PanSou离线下载缓存清理",
                "trigger": IntervalTrigger(minutes=30),
                "func": self._downloads.cleanup,
                "kwargs": {},
            }
        ]
