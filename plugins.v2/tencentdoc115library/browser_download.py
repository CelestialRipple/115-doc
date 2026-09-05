"""浏览器直链入口：服务器只解析目录和签发 302，不传输媒体正文。"""

from html import escape
from urllib.parse import quote, urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .library import LibraryBuilder
from .resolver import ShareResolutionError
from .signing import sign_action, verify_action
from .source_link import is_offline_link

BASE_PATH = "/api/v1/plugin/TencentDoc115Library/resources/browser/"
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


class BrowserDownloads:
    def __init__(self, store, resolver, config_provider):
        self.store = store
        self.resolver = resolver
        self.config_provider = config_provider

    def url(self, resource_id: str) -> str:
        token = sign_action(
            self.config_provider().get("action_signing_key", ""), "browser", resource_id
        )
        return (
            BASE_PATH + quote(resource_id, safe="") + "?" + urlencode({"token": token})
        )

    def handle(
        self, resource_id: str, request: Request, token: str = "", file_id: str = ""
    ):
        if not verify_action(
            self.config_provider().get("action_signing_key", ""),
            token,
            "browser",
            resource_id,
        ):
            return HTMLResponse(
                "下载链接无效或已过期，请重新搜索。", status_code=401, headers=HEADERS
            )
        resource = self.store.get_resource(resource_id)
        if not resource or resource.get("status") in {"removed", "invalid_share"}:
            return HTMLResponse(
                "资源不存在或分享已失效。", status_code=404, headers=HEADERS
            )
        user_agent = request.headers.get("user-agent", "")
        try:
            if is_offline_link(resource["share_url"]):
                direct_url = self.resolver.resolve(resource_id, user_agent=user_agent)
            else:
                files = self.resolver.list_video_files(resource["share_url"])
                if file_id:
                    selected = next(
                        (item for item in files if str(item["file_id"]) == file_id),
                        None,
                    )
                    if not selected:
                        return HTMLResponse(
                            "分享中没有该视频文件。", status_code=404, headers=HEADERS
                        )
                elif (
                    LibraryBuilder._effective_media_type(resource)
                    in {"电视剧", "剧集", "tv"}
                    and len(files) > 1
                ):
                    links = []
                    for item in files:
                        url = (
                            BASE_PATH
                            + quote(resource_id, safe="")
                            + "?"
                            + urlencode({"token": token, "file_id": item["file_id"]})
                        )
                        links.append(
                            f'<li><a href="{escape(url, quote=True)}" download rel="noreferrer">{escape(item.get("file_path") or item["file_name"])}</a></li>'
                        )
                    return HTMLResponse(
                        '<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                        '<title>选择下载文件</title><body style="max-width:900px;margin:40px auto;padding:16px;font:16px sans-serif">'
                        f"<h1>{escape(resource['title'])}</h1><p>选择一个视频，由浏览器连接 115 下载。</p><ul>{''.join(links)}</ul></body></html>",
                        headers=HEADERS,
                    )
                else:
                    selected = self.resolver.choose_movie_file(files)
                direct_url = self.resolver.resolve_file_url(
                    resource["share_url"],
                    str(selected["file_id"]),
                    user_agent,
                    force_refresh=True,
                )
            if not str(direct_url).startswith(("https://", "http://")):
                raise ShareResolutionError("115 返回了无效下载地址")
            return RedirectResponse(direct_url, status_code=302, headers=HEADERS)
        except ShareResolutionError as error:
            return JSONResponse(
                {"success": False, "message": str(error)},
                status_code=error.status_code,
                headers=HEADERS,
            )
