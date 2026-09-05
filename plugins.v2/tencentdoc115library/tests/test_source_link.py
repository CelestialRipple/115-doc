from tencentdoc115library.source_link import (
    extract_source_links,
    is_offline_link,
    offline_file_hint,
    offline_info_hash,
)


ED2K = (
    "ed2k://|file|Example.Movie.2026.iso|42714660864|"
    "19B8C66BBDCE9D7B920015F96FDC113C|/"
)


def test_ed2k_file_hint_and_hash() -> None:
    hint = offline_file_hint(ED2K)

    assert is_offline_link(ED2K)
    assert hint["file_name"] == "Example.Movie.2026.iso"
    assert hint["file_size"] == 42714660864
    assert offline_info_hash(ED2K) == "19b8c66bbdce9d7b920015f96fdc113c"


def test_magnet_base32_hash_and_display_name() -> None:
    link = "magnet:?xt=urn:btih:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA&dn=Movie.2025.mkv"

    assert offline_info_hash(link) == "00" * 20
    assert offline_file_hint(link)["file_name"] == "Movie.2025.mkv"


def test_extract_links_keeps_ed2k_spaces_until_terminator() -> None:
    value = f"标题|2026|{ED2K} 后续文字"

    links = extract_source_links(value)

    assert links == [
        {
            "kind": "ed2k",
            "url": ED2K,
            "start": value.index("ed2k://"),
            "end": value.index("ed2k://") + len(ED2K),
        }
    ]


def test_extract_links_unwraps_smart_sheet_magnet_url() -> None:
    wrapped = "https://magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"

    links = extract_source_links(wrapped)

    assert links == [
        {
            "kind": "magnet",
            "url": "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
            "start": 0,
            "end": len(wrapped) - len("https://"),
        }
    ]


def test_extract_links_unwraps_percent_encoded_offline_urls() -> None:
    wrapped_magnet = (
        "https://magnet%3A%3Fxt%3Durn%3Abtih%3A"
        "0123456789abcdef0123456789abcdef01234567"
    )
    wrapped_ed2k = (
        "https://ed2k%3A%2F%2F%7Cfile%7CExample.Movie.iso%7C1024%7C"
        "19B8C66BBDCE9D7B920015F96FDC113C%7C%2F"
    )

    magnet = extract_source_links(wrapped_magnet)
    ed2k = extract_source_links(wrapped_ed2k)

    assert magnet[0]["kind"] == "magnet"
    assert magnet[0]["url"].startswith("magnet:?")
    assert ed2k[0]["kind"] == "ed2k"
    assert ed2k[0]["url"] == (
        "ed2k://|file|Example.Movie.iso|1024|"
        "19B8C66BBDCE9D7B920015F96FDC113C|/"
    )
