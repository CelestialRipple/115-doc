"""Reuse the 115 resolver without registering download-only items for scraping."""

import copy
import html
from pathlib import Path
from threading import RLock
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import HTTPException
from starlette.responses import HTMLResponse, RedirectResponse

from .providers import http_url


def library_plugin():
    try:
        from app.runtime.extensions.plugin.manager import PluginManager
    except ImportError:
        try:
            from app.runtime.extensions.plugin_manager import PluginManager
        except ImportError:
            from app.core.plugin import PluginManager
    target = PluginManager().running_plugins.get("TencentDoc115Library")
    if target is None or not hasattr(target, "import_manual_resources"):
        raise HTTPException(409, "请先启用腾讯文档115媒体库插件（0.13.0 或更新版本）")
    return target


def share_url(item):
    url = item.url
    if (
        item.cloud == "115"
        and item.password
        and not parse_qs(urlsplit(url).query).get("password")
    ):
        url += ("&" if "?" in url else "?") + urlencode({"password": item.password})
    return url


class DownloadService:
    def __init__(self, path_provider):
        self.path_provider = path_provider
        self.lock = RLock()
        self.store = None

    def resolver(self, target):
        original = getattr(target, "_resolver", None)
        if original is None:
            raise HTTPException(409, "115 解析器尚未就绪")
        with self.lock:
            if self.store is None:
                # Same schema/API, separate database. The library builder never
                # sees download-only resources. Offline cache ownership survives
                # restarts and is cleaned using the resolver's own safeguards.
                self.store = type(original.store)(
                    Path(self.path_provider()) / "downloads.db"
                )
            resolver = copy.copy(original)
            resolver.store = self.store
            return resolver

    def resolve_offline(self, target, item, user_agent):
        resolver = self.resolver(target)
        rid = "pansou-" + item.id
        with self.lock:
            if not self.store.get_resource(rid):
                self.store.upsert_manual_resource(
                    sheet_id="pansou-downloads",
                    resource_id=rid,
                    title=item.title,
                    year="",
                    share_url=item.url,
                    media_type="",
                    group_name="聚合下载",
                    media_mode="mixed",
                    status="pending",
                )
        return resolver.resolve(rid, user_agent=user_agent)

    def cleanup(self):
        # Don't create a database merely because the scheduler wakes up.
        if (
            self.store is None
            and not (Path(self.path_provider()) / "downloads.db").exists()
        ):
            return
        try:
            self.resolver(library_plugin()).cleanup_offline_cache()
        except Exception:
            # URLs and provider exceptions can contain credentials.
            from app.log import logger

            logger.warning("PanSou：离线缓存清理未完成，将在下次重试")


def download_response(item, request, headers, service):
    if item.cloud not in {"115", "magnet", "ed2k"}:
        return RedirectResponse(item.url, status_code=302, headers=headers)
    try:
        target = library_plugin()
        resolver = getattr(target, "_resolver", None)
        if resolver is None:
            raise HTTPException(409, "115 解析器尚未就绪")
        user_agent = request.headers.get("user-agent", "")
        if item.cloud in {"magnet", "ed2k"}:
            direct_url = service.resolve_offline(target, item, user_agent)
        else:
            url = share_url(item)
            files = resolver.list_video_files(url)
            file_id = request.query_params.get("file_id", "")
            if not files:
                raise HTTPException(404, "分享中没有可下载的视频文件")
            if not file_id and len(files) > 1:
                links = []
                for file in files:
                    href = request.url.include_query_params(
                        file_id=str(file["file_id"])
                    )
                    links.append(
                        f'<li><a href="{html.escape(href.path + "?" + href.query, quote=True)}" rel="noreferrer">{html.escape(str(file.get("file_path") or file["file_name"]))}</a></li>'
                    )
                return HTMLResponse(
                    '<meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>选择下载文件</title><style>body{font:17px system-ui;max-width:900px;margin:3em auto;padding:1em}li{margin:1em}</style><h1>'
                    + html.escape(item.title)
                    + "</h1><p>选择视频后由浏览器连接115下载。</p><ul>"
                    + "".join(links)
                    + "</ul>",
                    headers=headers,
                )
            selected = (
                next((file for file in files if str(file["file_id"]) == file_id), None)
                if file_id
                else files[0]
            )
            if selected is None:
                raise HTTPException(404, "分享中没有该视频文件")
            direct_url = resolver.resolve_file_url(
                url, str(selected["file_id"]), user_agent, force_refresh=True
            )
        if not http_url(direct_url):
            raise HTTPException(502, "115 未返回有效下载地址")
        return RedirectResponse(direct_url, status_code=302, headers=headers)
    except HTTPException:
        raise
    except Exception as error:
        code = getattr(error, "status_code", 502)
        code = code if isinstance(code, int) and 400 <= code <= 599 else 502
        message = (
            "115 尚未完成离线解析，请稍后刷新重试"
            if item.cloud in {"magnet", "ed2k"}
            else "115 解析失败，请检查分享是否有效及115账号状态"
        )
        return HTMLResponse(
            '<meta charset="utf-8"><p>' + message + "</p>",
            status_code=code,
            headers=headers,
        )
