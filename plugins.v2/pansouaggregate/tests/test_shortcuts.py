from urllib.parse import parse_qs, urlsplit

from pansouaggregate.shortcuts import DEFAULT_WEB_SEARCHES, website_shortcuts
from pansouaggregate.providers import normalize_pansou


def test_defaults_encode_keyword_and_leave_dian_homepage_unchanged():
    keyword = "千与千寻 & A/#?"
    items, errors = website_shortcuts(DEFAULT_WEB_SEARCHES, keyword)
    assert not errors and len(items) == 3
    assert parse_qs(urlsplit(items[0].url).query)["wd"] == [keyword]
    assert parse_qs(urlsplit(items[1].url).query)["query"] == [keyword]
    assert parse_qs(urlsplit(items[1].url).query)["type"] == ["multi"]
    assert items[2].url == "https://m.dian115.com/"
    assert "网站首页" in items[2].title
    assert all(item.cloud == "web" for item in items)


def test_invalid_templates_cannot_inject_host_or_unsafe_protocol():
    raw = "\n".join(
        [
            "Bad|javascript:alert(1)",
            "Bad|https://{keyword}.example/search",
            "Bad|https://user:secret@example.com/",
            "Bad|https://example.com/{other}",
            "OK|https://example.com/search?q={keyword}",
        ]
    )
    items, errors = website_shortcuts(raw, "电影")
    assert len(items) == 1 and len(errors) == 4
    assert website_shortcuts("", "电影") == ([], {})


def test_pan_type_filter_precedes_limit_even_when_server_ignores_filter():
    data = {
        "merged_by_type": {
            "quark": [{"url": "https://pan.quark.cn/s/one", "note": "Other"}],
            "115": [{"url": "https://115.com/s/one", "note": "Movie"}],
            "ed2k": [
                {"url": "ed2k://|file|movie.mkv|1|" + "a" * 32 + "|/", "note": "ED2K"}
            ],
            "magnet": [{"url": "magnet:?xt=urn:btih:" + "a" * 40, "note": "Magnet"}],
        }
    }
    assert normalize_pansou(data, 1, {"115", "magnet"})[0].cloud == "115"
    assert {r.cloud for r in normalize_pansou(data, 100, {"115", "magnet"})} == {
        "115",
        "magnet",
    }
