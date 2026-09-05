"""Read-only disc release calendar with optional TMDB metadata matching."""

import asyncio
import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.responses import HTMLResponse
from app.plugins import _PluginBase

from .engine import CalendarEngine
from .ui import HTML


class BlurayReleaseCalendar(_PluginBase):
    plugin_name = "近期蓝光发行"
    plugin_desc = "浏览近期美国蓝光和4K光盘发行，匹配TMDB海报与简介；不自动订阅或下载"
    plugin_icon = "https://raw.githubusercontent.com/CelestialRipple/115-doc/main/docs/blurayreleasecalendar/icon.svg"
    plugin_version = "0.1.2"
    plugin_author = "CelestialRipple"
    author_url = "https://github.com/CelestialRipple"
    plugin_config_prefix = "blurayreleasecalendar_"
    plugin_order = 37
    auth_level = 1

    def init_plugin(self, config=None):
        self._config = {"enabled": False, **(config or {})}
        proxy = str(self._config.get("proxy") or "").strip()
        if not proxy:
            try:
                from app.core.config import settings

                proxy = str(getattr(settings, "PROXY_HOST", "") or "")
            except ImportError:
                pass
        if proxy and urlsplit(proxy).scheme not in {
            "http",
            "https",
            "socks5",
            "socks5h",
        }:
            proxy = ""
        self._engine = CalendarEngine(Path(self.get_data_path()) / "calendar.db", proxy)

    def get_state(self):
        return bool(getattr(self, "_config", {}).get("enabled"))

    def require_enabled(self):
        if not self.get_state():
            raise HTTPException(409, "请先启用近期蓝光发行插件")

    @staticmethod
    def month(value):
        now = date.today()
        value = value or now.strftime("%Y-%m")
        if not re.fullmatch(r"\d{4}-\d{2}", value):
            raise HTTPException(400, "月份格式须为YYYY-MM")
        y, m = map(int, value.split("-"))
        if not 1 <= m <= 12 or abs((y - now.year) * 12 + m - now.month) > 12:
            raise HTTPException(400, "请选择最近一年或未来一年内的月份")
        return value

    async def ui(self):
        return HTMLResponse(
            HTML,
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def releases(self, request: Request):
        self.require_enabled()
        month = self.month(request.query_params.get("month"))
        try:
            page = max(1, min(200, int(request.query_params.get("page", "1"))))
        except ValueError:
            raise HTTPException(400, "页码无效")
        fmt = request.query_params.get("format", "all")
        period = request.query_params.get("period", "all")
        today = date.today().isoformat()
        if fmt not in {"all", "4k", "bluray"} or period not in {
            "all",
            "released",
            "upcoming",
        }:
            raise HTTPException(400, "筛选条件无效")
        items, updated, warning = await asyncio.to_thread(
            self._engine.releases, month, request.query_params.get("refresh") == "1"
        )
        items = [
            item
            for item in items
            if (fmt == "all" or (item["format"] == "4K UHD") == (fmt == "4k"))
            and (
                period == "all"
                or (item["release_date"] <= today) == (period == "released")
            )
        ]
        return {
            "month": month,
            "total": len(items),
            "updated": updated,
            "warning": warning,
            "items": [
                {**item, "metadata": self._engine.metadata(item)}
                for item in items[(page - 1) * 24 : page * 24]
            ],
        }

    async def match(self, request: Request):
        self.require_enabled()
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(413, "请求过大")
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError()
            month = self.month(data.get("month"))
            ids = data.get("ids")
            if (
                not isinstance(ids, list)
                or len(ids) > 8
                or any(
                    not isinstance(x, str) or not re.fullmatch(r"\d{1,12}", x)
                    for x in ids
                )
            ):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(400, "发行版本列表无效")
        return await asyncio.to_thread(
            self._engine.match, month, list(dict.fromkeys(ids))
        )

    def get_api(self):
        return [
            {
                "path": "/ui",
                "endpoint": self.ui,
                "methods": ["GET"],
                "allow_anonymous": True,
                "summary": "发行日历页面",
            },
            {
                "path": "/releases",
                "endpoint": self.releases,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "读取发行日历",
            },
            {
                "path": "/match",
                "endpoint": self.match,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "读取匹配元数据",
            },
        ]

    def get_page(self):
        return [
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal"},
                "text": "浏览美国蓝光与4K发行日历，匹配影片海报、中文名与简介。不会自动订阅或下载。",
            },
            {
                "component": "VBtn",
                "props": {
                    "href": "/api/v1/plugin/BlurayReleaseCalendar/ui",
                    "target": "_blank",
                    "color": "primary",
                },
                "text": "打开近期蓝光发行",
            },
        ]

    def get_form(self):
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VSwitch",
                        "props": {"model": "enabled", "label": "启用近期蓝光发行"},
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "proxy",
                            "label": "访问发行来源的代理（留空使用MoviePilot代理）",
                        },
                    },
                ],
            }
        ], {"enabled": False, "proxy": ""}

    def get_module(self):
        return {}

    @staticmethod
    def get_command():
        return []

    def get_service(self):
        return []

    def stop_service(self):
        pass
