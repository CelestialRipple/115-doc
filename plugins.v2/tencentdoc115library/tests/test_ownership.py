from pathlib import Path

from tencentdoc115library.ownership import record_owned


def test_cleanup_deletes_only_unchanged_owned_files(plugin):
    root = Path(plugin._config["output_root"])
    root.mkdir()
    generated = root / "generated.nfo"
    changed = root / "changed.jpg"
    user_file = root / "user.nfo"
    for path in (generated, changed, user_file):
        path.write_bytes(b"original")
    record_owned(root, [generated, changed])
    changed.write_bytes(b"user modification")
    result = plugin._builder.clear_generated_output()
    assert result["deleted_files"] == 1
    assert not generated.exists()
    assert changed.read_bytes() == b"user modification"
    assert user_file.exists()


def test_symlink_cannot_claim_or_delete_outside_file(plugin, tmp_path):
    root = Path(plugin._config["output_root"])
    root.mkdir()
    outside = tmp_path / "outside.nfo"
    outside.write_text("user metadata")
    (root / "link.nfo").symlink_to(outside)
    record_owned(root, [root / "link.nfo"])
    plugin._builder.clear_generated_output()
    assert outside.read_text() == "user metadata"
