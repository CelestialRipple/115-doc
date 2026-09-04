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
