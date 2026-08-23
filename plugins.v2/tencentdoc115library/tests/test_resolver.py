import pytest

from tencentdoc115library.resolver import ShareResolutionError, ShareResolver


def test_parse_share_url_with_password() -> None:
    share_code, receive_code = ShareResolver.parse_share_url(
        "https://115.com/s/abcd1234?password=wxyz"
    )
    assert share_code == "abcd1234"
    assert receive_code == "wxyz"


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

    files = resolver.list_video_files(
        "https://115.com/s/anonymous-test?password=abcd"
    )

    assert files[0]["file_id"] == "123"
    assert files[0]["file_name"] == "电影.mkv"
