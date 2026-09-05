import json
from unittest.mock import Mock

import pytest

from pansouaggregate.providers import (
    BT4GClient,
    ChallengeRequired,
    PanSouClient,
    ProviderError,
    Resource,
    clean_link,
    dedupe,
    normalize_pansou,
    parse_bt4g,
    read_response,
)

HASH = "abcdef0123456789" * 2 + "01234567"
MAGNET = "magnet:?xt=urn:btih:" + HASH


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "http://user:pw@example.com/",
        "https://[broken",
        "magnet:?xt=urn:btih:bad",
        "https://example.com/\nheader",
    ],
)
def test_unsafe_links_rejected(value):
    assert clean_link(value) == ""


def test_magnet_identity_dedupes_titles_and_discards_external_fetch_hints():
    one = clean_link(
        MAGNET.upper().replace("MAGNET:?XT=URN:BTIH:", "magnet:?xt=urn:btih:")
        + "&dn=One&xs=http://127.0.0.1/secret"
    )
    two = clean_link(MAGNET + "&dn=Two")
    assert "127.0.0.1" not in one
    assert len(dedupe([Resource("One", one, "a"), Resource("Two", two, "b")])) == 1


def test_pansou_merge_preserves_password_and_malformed_rows_are_ignored():
    items = normalize_pansou(
        {
            "merged_by_type": {
                "115": [
                    None,
                    {
                        "url": "https://115.com/s/abc",
                        "password": "code",
                        "note": "Movie",
                        "source": "plugin:test",
                    },
                ],
                "magnet": [{"url": MAGNET, "note": "Other"}],
            }
        }
    )
    assert len(items) == 2
    assert items[0].password == "code"
    assert items[0].source == "PanSou · plugin:test"
    assert items[1].cloud == "magnet"


def test_pansou_nested_and_results_format():
    items = normalize_pansou(
        {
            "data": {
                "results": [
                    {
                        "title": "Movie",
                        "links": [
                            {"type": "quark", "url": "https://pan.quark.cn/s/abc"}
                        ],
                    }
                ]
            }
        }
    )
    assert items[0].title == "Movie"
    with pytest.raises(ProviderError):
        normalize_pansou({"error": "authentication failed"})


def test_bt4g_details_are_not_fabricated_magnets():
    items = parse_bt4g(
        '<h5><a href="/magnet/1234567890">Big Buck Bunny</a></h5><h5><a href="https://evil.test/magnet/abc">Ad</a></h5>',
        "https://bt4gprx.com",
    )
    assert len(items) == 1
    assert items[0].cloud == "bt4g"
    assert items[0].url.endswith("/magnet/1234567890")


def test_live_bt4g_notion_layout_uses_total_size_not_first_file_size():
    # Minimal fixture from the user-verified public Big Buck Bunny search page.
    html = '<div class="notion-list-item"><div class="notion-list-item-title"><a href="/magnet/kzb8HRBQ62h4eEUGBY6bKvf5ZIzBIqezD"><b>Big Buck Bunny</b> both discs</a></div><div class="notion-file-row">VIDEO_TS.BUP <span>24.00KB</span></div><div class="notion-list-item-meta">Total Size: <b class="cpill red-pill">13.69GB</b><span class="notion-seeders">2</span></div></div>'
    items = parse_bt4g(html, "https://bt4gprx.com")
    assert items[0].title == "Big Buck Bunny both discs"
    assert items[0].size == int(13.69 * 1024**3)
    assert items[0].seeders == 2


def test_live_detail_extracts_explicit_hash_without_visiting_download_host():
    body = '<h1 class="notion-detail-title">Big Buck Bunny</h1><div class="notion-btn-group"><a href="//downloadtorrentfile.com/hash/6d2d195d2e79fb1719d55ffc1982ff09bc0eaed7?name=Big-Buck-Bunny">Magnet Link</a></div>'
    items = parse_bt4g(
        body, "https://bt4gprx.com/magnet/sBWeZGLRdNGXO4NrnQbtT1TYPBbGSoy9A"
    )
    assert items[0].cloud == "magnet"
    assert "6d2d195d2e79fb1719d55ffc1982ff09bc0eaed7" in items[0].url
    assert "sBWeZ" not in items[0].url


def test_bt4g_magnet_size_and_empty_layout_failure():
    items = parse_bt4g(
        f'<li><h5>Movie</h5>1.5 GB<a href="{MAGNET}">Download</a></li>',
        "https://bt4gprx.com",
    )
    assert items[0].size == 1610612736
    assert items[0].title == "Movie"
    assert parse_bt4g("<p>No results</p>", "https://bt4gprx.com") == []
    with pytest.raises(ProviderError):
        parse_bt4g("<title>Changed site layout</title>", "https://bt4gprx.com")


def response(status=200, body=b"{}", headers=None):
    response = Mock(status_code=status, headers=headers or {})
    response.iter_content.return_value = [body]
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    session = Mock()
    session.request.return_value = response
    return session


def test_challenge_and_failed_status_are_not_empty_results():
    with pytest.raises(ChallengeRequired):
        read_response(
            response(403, b"<html>cf-chl-widget</html>"), "GET", "https://bt4gprx.com"
        )
    with pytest.raises(ProviderError, match="HTTP 401"):
        read_response(response(401), "POST", "http://nas/api/search")
    with pytest.raises(ProviderError, match="HTTP 302"):
        read_response(response(302), "POST", "http://nas/api/search")


def test_response_cap_and_no_credential_redirect():
    session = response(body=b"x" * (4 * 1024 * 1024 + 1))
    with pytest.raises(ProviderError, match="4 MiB"):
        read_response(session, "GET", "http://nas/api/search")
    assert session.request.call_args.kwargs["allow_redirects"] is False


def test_normal_cloudflare_detection_script_is_not_a_captcha():
    body = b'<title>Search results</title><script src="/cdn-cgi/challenge-platform/js"></script><p>No results</p>'
    assert (
        read_response(response(body=body), "GET", "https://bt4gprx.com")
        == body.decode()
    )


def test_pansou_endpoint_and_authorization(monkeypatch):
    calls = []

    def read(session, method, url, **kwargs):
        assert session.trust_env is False
        calls.append((url, kwargs))
        return json.dumps(
            {"token": "test-token"}
            if url.endswith("/login")
            else {"total": 0, "merged_by_type": {}}
        )

    monkeypatch.setattr("pansouaggregate.providers.read_response", read)
    assert (
        PanSouClient(
            {
                "pansou_url": "http://nas/api/search",
                "pansou_username": "user",
                "pansou_password": "private",
                "cloud_types": "115,magnet",
            }
        ).search("Big Buck Bunny")
        == []
    )
    assert calls[0][0] == "http://nas/api/auth/login"
    assert calls[1][1]["headers"]["Authorization"] == "Bearer test-token"
    assert calls[1][1]["json"]["cloud_types"] == ["115", "magnet"]


def test_bt4g_does_not_receive_pansou_token(monkeypatch):
    def read(session, method, url, **kwargs):
        assert "Authorization" not in kwargs["headers"]
        assert "private" not in str(kwargs)
        assert "q=Big+Buck+Bunny" in url
        return "<p>No results</p>"

    monkeypatch.setattr("pansouaggregate.providers.read_response", read)
    assert BT4GClient({"pansou_token": "private"}).search("Big Buck Bunny") == []
