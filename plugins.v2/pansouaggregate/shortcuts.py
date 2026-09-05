"""Configurable, browser-only website shortcuts; never fetched by the server."""

import re
from urllib.parse import quote, urlsplit

from .providers import Resource, http_url

DEFAULT_WEB_SEARCHES = """RB4K|https://www.rb4k.cn/vod/search.html?wd={keyword}
RE0|https://re0.me/search?tab=media&query={keyword}&page=1&type=multi
癫影|https://m.dian115.com/"""


def website_shortcuts(raw, keyword):
    items, errors = [], {}
    for index, line in enumerate(str(raw or "").splitlines()[:20], 1):
        if not line.strip():
            continue
        name, separator, template = line.partition("|")
        name, template = name.strip(), template.strip()
        # Placeholders only belong in path/query, never scheme or authority.
        try:
            parsed = urlsplit(template)
            valid = (
                separator
                and name
                and len(name) <= 40
                and len(template) <= 2048
                and http_url(template)
                and not any(c in parsed.netloc for c in "{}")
                and not re.search(r"[{}]", template.replace("{keyword}", ""))
            )
        except ValueError:
            valid = False
        if not valid:
            errors[f"网页入口第{index}行"] = (
                "格式应为：站点名称|搜索网址，用 {keyword} 表示搜索词"
            )
            continue
        url = template.replace("{keyword}", quote(keyword, safe=""))
        suffix = "网页搜索" if "{keyword}" in template else "网站首页"
        items.append(Resource(f"{keyword} · {name} {suffix}", url, name, "web"))
    return items, errors
