import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

from app.adapters.network.browser import launch_browser_context
from app.core.config import settings

from .link_parser import (
    extract_resource_links,
    extract_resource_rows,
    resource_link_type,
)


class Re0BrowserError(Exception):
    """RE0 浏览器操作异常"""


class Re0LoginError(Re0BrowserError):
    """RE0 登录异常"""


class Re0BrowserClient:
    """在单线程内复用 RE0 浏览器上下文"""

    BASE_URL = "https://re0.me"
    OPERATION_TIMEOUT = 90

    def __init__(self) -> None:
        """初始化串行浏览器执行器"""
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="re0-browser",
        )
        self._context: Any = None
        self._page: Any = None
        self._username = ""
        self._password = ""
        self._proxy = ""
        self._headless = True
        self._closed = False
        self._config_lock = threading.RLock()
        self._last_activity = 0.0

    def configure(
        self,
        username: str,
        password: str,
        proxy: str = "",
        headless: bool = True,
    ) -> None:
        """更新登录配置并在配置变化时重置会话"""
        new_config = (
            str(username or "").strip(),
            str(password or ""),
            str(proxy or "").strip(),
            bool(headless),
        )
        with self._config_lock:
            old_config = (
                self._username,
                self._password,
                self._proxy,
                self._headless,
            )
            self._username, self._password, self._proxy, self._headless = new_config
        if old_config != new_config and (self._context or self._page):
            self.reset()

    def status(self) -> Dict[str, Any]:
        """返回浏览器会话状态"""
        return {
            "configured": bool(self._username and self._password),
            "session_open": bool(self._context and self._page),
            "last_activity": self._last_activity or None,
            "base_url": self.BASE_URL,
        }

    def test_login(self) -> Dict[str, Any]:
        """验证 RE0 登录状态"""
        return self._submit(self._test_login_worker)

    def search_resources(
        self,
        media_type: str,
        tmdb_id: str,
    ) -> List[Dict[str, Any]]:
        """按 TMDB 媒体标识搜索 RE0 资源"""
        return self._submit(self._search_resources_worker, media_type, tmdb_id)

    def unlock_resource(self, href: str, expected_points: int) -> Dict[str, Any]:
        """打开资源页并在需要时执行一次确认解锁"""
        return self._submit(self._unlock_resource_worker, href, expected_points)

    def checkin(self) -> Dict[str, Any]:
        """执行一次每日签到"""
        return self._submit(self._checkin_worker)

    def reset(self) -> None:
        """关闭当前浏览器上下文"""
        if self._closed:
            return
        self._submit(self._close_worker)

    def close(self) -> None:
        """关闭浏览器与执行器"""
        if self._closed:
            return
        try:
            self._submit(self._close_worker)
        finally:
            self._closed = True
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _submit(self, func: Any, *args: Any) -> Any:
        """把所有浏览器操作固定到同一线程"""
        if self._closed:
            raise Re0BrowserError("RE0 浏览器客户端已关闭")
        future = self._executor.submit(func, *args)
        return future.result(timeout=self.OPERATION_TIMEOUT)

    def _ensure_page(self) -> Any:
        """按需创建持久浏览器上下文和页面"""
        if self._context is not None and self._page is not None:
            return self._page
        launch_kwargs: Dict[str, Any] = {
            "viewport": {"width": 1440, "height": 1000},
            "humanize": getattr(settings, "CLOAKBROWSER_HUMANIZE", True),
        }
        human_preset = getattr(settings, "CLOAKBROWSER_HUMAN_PRESET", None)
        if human_preset:
            launch_kwargs["human_preset"] = human_preset
        if self._proxy:
            launch_kwargs["proxy"] = self._proxy
        try:
            self._context = launch_browser_context(
                headless=self._headless,
                **launch_kwargs,
            )
            self._page = self._context.new_page()
            self._page.set_default_timeout(30000)
            return self._page
        except Exception as error:
            self._close_worker()
            raise Re0BrowserError(f"无法启动 MoviePilot 内置浏览器：{error}") from error

    def _ensure_login(self) -> Any:
        """确保当前持久上下文已登录 RE0"""
        if not self._username or not self._password:
            raise Re0LoginError("请先在插件设置中填写 RE0 用户名和密码")
        page = self._ensure_page()
        try:
            page.goto(
                f"{self.BASE_URL}/manager/my-apps",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            page.wait_for_timeout(800)
        except Exception as error:
            raise Re0BrowserError(f"打开 RE0 失败：{error}") from error
        if "/login" not in str(page.url):
            self._last_activity = time.time()
            return page
        username_selectors = (
            'input[name="username"]',
            'input[name="email"]',
            'input[type="text"]',
        )
        password_selectors = (
            'input[name="password"]',
            'input[type="password"]',
        )
        username_selector = self._first_visible_selector(page, username_selectors)
        password_selector = self._first_visible_selector(page, password_selectors)
        if not username_selector or not password_selector:
            raise Re0LoginError("RE0 登录页结构已变化，未找到账号或密码输入框")
        try:
            page.fill(username_selector, self._username)
            page.fill(password_selector, self._password)
            page.locator('button[type="submit"]').first.click()
            page.wait_for_timeout(1800)
        except Exception as error:
            raise Re0LoginError(f"提交 RE0 登录表单失败：{error}") from error
        if "/login" in str(page.url):
            message = self._body_text(page)
            if "验证码" in message or "captcha" in message.lower():
                raise Re0LoginError("RE0 要求验证码，当前无头自动登录无法继续")
            raise Re0LoginError("RE0 登录失败，请检查账号和密码")
        self._last_activity = time.time()
        return page

    @staticmethod
    def _first_visible_selector(page: Any, selectors: Any) -> Optional[str]:
        """返回第一个可见选择器"""
        for selector in selectors:
            try:
                if page.locator(selector).first.is_visible():
                    return selector
            except Exception:
                continue
        return None

    @staticmethod
    def _body_text(page: Any) -> str:
        """安全读取页面正文"""
        try:
            return str(page.locator("body").inner_text(timeout=3000) or "")
        except Exception:
            return ""

    def _test_login_worker(self) -> Dict[str, Any]:
        """在线程内测试登录"""
        page = self._ensure_login()
        return {"success": True, "url": str(page.url)}

    def _search_resources_worker(
        self,
        media_type: str,
        tmdb_id: str,
    ) -> List[Dict[str, Any]]:
        """在线程内抓取资源卡片"""
        normalized_type = "tv" if str(media_type).lower() in {"tv", "电视剧"} else "movie"
        safe_tmdb_id = str(tmdb_id or "").strip()
        if not safe_tmdb_id.isdigit():
            raise Re0BrowserError("TMDB ID 必须是数字")
        page = self._ensure_login()
        detail_url = f"{self.BASE_URL}/tmdb/{normalized_type}/{safe_tmdb_id}"
        captured: List[Dict[str, Any]] = []

        def _capture_response(response: Any) -> None:
            """收集页面异步接口中的资源对象"""
            try:
                if response.status < 200 or response.status >= 300:
                    return
                content_type = str(response.headers.get("content-type", ""))
                if "json" not in content_type:
                    return
                captured.extend(extract_resource_rows(response.json()))
            except Exception:
                return

        page.on("response", _capture_response)
        try:
            page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
            if "/login" in str(page.url):
                raise Re0LoginError("RE0 登录状态已失效")
            resources = captured or page.evaluate(self._resource_cards_script()) or []
        except Re0BrowserError:
            raise
        except Exception as error:
            raise Re0BrowserError(f"RE0 资源搜索失败：{error}") from error
        finally:
            self._remove_response_listener(page, _capture_response)
        output: List[Dict[str, Any]] = []
        seen = set()
        for raw in resources:
            href = str(raw.get("href") or "").strip()
            slug = str(raw.get("slug") or "").strip() or self._slug_from_href(href)
            if not slug or slug in seen:
                continue
            seen.add(slug)
            raw["href"] = urljoin(self.BASE_URL, href)
            raw["slug"] = slug
            output.append(raw)
        self._last_activity = time.time()
        return output

    def _unlock_resource_worker(
        self,
        href: str,
        expected_points: int,
    ) -> Dict[str, Any]:
        """在线程内打开资源详情并提取链接"""
        safe_url = self._safe_resource_url(href)
        page = self._ensure_login()
        captured: List[str] = []
        captured_resources: List[Dict[str, Any]] = []

        def _capture_response(response: Any) -> None:
            try:
                if response.status < 200 or response.status >= 300:
                    return
                content_type = str(response.headers.get("content-type", ""))
                if "json" not in content_type:
                    return
                body = response.json()
                captured.extend(extract_resource_links(body))
                captured_resources.extend(extract_resource_rows(body))
            except Exception:
                return

        page.on("response", _capture_response)
        try:
            page.goto(safe_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1200)
            existing = captured or self._extract_page_links(page)
            if existing:
                return self._link_result(existing, already_owned=True)
            confirm = None
            used_fallback = False
            for label in ("确认解锁", "确定解锁", "免费解锁"):
                candidate = page.get_by_text(label, exact=True).first
                try:
                    if candidate.is_visible():
                        confirm = candidate
                        break
                except Exception:
                    continue
            if confirm is None:
                try:
                    candidate = page.locator("button").filter(has_text="解锁").first
                    if candidate.is_visible():
                        confirm = candidate
                        used_fallback = True
                except Exception:
                    confirm = None
            if confirm is None:
                body = self._body_text(page)
                if "积分不足" in body:
                    raise Re0BrowserError("RE0 积分不足，无法解锁该资源")
                raise Re0BrowserError("未找到资源链接或确认解锁按钮，页面结构可能已变化")
            current_points = self._current_unlock_points(
                confirm,
                captured_resources,
            )
            if current_points is None:
                raise Re0BrowserError("无法复核资源当前积分，为避免误扣分已拒绝解锁")
            if current_points != expected_points:
                raise Re0BrowserError(
                    f"资源积分已从 {expected_points} 变为 {current_points}，请重新搜索确认"
                )
            confirm.click()
            if used_fallback:
                page.wait_for_timeout(700)
                if not captured and not self._extract_page_links(page):
                    for label in ("确认解锁", "确定解锁", "免费解锁"):
                        secondary = page.get_by_text(label, exact=True).first
                        try:
                            if secondary.is_visible():
                                secondary.click()
                                break
                        except Exception:
                            continue
            deadline = time.time() + 20
            links: List[str] = []
            while time.time() < deadline:
                links = captured or self._extract_page_links(page)
                if links:
                    break
                page.wait_for_timeout(400)
            if not links:
                raise Re0BrowserError("解锁已提交，但未能从页面响应中取得资源链接")
            self._last_activity = time.time()
            return self._link_result(links, already_owned=False)
        except Re0BrowserError:
            raise
        except Exception as error:
            raise Re0BrowserError(f"RE0 资源解锁失败：{error}") from error
        finally:
            self._remove_response_listener(page, _capture_response)

    def _checkin_worker(self) -> Dict[str, Any]:
        """在线程内执行签到"""
        page = self._ensure_login()
        try:
            page.goto(f"{self.BASE_URL}/manager", wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            body = self._body_text(page)
            if "今日已签到" in body or "已签到" in body:
                return {"success": True, "message": "今日已经签到"}
            button = page.get_by_text("签到", exact=True).first
            if not button.is_visible():
                raise Re0BrowserError("未找到签到入口，RE0 页面结构可能已变化")
            button.click()
            page.wait_for_timeout(1200)
            body = self._body_text(page)
            if "签到" not in body:
                return {"success": True, "message": "签到请求已提交"}
            return {"success": True, "message": "签到操作已完成"}
        except Re0BrowserError:
            raise
        except Exception as error:
            raise Re0BrowserError(f"RE0 签到失败：{error}") from error

    @staticmethod
    def _resource_cards_script() -> str:
        """返回当前 RE0 资源卡片提取脚本"""
        return r"""
        () => {
            const anchors = [...document.querySelectorAll('a[href*="/resource/"]')];
            const pointsRe = /(\d+)\s*积分/;
            const sizeRe = /(\d+(?:\.\d+)?)\s*(TB|GB|MB|G|M)\b/i;
            const resolutionRe = /\b(8K|4K|2K|2160P?|1080P?|720P?)\b/i;
            const dateRe = /(?:发布于|更新于)\s*([\d/\-]+)/;
            return anchors.map(anchor => {
                let card = anchor;
                for (let i = 0; i < 6 && card.parentElement; i++) {
                    const candidate = card.parentElement;
                    const text = (candidate.innerText || '').trim();
                    if (text.length > 30 && text.length < 1800) card = candidate;
                    if (/积分|免费|115|磁力|ED2K/i.test(text)) break;
                }
                const text = (card.innerText || anchor.innerText || '').trim();
                const lines = text.split('\n').map(v => v.trim()).filter(Boolean);
                const points = text.match(pointsRe);
                const size = text.match(sizeRe);
                const resolution = text.match(resolutionRe);
                const date = text.match(dateRe);
                let provider = '';
                if (/115/i.test(text)) provider = '115';
                else if (/ED2K|电驴/i.test(text)) provider = 'ed2k';
                else if (/磁力|magnet/i.test(text)) provider = 'magnet';
                const isFree = /免费/.test(text);
                const title = lines.find(line =>
                    line.length > 3 &&
                    !/^\d+\s*积分$/.test(line) &&
                    !/^(发布于|更新于)/.test(line) &&
                    !/^\d+(?:\.\d+)?\s*(TB|GB|MB|G|M)$/i.test(line)
                ) || '';
                return {
                    href: anchor.getAttribute('href') || '',
                    title,
                    provider,
                    size: size ? `${size[1]} ${size[2].toUpperCase()}` : '',
                    resolution: resolution ? resolution[1].toUpperCase() : '',
                    posted_at: date ? date[1] : '',
                    is_free: isFree,
                    unlock_points: isFree ? 0 : (points ? Number(points[1]) : null),
                    already_owned: /已解锁|复制链接/.test(text),
                };
            });
        }
        """

    @staticmethod
    def _extract_page_links(page: Any) -> List[str]:
        """从页面表单、链接和可见文本提取资源链接"""
        values = page.evaluate(
            r"""
            () => {
                const values = [];
                for (const el of document.querySelectorAll('input,textarea,a,code,pre')) {
                    values.push(el.value || el.href || el.textContent || '');
                }
                values.push(document.body?.innerText || '');
                return values;
            }
            """
        )
        return extract_resource_links(values)

    @staticmethod
    def _remove_response_listener(page: Any, listener: Any) -> None:
        """移除临时响应监听器，避免持久页面累积回调"""
        try:
            page.remove_listener("response", listener)
        except Exception:
            return

    @staticmethod
    def _current_unlock_points(
        button: Any,
        resources: List[Dict[str, Any]],
    ) -> Optional[int]:
        """从详情响应或操作按钮复核当前解锁积分"""
        for resource in resources:
            points = resource.get("unlock_points")
            if points is not None:
                return int(points)
        try:
            text = str(button.inner_text() or "")
        except Exception:
            text = ""
        if "免费" in text:
            return 0
        match = re.search(r"(\d+)\s*积分", text)
        return int(match.group(1)) if match else None

    @staticmethod
    def _link_result(links: List[str], already_owned: bool) -> Dict[str, Any]:
        """生成不记录到日志的解锁结果"""
        return {
            "success": True,
            "already_owned": already_owned,
            "links": [
                {"url": link, "type": resource_link_type(link)} for link in links
            ],
        }

    @classmethod
    def _slug_from_href(cls, href: str) -> str:
        """从资源详情地址提取 slug"""
        path = urlparse(urljoin(cls.BASE_URL, href)).path.rstrip("/")
        parts = [part for part in path.split("/") if part]
        if "resource" not in parts:
            return ""
        index = parts.index("resource")
        tail = parts[index + 1 :]
        if len(tail) not in {1, 2}:
            return ""
        slug = "/".join(tail)
        if not all(
            part and all(char.isalnum() or char in "._-" for char in part)
            for part in tail
        ):
            return ""
        return slug

    @classmethod
    def _safe_resource_url(cls, href: str) -> str:
        """校验待打开的资源地址属于 RE0"""
        url = urljoin(cls.BASE_URL, str(href or "").strip())
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "re0.me":
            raise Re0BrowserError("拒绝打开非 RE0 资源地址")
        if not cls._slug_from_href(url):
            raise Re0BrowserError("RE0 资源地址格式无效")
        return url

    def _close_worker(self) -> None:
        """在线程内关闭页面和上下文"""
        if self._page is not None:
            try:
                self._page.close()
            except Exception:
                pass
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        self._page = None
        self._context = None
