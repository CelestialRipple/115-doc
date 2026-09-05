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


def test_multiple_assets_coexist_and_reinstall(tmp_path):
    root = Path(__file__).parents[3]
    install = runpy.run_path(str(root / "integrations/install-browser-adapter.py"))[
        "install"
    ]
    (tmp_path / "index.html").write_text(
        "<html><head></head><body>MoviePilot</body></html>"
    )
    search = root / "integrations/moviepilot-browser-download.user.js"
    calendar = root / "integrations/moviepilot-bluray-calendar.js"
    for _ in range(2):
        install(tmp_path, search)
        install(tmp_path, calendar, "moviepilot-bluray-calendar.js")
    html = (tmp_path / "index.html").read_text()
    assert html.count("/moviepilot-aggregate-browser.js") == 1
    assert html.count("/moviepilot-bluray-calendar.js") == 1
    assert (
        tmp_path / "moviepilot-aggregate-browser.js"
    ).read_bytes() == search.read_bytes()
    assert (
        tmp_path / "moviepilot-bluray-calendar.js"
    ).read_bytes() == calendar.read_bytes()
