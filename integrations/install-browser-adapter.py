"""Install the browser click adapter into an existing MoviePilot static frontend.

Usage: python3 install-browser-adapter.py /path/to/moviepilot/public
Re-run after a MoviePilot frontend upgrade. Does not restart MoviePilot.
"""

import argparse
import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path


def atomic_write(path, content):
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def install(public, script, asset_name="moviepilot-aggregate-browser.js"):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+\.js", asset_name):
        raise ValueError("Invalid static asset name")
    public = Path(public).resolve()
    index = public / "index.html"
    before = index.read_bytes()
    text = before.decode("utf-8")
    if not re.search(r"<head\b[^>]*>", text, re.I):
        raise ValueError("index.html does not contain a head element")
    code = Path(script).read_bytes()
    digest = hashlib.sha256(code).hexdigest()[:12]
    tag = f'<script src="/{asset_name}?v={digest}"></script>'
    pattern = (
        r"<script\b[^>]*\bsrc=[\"\']/"
        + re.escape(asset_name)
        + r"(?:\?[^\"\']*)?[\"\'][^>]*>\s*</script>"
    )
    text = re.sub(pattern, "", text, flags=re.I)
    after = re.sub(
        r"(<head\b[^>]*>)", lambda m: m[0] + tag, text, count=1, flags=re.I
    ).encode()
    if before != after:
        # Only the owned tag is updated. Keep a digest-addressed original for rollback.
        backup = (
            public
            / "adapter-backups"
            / ("index." + hashlib.sha256(before).hexdigest()[:12] + ".html")
        )
        backup.parent.mkdir(exist_ok=True)
        if not backup.exists():
            shutil.copy2(index, backup)
        atomic_write(public / asset_name, code)
        atomic_write(index, after)
    else:
        atomic_write(public / asset_name, code)
    return digest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("public")
    parser.add_argument(
        "--script",
        default=str(Path(__file__).with_name("moviepilot-browser-download.user.js")),
    )
    parser.add_argument("--asset-name", default="moviepilot-aggregate-browser.js")
    args = parser.parse_args()
    print(
        "Browser adapter installed:", install(args.public, args.script, args.asset_name)
    )
