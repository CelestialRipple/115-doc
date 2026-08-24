import asyncio
import socket
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


def test_emby_url_avoids_duplicate_emby_prefix():
    config = {"emby_internal_url": "http://emby:8096/emby"}

    assert DirectPlayGateway._emby_url(
        config,
        "/emby/Items/1?api_key=test",
    ) == "http://emby:8096/emby/Items/1?api_key=test"


def test_playback_info_forces_gateway_direct_stream():
    config = {"emby_strm_paths": "/data/tencentdoc115"}
    gateway = DirectPlayGateway(lambda: config, object())
    payload = {
        "MediaSources": [
            {
                "Path": "http://moviepilot/private-play-url",
                "SupportsDirectPlay": True,
                "SupportsDirectStream": False,
                "SupportsTranscoding": True,
                "TranscodingUrl": "/Videos/1/master.m3u8",
            }
        ]
    }

    modified = gateway._modify_playback_info(
        payload,
        config,
        "/data/tencentdoc115/电影/测试.strm",
    )

    source = modified["MediaSources"][0]
    assert source["SupportsDirectPlay"] is False
    assert source["SupportsDirectStream"] is True
    assert source["SupportsTranscoding"] is False
    assert "TranscodingUrl" not in source


def test_gateway_redirects_managed_strm_and_proxies_other_requests():
    async def scenario():
        with TemporaryDirectory() as temporary_directory:
            strm_path = Path(temporary_directory) / "测试.strm"
            strm_path.write_text(
                "http://moviepilot:3000/api/v1/plugin/"
                "TencentDoc115Library/play/resource-1?token=secret&file_id=file-1",
                encoding="utf-8",
            )

            async def item_handler(request):
                if request.query.get("api_key") != "client-key":
                    raise web.HTTPUnauthorized()
                return web.json_response({"Path": str(strm_path)})

            async def public_info_handler(_request):
                return web.json_response({"ServerName": "fake-emby"})

            emby_app = web.Application()
            emby_app.router.add_get("/emby/Items/{item_id}", item_handler)
            emby_app.router.add_get(
                "/emby/System/Info/Public",
                public_info_handler,
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
                    assert user_agent == "gateway-test"
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
                        params={"api_key": "client-key"},
                        headers={"User-Agent": "gateway-test"},
                        allow_redirects=False,
                    ) as response:
                        assert response.status == 302
                        assert response.headers["Location"].startswith(
                            "https://115cdn.example/video.mkv"
                        )
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
            finally:
                await gateway._cleanup()
                await emby_runner.cleanup()

    asyncio.run(scenario())
