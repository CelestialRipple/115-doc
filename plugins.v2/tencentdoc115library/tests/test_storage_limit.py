from tencentdoc115library.storage_limit import (
    GIB,
    configured_limit_bytes,
    directory_size,
    format_gib,
)


def test_configured_limit_bytes_supports_zero_fraction_and_invalid_values() -> None:
    assert configured_limit_bytes({"output_size_limit_gb": 0}) == 0
    assert configured_limit_bytes({"output_size_limit_gb": "1.5"}) == int(1.5 * GIB)
    assert configured_limit_bytes({"output_size_limit_gb": "invalid"}) == 0
    assert configured_limit_bytes({"output_size_limit_gb": -1}) == 0


def test_directory_size_counts_files_but_not_symlinks(tmp_path) -> None:
    nested = tmp_path / "电影" / "测试"
    nested.mkdir(parents=True)
    (nested / "movie.strm").write_bytes(b"12345")
    (nested / "movie.nfo").write_bytes(b"1234567")
    (tmp_path / "shortcut").symlink_to(nested / "movie.nfo")

    assert directory_size(tmp_path) == 12
    assert format_gib(GIB) == "1.00 GiB"
