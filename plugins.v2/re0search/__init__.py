import asyncio
import secrets
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from fastapi import HTTPException, Request
from starlette.responses import HTMLResponse

from app.chain.media import MediaChain
from app.log import logger
from app.plugins import _PluginBase

from .browser import Re0BrowserClient
from .link_parser import public_resource
from .schemas import LibrarySaveRequest, ResourceSearchRequest, UnlockRequest


class Re0Search(_PluginBase):
    """通过 MoviePilot 内置浏览器搜索和解锁 RE0 资源"""

    plugin_name = "RE0资源搜索"
    plugin_desc = "在独立页面中搜索、确认解锁并获取 RE0 资源链接"
    plugin_icon = "https://re0.me/favicon.ico"
    plugin_version = "0.1.2"
    plugin_author = "CelestialRipple"
    author_url = "https://github.com/CelestialRipple"
    plugin_config_prefix = "re0search_"
    plugin_order = 35
    auth_level = 1

    _enabled = False
    _username = ""
    _password = ""
    _proxy = ""
    _headless = True
    _auto_checkin = False
    _checkin_cron = "17 8 * * *"
    _ui_key = ""
    _browser: Optional[Re0BrowserClient] = None
    _media_results: Dict[str, Dict[str, Any]] = {}
    _resources: Dict[str, Dict[str, Any]] = {}
    _unlocked: Dict[str, Dict[str, Any]] = {}
    _last_error = ""
    _last_checkin = ""

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """读取配置并初始化持久浏览器客户端"""
        current = dict(config or {})
        self._enabled = bool(current.get("enabled"))
        self._username = str(current.get("username") or "").strip()
        self._password = str(current.get("password") or "")
        self._proxy = str(current.get("proxy") or "").strip()
        self._headless = bool(current.get("headless", True))
        self._auto_checkin = bool(current.get("auto_checkin"))
        self._checkin_cron = str(current.get("checkin_cron") or "17 8 * * *")
        self._ui_key = str(current.get("ui_key") or "").strip()
        if not self._ui_key:
            self._ui_key = secrets.token_urlsafe(24)
            current["ui_key"] = self._ui_key
            self.update_config(current)
        if self._browser is None:
            self._browser = Re0BrowserClient()
        self._browser.configure(
            username=self._username,
            password=self._password,
            proxy=self._proxy,
            headless=self._headless,
        )

    def get_state(self) -> bool:
        """返回插件启用状态"""
        return self._enabled

    async def media_search(self, request: Request) -> Dict[str, Any]:
        """使用 MoviePilot 元数据能力搜索媒体"""
        self._require_ui_key(request)
        keyword = str(request.query_params.get("keyword") or "").strip()
        if not keyword:
            return {"success": False, "message": "请输入电影或剧集名称"}
        try:
            _, medias = await MediaChain().async_search(title=keyword)
            results = []
            for media in medias or []:
                item = media.to_dict()
                tmdb_id = item.get("tmdb_id")
                if not tmdb_id:
                    continue
                media_type = self._media_type_value(item.get("type"))
                results.append(
                    {
                        "title": item.get("title") or item.get("name"),
                        "year": item.get("year"),
                        "overview": item.get("overview"),
                        "poster_path": item.get("poster_path"),
                        "media_type": media_type,
                        "tmdb_id": str(tmdb_id),
                    }
                )
            now = time.time()
            for item in results:
                cache_key = f"{item['media_type']}:{item['tmdb_id']}"
                self._media_results[cache_key] = {**item, "cached_at": now}
            self._prune_resources()
            return {"success": True, "items": results[:20]}
        except Exception as error:
            self._record_error(f"MoviePilot 媒体搜索失败：{error}")
            return {"success": False, "message": self._last_error}

    async def resource_search(
        self,
        payload: ResourceSearchRequest,
        request: Request,
    ) -> Dict[str, Any]:
        """按所选 TMDB 媒体搜索 RE0 资源"""
        self._require_ui_key(request)
        if not self._enabled or self._browser is None:
            return {"success": False, "message": "请先启用插件"}
        media_key = f"{payload.media_type}:{payload.tmdb_id}"
        selected_media = self._media_results.get(media_key)
        if (
            not selected_media
            or time.time() - float(selected_media.get("cached_at") or 0) > 1800
        ):
            return {"success": False, "message": "媒体选择已过期，请重新搜索标题"}
        try:
            resources = await asyncio.to_thread(
                self._browser.search_resources,
                payload.media_type,
                payload.tmdb_id,
            )
            now = time.time()
            for resource in resources:
                slug = str(resource.get("slug") or "")
                if slug:
                    self._resources[slug] = {
                        **resource,
                        "cached_at": now,
                        "media_type": payload.media_type,
                        "tmdb_id": payload.tmdb_id,
                        "media_title": selected_media.get("title"),
                        "media_year": selected_media.get("year"),
                    }
            self._prune_resources()
            return {
                "success": True,
                "items": [public_resource(item) for item in resources],
            }
        except Exception as error:
            self._record_error(str(error))
            return {"success": False, "message": self._last_error}

    async def unlock(
        self,
        payload: UnlockRequest,
        request: Request,
    ) -> Dict[str, Any]:
        """校验搜索上下文和积分确认后解锁资源"""
        self._require_ui_key(request)
        if self._browser is None:
            return {"success": False, "message": "浏览器客户端尚未初始化"}
        cached = self._resources.get(payload.slug)
        if not cached or time.time() - float(cached.get("cached_at") or 0) > 1800:
            return {"success": False, "message": "搜索结果已过期，请重新搜索"}
        points = cached.get("unlock_points")
        if points is None:
            return {
                "success": False,
                "message": "页面未能识别该资源所需积分，为避免误扣分已拒绝解锁",
            }
        if int(points) != payload.confirmed_points:
            return {
                "success": False,
                "message": f"积分确认不一致，当前资源需要 {points} 积分",
            }
        try:
            result = await asyncio.to_thread(
                self._browser.unlock_resource,
                str(cached.get("href") or ""),
                int(points),
            )
            self._unlocked[payload.slug] = {
                "links": list(result.get("links") or []),
                "cached_at": time.time(),
                "media_title": cached.get("media_title"),
                "media_year": cached.get("media_year"),
            }
            self._prune_resources()
            return result
        except Exception as error:
            self._record_error(str(error))
            return {"success": False, "message": self._last_error}

    async def save_to_library(
        self,
        payload: LibrarySaveRequest,
        request: Request,
    ) -> Dict[str, Any]:
        """把已解锁链接交给媒体库插件的公开自选导入入口"""
        self._require_ui_key(request)
        cached = self._unlocked.get(payload.slug)
        if not cached or time.time() - float(cached.get("cached_at") or 0) > 1800:
            return {"success": False, "message": "解锁结果已过期，请重新搜索并解锁"}
        group_name = payload.group_name.strip()
        if group_name in {".", ".."} or "/" in group_name or "\\" in group_name:
            return {"success": False, "message": "文件夹不能包含斜杠或使用相对路径"}
        links = [
            str(item.get("url") or "").strip()
            for item in cached.get("links") or []
            if str(item.get("url") or "").strip()
        ]
        if not links:
            return {"success": False, "message": "当前解锁结果中没有可保存的链接"}
        title = str(cached.get("media_title") or "RE0资源").strip()
        title = title.replace("|", " ").replace("\n", " ").strip()
        year = str(cached.get("media_year") or "").strip()
        prefix = f"{title}|{year}" if year else title
        import_text = "\n".join(f"{prefix}|{link}" for link in links)
        try:
            try:
                from app.runtime.extensions.plugin_manager import PluginManager
            except ImportError:
                from app.core.plugin import PluginManager

            target = PluginManager().running_plugins.get("TencentDoc115Library")
            if target is None or not hasattr(target, "import_manual_resources"):
                return {
                    "success": False,
                    "message": "未运行支持自选导入的腾讯文档115媒体库插件",
                }
            action = SimpleNamespace(
                links=import_text,
                group_name=group_name,
                media_mode=payload.media_mode,
            )
            response = await asyncio.to_thread(target.import_manual_resources, action)
            return {
                "success": bool(getattr(response, "success", False)),
                "message": str(getattr(response, "message", "导入请求已提交")),
            }
        except Exception as error:
            self._record_error(f"保存到媒体库失败：{error}")
            return {"success": False, "message": self._last_error}

    async def login_test(self, request: Request) -> Dict[str, Any]:
        """测试当前配置能否登录 RE0"""
        self._require_ui_key(request)
        if self._browser is None:
            return {"success": False, "message": "浏览器客户端尚未初始化"}
        try:
            result = await asyncio.to_thread(self._browser.test_login)
            return result
        except Exception as error:
            self._record_error(str(error))
            return {"success": False, "message": self._last_error}

    async def manual_checkin(self, request: Request) -> Dict[str, Any]:
        """执行用户主动发起的签到"""
        self._require_ui_key(request)
        return await asyncio.to_thread(self._run_checkin)

    def status(self, request: Request) -> Dict[str, Any]:
        """返回插件和浏览器会话状态"""
        self._require_ui_key(request)
        browser_status = self._browser.status() if self._browser else {}
        return {
            "success": True,
            "enabled": self._enabled,
            "browser": browser_status,
            "cached_resources": len(self._resources),
            "last_checkin": self._last_checkin,
            "last_error": self._last_error,
        }

    def ui(self, request: Request) -> HTMLResponse:
        """返回独立的 RE0 资源搜索页面"""
        self._require_ui_key(request)
        return HTMLResponse(self._ui_html())

    def get_api(self) -> List[Dict[str, Any]]:
        """注册独立页面和资源操作接口"""
        return [
            {
                "path": "/ui",
                "endpoint": self.ui,
                "methods": ["GET"],
                "summary": "RE0 独立搜索页面",
                "allow_anonymous": True,
            },
            {
                "path": "/media/search",
                "endpoint": self.media_search,
                "methods": ["GET"],
                "summary": "搜索 MoviePilot 媒体元数据",
                "allow_anonymous": True,
            },
            {
                "path": "/resources/search",
                "endpoint": self.resource_search,
                "methods": ["POST"],
                "summary": "搜索 RE0 资源",
                "allow_anonymous": True,
            },
            {
                "path": "/resources/unlock",
                "endpoint": self.unlock,
                "methods": ["POST"],
                "summary": "确认并解锁 RE0 资源",
                "allow_anonymous": True,
            },
            {
                "path": "/library/save",
                "endpoint": self.save_to_library,
                "methods": ["POST"],
                "summary": "把已解锁资源保存到媒体库",
                "allow_anonymous": True,
            },
            {
                "path": "/login/test",
                "endpoint": self.login_test,
                "methods": ["POST"],
                "summary": "测试 RE0 登录",
                "allow_anonymous": True,
            },
            {
                "path": "/checkin",
                "endpoint": self.manual_checkin,
                "methods": ["POST"],
                "summary": "执行 RE0 签到",
                "allow_anonymous": True,
            },
            {
                "path": "/status",
                "endpoint": self.status,
                "methods": ["GET"],
                "summary": "查看 RE0 插件状态",
                "allow_anonymous": True,
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """按配置注册每日签到任务"""
        if not self._enabled or not self._auto_checkin:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._checkin_cron)
        except Exception as error:
            logger.error(f"【RE0资源搜索】签到周期无效：{error}")
            return []
        return [
            {
                "id": "Re0Search_daily_checkin",
                "name": "RE0 每日签到",
                "trigger": trigger,
                "func": self._run_checkin,
                "kwargs": {},
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单"""
        form = [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "headless",
                                            "label": "无头浏览器",
                                            "hint": "群晖套件建议保持开启",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "auto_checkin",
                                            "label": "每日自动签到",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._text_col("username", "RE0 用户名或邮箱", 6),
                            self._text_col("password", "RE0 密码", 6, "password"),
                            self._text_col(
                                "proxy",
                                "浏览器代理",
                                6,
                                hint="例如 socks5://192.168.5.194:7891",
                            ),
                            self._text_col(
                                "checkin_cron",
                                "签到周期",
                                6,
                                hint="默认每天 08:17",
                            ),
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {"type": "info", "variant": "tonal"},
                        "text": "保存配置后从插件详情页进入独立搜索页面。付费资源必须逐条确认积分，插件不会批量抓取或自动扣分",
                    },
                ],
            }
        ]
        return form, {
            "enabled": False,
            "username": "",
            "password": "",
            "proxy": "",
            "headless": True,
            "auto_checkin": False,
            "checkin_cron": "17 8 * * *",
            "ui_key": self._ui_key,
        }

    def get_page(self) -> List[dict]:
        """返回插件入口和运行状态"""
        url = f"/api/v1/plugin/Re0Search/ui?key={self._ui_key}"
        status = self._browser.status() if self._browser else {}
        state = "已配置" if status.get("configured") else "未配置账号"
        return [
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "text": "RE0 资源搜索",
                    },
                    {
                        "component": "VCardText",
                        "text": f"浏览器会话：{state} · 最近签到：{self._last_checkin or '暂无'} · 最近错误：{self._last_error or '暂无'}",
                    },
                    {
                        "component": "VCardActions",
                        "content": [
                            {
                                "component": "VBtn",
                                "props": {
                                    "href": url,
                                    "target": "_blank",
                                    "color": "primary",
                                    "variant": "tonal",
                                    "prepend-icon": "mdi-magnify",
                                },
                                "text": "打开搜索页面",
                            }
                        ],
                    },
                ],
            }
        ]

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件命令列表"""
        return []

    def stop_service(self) -> None:
        """停止插件并释放浏览器进程"""
        if self._browser:
            self._browser.close()
        self._browser = None

    def _run_checkin(self) -> Dict[str, Any]:
        """执行签到并记录结果"""
        if not self._browser:
            return {"success": False, "message": "浏览器客户端尚未初始化"}
        try:
            result = self._browser.checkin()
            self._last_checkin = str(result.get("message") or "签到完成")
            return result
        except Exception as error:
            self._record_error(str(error))
            return {"success": False, "message": self._last_error}

    def _require_ui_key(self, request: Request) -> None:
        """校验独立页面使用的随机访问密钥"""
        supplied = str(request.query_params.get("key") or "")
        if not self._ui_key or not secrets.compare_digest(supplied, self._ui_key):
            raise HTTPException(status_code=403, detail="RE0 插件页面访问密钥无效")

    def _prune_resources(self) -> None:
        """清理过期搜索上下文并限制内存占用"""
        deadline = time.time() - 1800
        self._media_results = {
            key: item
            for key, item in self._media_results.items()
            if float(item.get("cached_at") or 0) >= deadline
        }
        self._resources = {
            slug: item
            for slug, item in self._resources.items()
            if float(item.get("cached_at") or 0) >= deadline
        }
        if len(self._resources) > 500:
            ordered = sorted(
                self._resources.items(),
                key=lambda pair: float(pair[1].get("cached_at") or 0),
                reverse=True,
            )
            self._resources = dict(ordered[:500])
        self._unlocked = {
            slug: item
            for slug, item in self._unlocked.items()
            if float(item.get("cached_at") or 0) >= deadline
        }

    def _record_error(self, message: str) -> None:
        """记录脱敏后的最近错误"""
        self._last_error = str(message or "未知错误")[:500]
        logger.warning(f"【RE0资源搜索】{self._last_error}")

    @staticmethod
    def _media_type_value(value: Any) -> str:
        """把 MoviePilot 媒体类型转换成 RE0 路由值"""
        text = str(getattr(value, "value", value) or "").lower()
        return "tv" if text in {"tv", "电视剧", "剧集"} else "movie"

    @staticmethod
    def _text_col(
        model: str,
        label: str,
        md: int,
        field_type: str = "text",
        hint: str = "",
    ) -> Dict[str, Any]:
        """生成统一的文本配置列"""
        props: Dict[str, Any] = {
            "model": model,
            "label": label,
            "type": field_type,
        }
        if hint:
            props.update({"hint": hint, "persistent-hint": True})
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [{"component": "VTextField", "props": props}],
        }

    def _ui_html(self) -> str:
        """生成轻量独立搜索页面"""
        key = self._ui_key
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RE0 资源搜索</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#10131a;color:#edf1f7;margin:0}}
main{{max-width:980px;margin:0 auto;padding:24px}} .card{{background:#1a1f2b;border:1px solid #303848;border-radius:14px;padding:16px;margin:12px 0}}
button,input{{font:inherit;border-radius:9px;padding:10px 12px}} input{{width:min(520px,70vw);background:#10131a;color:#fff;border:1px solid #465064}}
button{{border:0;background:#6c63ff;color:#fff;cursor:pointer;margin:4px}} button.secondary{{background:#344054}} button.warn{{background:#b54708}}
.muted{{color:#aab3c2}} .error{{color:#ff8b8b;white-space:pre-wrap}} .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}}
a{{color:#9bbcff;word-break:break-all}} code{{white-space:pre-wrap;word-break:break-all}}
</style></head><body><main><h1>RE0 资源搜索</h1>
<div class="card"><input id="keyword" placeholder="输入电影或剧集名称"><button onclick="searchMedia()">搜索媒体</button><button class="secondary" onclick="testLogin()">测试登录</button><button class="secondary" onclick="checkin()">签到</button><div id="message" class="muted"></div></div>
<div class="card"><b>保存选项</b><p><input id="group" value="RE0" placeholder="直属输出文件夹"><select id="mode"><option value="mixed">自动识别</option><option value="movie">电影</option><option value="tv">剧集</option></select></p><span class="muted">保存操作会把已解锁链接交给腾讯文档115媒体库的自选导入任务</span></div>
<section id="media" class="grid"></section><section id="resources" class="grid"></section>
<script>
const key={key!r}; const base='/api/v1/plugin/Re0Search';
const msg=t=>document.getElementById('message').textContent=t;
async function call(path,options={{}}){{const sep=path.includes('?')?'&':'?';const r=await fetch(base+path+sep+'key='+encodeURIComponent(key),options);return await r.json();}}
async function searchMedia(){{msg('正在使用 MoviePilot 识别媒体…');document.getElementById('resources').innerHTML='';const q=document.getElementById('keyword').value;const d=await call('/media/search?keyword='+encodeURIComponent(q));if(!d.success){{msg(d.message);return}}msg('请选择正确的 TMDB 条目');document.getElementById('media').innerHTML=(d.items||[]).map(x=>`<article class="card"><h3>${{esc(x.title||'')}}</h3><div class="muted">${{esc(x.year||'')}} · ${{x.media_type==='tv'?'剧集':'电影'}} · TMDB ${{esc(x.tmdb_id)}}</div><p>${{esc(x.overview||'').slice(0,180)}}</p><button onclick="searchResources('${{x.media_type}}','${{x.tmdb_id}}')">搜索 RE0 资源</button></article>`).join('');}}
async function searchResources(type,id){{msg('正在通过内置浏览器打开 RE0…');const d=await call('/resources/search',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{media_type:type,tmdb_id:id}})}});if(!d.success){{msg(d.message);return}}msg(`找到 ${{(d.items||[]).length}} 条资源`);document.getElementById('resources').innerHTML=(d.items||[]).map(x=>`<article class="card"><h3>${{esc(x.title||'未命名资源')}}</h3><div class="muted">${{esc(x.provider||'未知类型')}} · ${{esc(x.size||'未知大小')}} · ${{x.already_owned?'已解锁':(x.unlock_points===null?'积分未知':x.unlock_points+' 积分')}}</div><button class="${{x.already_owned?'secondary':'warn'}}" onclick="unlock('${{x.slug}}',${{x.unlock_points===null?'null':x.unlock_points}},${{x.already_owned?'true':'false'}})">${{x.already_owned?'获取已解锁链接':'确认解锁并获取链接'}}</button></article>`).join('');}}
async function unlock(slug,points,owned){{if(points===null){{msg('无法识别积分，已拒绝解锁');return}}const prompt=owned?'确认读取已经解锁的资源链接？':`确认解锁？本次最多消耗 ${{points}} 积分`;if(!confirm(prompt))return;msg('正在解锁…');const d=await call('/resources/unlock',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{slug:slug,confirmed_points:points}})}});if(!d.success){{msg(d.message);return}}msg('链接已取得，不会自动转存');const links=(d.links||[]).map(x=>`<p><b>${{esc(x.type)}}</b><br><a href="${{attr(x.url)}}" target="_blank" rel="noreferrer">${{esc(x.url)}}</a></p>`).join('');document.getElementById('resources').insertAdjacentHTML('afterbegin',`<article class="card"><h3>解锁结果</h3>${{links}}<button onclick="saveLibrary('${{slug}}')">保存到媒体库</button></article>`);}}
async function saveLibrary(slug){{const group=document.getElementById('group').value;const mode=document.getElementById('mode').value;if(!group.trim()){{msg('请填写输出文件夹');return}}if(!confirm(`确认保存到媒体库文件夹 ${{group}}？`))return;msg('正在提交媒体库任务…');const d=await call('/library/save',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{slug:slug,group_name:group,media_mode:mode}})}});msg(d.message||'请求已提交');}}
async function testLogin(){{msg('正在测试登录…');const d=await call('/login/test',{{method:'POST'}});msg(d.success?'登录成功':d.message);}}
async function checkin(){{if(!confirm('确认执行一次 RE0 签到？'))return;msg('正在签到…');const d=await call('/checkin',{{method:'POST'}});msg(d.message||'签到完成');}}
function esc(v){{return String(v??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));}}
function attr(v){{return esc(v);}}
</script></main></body></html>"""
