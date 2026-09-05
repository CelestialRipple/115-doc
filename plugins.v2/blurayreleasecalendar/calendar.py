"""Read the public Blu-ray.com calendar without executing its JavaScript."""

import html
import re
from datetime import date
from urllib.parse import quote

import requests

SOURCE = "https://www.blu-ray.com/movies/releasedates.php"
MONTHS = {
    name: i
    for i, name in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        1,
    )
}
FIELDS = re.compile(r"(\w+)\s*:\s*('(?:\\.|[^'\\])*'|\d+)(?:\s*,|\s*$)")


def js_text(value):
    if not value.startswith("'"):
        return value

    def unescape(match):
        code = match[1]
        if code.startswith(("u", "x")) and len(code) > 1:
            return chr(int(code[1:], 16))
        return {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(code, code)

    return html.unescape(
        re.sub(r"\\(u[\da-fA-F]{4}|x[\da-fA-F]{2}|.)", unescape, value[1:-1])
    )


def parse_calendar(text, month):
    records = re.findall(r"^movies\[\d+\]\s*=\s*\{(.*)\};\s*$", text, re.M)
    if not records:
        # Layout changes/challenges must not look like a genuinely empty calendar.
        if "var movies = new Array()" in text and "function init()" in text:
            return []
        raise ValueError("发行日历未返回可识别的数据，请稍后重试")
    items = {}
    for record in records[:2500]:
        row = {key: js_text(value) for key, value in FIELDS.findall(record)}
        try:
            match = re.fullmatch(
                r"(\w+) (\d{1,2}), (\d{4})", row.get("releasedate", "")
            )
            release = date(int(match[3]), MONTHS[match[1]], int(match[2])).isoformat()
            rid = str(int(row["id"]))
        except (TypeError, KeyError, ValueError):
            continue
        if not release.startswith(month) or not row.get("title"):
            continue
        title = row["title"][:300]
        is_4k = bool(re.search(r"\b4K\b", title, re.I))
        slug = quote(row.get("title_keywords", ""), safe="-")
        items[rid] = {
            "id": rid,
            "title": title,
            "year": row.get("year", "")[:4],
            "year_end": row.get("yearend", "")[:4],
            "release_date": release,
            "format": "4K UHD" if is_4k else "Blu-ray",
            "region": "美国",
            "edition": row.get("edition", "")[:500],
            "casing": row.get("casing", "")[:100],
            "studio": row.get("studio", "")[:150],
            "source_url": f"https://www.blu-ray.com/movies/{slug}-Blu-ray/{rid}/",
        }
    return sorted(
        items.values(), key=lambda x: (x["release_date"], x["title"], x["id"])
    )


def fetch_calendar(month, proxy=""):
    year, number = month.split("-")
    with requests.Session() as session:
        session.trust_env = False
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        with session.get(
            SOURCE,
            params={"year": year, "month": int(number)},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
            timeout=(8, 25),
            stream=True,
            allow_redirects=False,
        ) as response:
            if response.status_code != 200:
                raise ValueError(
                    f"发行来源返回 HTTP {response.status_code}，可检查代理或稍后重试"
                )
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > 4 * 1024 * 1024:
                    raise ValueError("发行来源响应过大")
            encoding = "iso-8859-1" if b"charset=ISO-8859-1" in data[:4096] else "utf-8"
            return parse_calendar(data.decode(encoding, errors="replace"), month)
