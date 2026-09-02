import asyncio
import json
import socket
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory

from aiohttp import ClientSession, web

from tencentdoc115library.gateway import DirectPlayGateway


def _unused_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_parse_path_mappings_ignores_invalid_lines():
    mappings = DirectPlayGateway.parse_path_mappings(
        "/media/tencentdoc115|/data/tencentdoc115\n无效行\n/a|/b"
    )

    assert mappings == [
        ("/media/tencentdoc115", "/data/tencentdoc115"),
        ("/a", "/b"),
    ]


def test_local_strm_path_uses_long_prefix_mapping():
    result = DirectPlayGateway.local_strm_path(
        "/media/tencentdoc115/星火/电影.strm",
        "/media/tencentdoc115|/data/tencentdoc115",
    )

    assert result == Path("/data/tencentdoc115/星火/电影.strm")


def test_managed_path_requires_strm_and_configured_prefix():
    config = {"emby_strm_paths": "/data/tencentdoc115\n/media/other"}

    assert DirectPlayGateway._is_managed_path(
        "/data/tencentdoc115/电影/测试.strm",
        config,
    )
    assert not DirectPlayGateway._is_managed_path(
        "/data/tencentdoc1152/电影/测试.strm",
        config,
    )
    assert not DirectPlayGateway._is_managed_path(
        "/data/tencentdoc115/电影/测试.mkv",
        config,
    )


def test_select_item_path_prefers_managed_media_source():
    config = {"emby_strm_paths": "/media/tencentdoc115"}
    payload = {
        "Path": "http://moviepilot/private-play-url",
        "MediaSources": [
            {"Path": "http://moviepilot/another-play-url"},
            {"Path": "/media/tencentdoc115/电影/测试.strm"},
        ],
    }

    assert DirectPlayGateway._select_item_path(payload, config) == (
        "/media/tencentdoc115/电影/测试.strm"
    )


def test_select_item_path_returns_unmatched_strm_for_diagnostics():
    config = {"emby_strm_paths": "/wrong-prefix"}
    payload = {
        "Path": "http://moviepilot/private-play-url",
        "MediaSources": [{"Path": "/media/tencentdoc115/电影/测试.strm"}],
    }

    assert DirectPlayGateway._select_item_path(payload, config) == (
        "/media/tencentdoc115/电影/测试.strm"
    )


def test_emby_url_avoids_duplicate_emby_prefix():
    config = {"emby_internal_url": "http://emby:8096/emby"}

    assert DirectPlayGateway._emby_url(
        config,
        "/emby/Items/1?api_key=test",
    ) == "http://emby:8096/emby/Items/1?api_key=test"


def test_playback_info_forces_gateway_direct_play():
    config = {"emby_strm_paths": "/data/tencentdoc115"}
    gateway = DirectPlayGateway(lambda: config, object())
    payload = {
        "MediaSources": [
            {
                "Id": "source-1",
                "Path": "http://moviepilot/private-play-url",
                "SupportsDirectPlay": True,
                "SupportsDirectStream": False,
                "SupportsTranscoding": True,
                "TranscodingUrl": "/Videos/1/master.m3u8",
                "TranscodingContainer": "ts",
                "TranscodingSubProtocol": "hls",
            }
        ]
    }

    modified = gateway._modify_playback_info(
        payload,
        config,
        "/data/tencentdoc115/电影/测试.strm",
        item_id="item-1",
    )

    source = modified["MediaSources"][0]
    assert source["SupportsDirectPlay"] is True
    assert source["SupportsDirectStream"] is True
    assert source["SupportsTranscoding"] is False
    assert "TranscodingUrl" not in source
    assert "TranscodingContainer" not in source
    assert "TranscodingSubProtocol" not in source
    assert source["DirectStreamUrl"] == (
        "/videos/item-1/stream?Static=true&MediaSourceId=source-1"
    )


def test_playback_info_uses_original_media_source_path():
    config = {"emby_strm_paths": "/media/tencentdoc115"}
    gateway = DirectPlayGateway(lambda: config, object())
    payload = {
        "MediaSources": [
            {
                "Id": "mediasource_31",
                "Path": "http://moviepilot/private-play-url",
                "SupportsDirectPlay": True,
                "SupportsDirectStream": False,
                "SupportsTranscoding": True,
            }
        ]
    }

    modified = gateway._modify_playback_info(
        payload,
        config,
        source_paths={
            "mediasource_31": "/media/tencentdoc115/电影/测试.strm"
        },
        item_id="item 31",
    )

    source = modified["MediaSources"][0]
    assert source["SupportsDirectPlay"] is True
    assert source["SupportsDirectStream"] is True
    assert source["SupportsTranscoding"] is False
    assert source["DirectStreamUrl"] == (
        "/videos/item%2031/stream?Static=true&MediaSourceId=mediasource_31"
    )


