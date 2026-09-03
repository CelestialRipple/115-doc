import pytest

from tencentdoc115library.resolver import ShareResolutionError, ShareResolver


def test_parse_share_url_with_password() -> None:
    share_code, receive_code = ShareResolver.parse_share_url(
        "https://115.com/s/abcd1234?password=wxyz"
    )
    assert share_code == "abcd1234"
    assert receive_code == "wxyz"


def test_parse_share_url_accepts_115_alternate_domains() -> None:
    share_code, receive_code = ShareResolver.parse_share_url(
        "https://115cdn.com/s/swsbevb3hkk?password=sk4k&"
    )
    assert share_code == "swsbevb3hkk"
    assert receive_code == "sk4k"


def test_parse_invalid_share_url_reports_honest_error() -> None:
    with pytest.raises(ShareResolutionError, match="无法.*分享码"):
        ShareResolver.parse_share_url("https://example.com/not-a-share")


def test_movie_selection_uses_largest_video() -> None:
    selected = ShareResolver.choose_movie_file(
        [
            {"file_id": "1", "file_size": 100, "file_name": "sample.mp4"},
            {"file_id": "2", "file_size": 1000, "file_name": "movie.mkv"},
        ]
    )
    assert selected["file_id"] == "2"


def test_share_listing_is_anonymous_and_does_not_load_cookie() -> None:
    resolver = ShareResolver(
        store=object(),
        config_provider=lambda: {
            "request_interval": 0.1,
            "request_retries": 0,
            "share_page_size": 1000,
        },
    )
    resolver._cookie = lambda: (_ for _ in ()).throw(
        AssertionError("匿名枚举不应读取 Cookie")
    )
    resolver._anonymous_share_snap = lambda payload: {
        "state": True,
        "data": {
            "count": 1,
            "list": [{"fid": "123", "n": "电影.mkv", "s": 2048}],
        },
    }

    files = resolver.list_video_files("https://115.com/s/anonymous-test?password=abcd")

    assert files[0]["file_id"] == "123"
    assert files[0]["file_name"] == "电影.mkv"


def _caching_resolver(calls: list, ttl: int = 600) -> ShareResolver:
    resolver = ShareResolver(
        store=object(),
        config_provider=lambda: {"direct_url_cache_ttl": ttl},
    )
    resolver._download_url = lambda share_url, file_id, user_agent="": (
        calls.append((share_url, file_id, user_agent))
        or f"https://115cdn.example/{file_id}?n={len(calls)}"
    )
    return resolver


def test_direct_url_is_cached_per_file_and_user_agent() -> None:
    calls: list = []
    resolver = _caching_resolver(calls)
    share_url = "https://115.com/s/abcd1234?password=wxyz"

    first = resolver.resolve_file_url(share_url, "file-1", "infuse")
    second = resolver.resolve_file_url(share_url, "file-1", "infuse")

    assert first == second
    assert len(calls) == 1
    resolver.resolve_file_url(share_url, "file-1", "emby")
    assert len(calls) == 2

def test_direct_url_cache_can_be_invalidated_and_disabled() -> None:
    calls: list = []
    resolver = _caching_resolver(calls)
    share_url = "https://115.com/s/abcd1234?password=wxyz"

    resolver.resolve_file_url(share_url, "file-1", "infuse")
    resolver.invalidate_file_url(share_url, "file-1")
    resolver.resolve_file_url(share_url, "file-1", "infuse")
    assert len(calls) == 2

    resolver.clear_url_cache()
    resolver.resolve_file_url(share_url, "file-1", "infuse")
    assert len(calls) == 3

    disabled = _caching_resolver(calls, ttl=0)
    disabled.resolve_file_url(share_url, "file-2", "infuse")
    disabled.resolve_file_url(share_url, "file-2", "infuse")
    assert len(calls) == 5


def test_direct_url_force_refresh_bypasses_cache() -> None:
    calls: list = []
    resolver = _caching_resolver(calls)
    share_url = "https://115.com/s/abcd1234?password=wxyz"

    first = resolver.resolve_file_url(
        share_url, "file-iso", "infuse", force_refresh=True
    )
    second = resolver.resolve_file_url(
        share_url, "file-iso", "infuse", force_refresh=True
    )

    assert first != second
    assert len(calls) == 2
