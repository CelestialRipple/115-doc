from tencentdoc115library.download_marker import (
    build_download_marker,
    parse_download_marker,
)


def test_download_marker_round_trip() -> None:
    marker = build_download_marker("sheet-1:42")

    assert marker.startswith("magnet:?xt=urn:btih:")
    assert parse_download_marker(marker) == "sheet-1:42"


def test_regular_magnet_is_not_intercepted() -> None:
    assert parse_download_marker("magnet:?xt=urn:btih:abcdef") == ""
