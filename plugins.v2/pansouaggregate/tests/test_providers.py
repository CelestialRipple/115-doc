import json
from unittest.mock import Mock

import pytest

from pansouaggregate.providers import (
    ChallengeRequired,
    PanSouClient,
    ProviderError,
    Resource,
    clean_link,
    dedupe,
    normalize_pansou,
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


def test_force_refresh_is_forwarded_to_pansou(monkeypatch):
    bodies = []

    def read(session, method, url, **kwargs):
        bodies.append(kwargs["json"])
        return '{"total":0,"merged_by_type":{}}'

    monkeypatch.setattr("pansouaggregate.providers.read_response", read)
    client = PanSouClient({"pansou_url": "http://nas:8888"})
    client.search("千与千寻")
    client.search("千与千寻", refresh=True)
    assert not bodies[0].get("refresh")
    assert bodies[1]["refresh"] is True
    assert bodies[1]["cloud_types"] == ["115", "magnet"]
