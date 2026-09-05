import runpy
from pathlib import Path


def test_frontend_install_preserves_existing_content_and_is_idempotent(tmp_path):
    root = Path(__file__).parents[3]
    install = runpy.run_path(str(root / "integrations/install-browser-adapter.py"))[
        "install"
    ]
    public = tmp_path / "public"
    public.mkdir()
    original = b'<!doctype html><html><head><script src="existing.js"></script></head><body>existing content</body></html>'
    (public / "index.html").write_bytes(original)
    script = root / "integrations/moviepilot-browser-download.user.js"
    install(public, script)
    once = (public / "index.html").read_bytes()
    install(public, script)
    assert (public / "index.html").read_bytes() == once
    assert once.count(b"/moviepilot-aggregate-browser.js") == 1
    assert b'<script src="existing.js"></script>' in once
    assert b"existing content" in once
    assert next((public / "adapter-backups").glob("*.html")).read_bytes() == original
    assert (
        public / "moviepilot-aggregate-browser.js"
    ).read_bytes() == script.read_bytes()