def test_gateway_redirects_managed_strm_and_proxies_other_requests():
    async def scenario():
        with TemporaryDirectory() as temporary_directory:
            strm_path = Path(temporary_directory) / "测试.strm"
            strm_path.write_text(
                "http://moviepilot:3000/api/v1/plugin/"
                "TencentDoc115Library/play/resource-1?token=secret&file_id=file-1",
                encoding="utf-8",
            )

            async def items_handler(request):
                query_key = request.query.get("api_key")
                header_key = request.headers.get("X-Emby-Token")
                if query_key != "client-key" and header_key != "emby-key":
                    raise web.HTTPUnauthorized()
                assert request.headers.get("Accept-Encoding") == "identity"
                assert request.query.get("Ids") == "source-1"
                assert request.query.get("Limit") == "1"
                assert request.query.get("Fields") == "Path,MediaSources"
                assert request.query.get("Recursive") == "true"
                body = json.dumps(
                    {"Items": [{"Path": str(strm_path)}]},
                    ensure_ascii=False,
                ).encode("utf-8")
                return web.Response(
                    body=zlib.compress(body),
                    content_type="application/json",
                    headers={"Content-Encoding": "deflate"},
                )

            async def playback_info_handler(request):
                if request.query.get("api_key") != "client-key":
                    raise web.HTTPUnauthorized()
                return web.json_response(
                    {
                        "MediaSources": [
                            {
                                "Id": "mediasource_source-1",
                                "Path": "http://moviepilot/private-play-url",
                                "SupportsDirectPlay": False,
                                "SupportsDirectStream": False,
                                "SupportsTranscoding": True,
                                "TranscodingUrl": "/Videos/1/master.m3u8",
                            }
                        ]
                    }
                )

            async def public_info_handler(_request):
                return web.json_response({"ServerName": "fake-emby"})

            async def login_handler(request):
                assert request.headers.get("Accept-Encoding") == "identity"
                assert (await request.post())["Username"] == "test-user"
                body = json.dumps(
                    {"AccessToken": "login-token", "User": {"Name": "test-user"}}
                ).encode("utf-8")
                return web.Response(
                    body=zlib.compress(body),
                    content_type="application/json",
                    headers={"Content-Encoding": "deflate"},
                )

            emby_app = web.Application()
            emby_app.router.add_get("/emby/Items", items_handler)
            emby_app.router.add_post(
                "/emby/Items/1/PlaybackInfo",
                playback_info_handler,
            )
            emby_app.router.add_get(
                "/emby/System/Info/Public",
                public_info_handler,
            )
            emby_app.router.add_post(
                "/emby/Users/authenticatebyname",
                login_handler,
            )
            emby_runner = web.AppRunner(emby_app)
            await emby_runner.setup()
            emby_port = _unused_port()
            await web.TCPSite(emby_runner, "127.0.0.1", emby_port).start()

            class FakeResolver:
                @staticmethod
                def resolve(resource_id, file_id=None, user_agent=""):
                    assert resource_id == "resource-1"
                    assert file_id == "file-1"
                    assert user_agent in {"gateway-test", "infuse-test"}
                    return "https://115cdn.example/video.mkv?temporary=1"

            gateway_port = _unused_port()
            config = {
                "direct_gateway_enabled": True,
                "direct_gateway_port": gateway_port,
                "emby_internal_url": f"http://127.0.0.1:{emby_port}",
                "emby_api_key": "emby-key",
                "emby_strm_paths": temporary_directory,
                "emby_path_mappings": "",
                "playback_token": "secret",
            }
            gateway = DirectPlayGateway(lambda: config, FakeResolver())
            await gateway._serve()
            try:
                async with ClientSession() as client:
                    async with client.get(
                        f"http://127.0.0.1:{gateway_port}/emby/Items/1/Download",
                        params={
                            "api_key": "client-key",
                            "mediaSourceId": "mediasource_source-1",
                        },
                        headers={"User-Agent": "gateway-test"},
                        allow_redirects=False,
                    ) as response:
                        assert response.status == 302
                        assert response.headers["Location"].startswith(
                            "https://115cdn.example/video.mkv"
                        )
                    async with client.post(
                        f"http://127.0.0.1:{gateway_port}/emby/Items/1/PlaybackInfo",
                        params={"api_key": "client-key"},
                        headers={"User-Agent": "infuse-test"},
                        json={},
                    ) as response:
                        assert response.status == 200
                        source = (await response.json())["MediaSources"][0]
                        assert source["SupportsDirectPlay"] is True
                        assert source["SupportsDirectStream"] is True
                        assert source["SupportsTranscoding"] is False
                        assert source["DirectStreamUrl"] == (
                            "/videos/1/stream?Static=true&"
                            "MediaSourceId=mediasource_source-1"
                        )
                    async with client.get(
                        f"http://127.0.0.1:{gateway_port}/emby/Videos/1/stream.mkv",
                        params={"MediaSourceId": "mediasource_source-1"},
                        headers={"User-Agent": "infuse-test"},
                        allow_redirects=False,
                    ) as response:
                        assert response.status == 302
                        assert response.headers["Location"].startswith(
                            "https://115cdn.example/video.mkv"
                        )
                    async with client.get(
                        f"http://127.0.0.1:{gateway_port}/emby/Videos/1/stream.mkv",
                        params={"MediaSourceId": "mediasource_source-1"},
                        headers={"User-Agent": "different-client"},
                        allow_redirects=False,
                    ) as response:
                        assert response.status != 302
                    async with client.get(
                        f"http://127.0.0.1:{gateway_port}/emby/Items/1/Download",
                        headers={"User-Agent": "gateway-test"},
                        allow_redirects=False,
                    ) as response:
                        assert response.status != 302
                    async with client.get(
                        f"http://127.0.0.1:{gateway_port}/emby/System/Info/Public"
                    ) as response:
                        assert response.status == 200
                        assert (await response.json())["ServerName"] == "fake-emby"
                    async with client.post(
                        f"http://127.0.0.1:{gateway_port}/emby/Users/authenticatebyname",
                        data={"Username": "test-user", "Pw": "test-password"},
                    ) as response:
                        assert response.status == 200
                        assert response.headers["Content-Encoding"] == "deflate"
                        assert (await response.json())["AccessToken"] == "login-token"
            finally:
                await gateway._cleanup()
                await emby_runner.cleanup()

    asyncio.run(scenario())
