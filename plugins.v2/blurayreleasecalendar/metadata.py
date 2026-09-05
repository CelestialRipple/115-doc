"""Conservative title/year matching via MoviePilot's configured TMDB client."""

import re
import unicodedata
from difflib import SequenceMatcher


def normalize(value):
    return re.sub(r"[^\w]", "", unicodedata.normalize("NFKC", value).casefold())


def query_title(item):
    return re.sub(r"\s+4K\s*$", "", item["title"], flags=re.I).strip()


def select_match(item, candidates):
    title = query_title(item)
    # Anthologies/collections and season sets need explicit identity; don't map
    # a box containing many films onto the first popular single movie.
    if re.search(
        r"\b(collection|trilogy|anthology|complete series|season|box set)\b",
        title,
        re.I,
    ) or item.get("year_end") not in ("", None, item.get("year")):
        return None
    scored = []
    for candidate in candidates:
        if not candidate.get("id"):
            continue
        year = str(candidate.get("release_date") or "")[:4]
        if item.get("year") and (
            not year.isdigit() or abs(int(year) - int(item["year"])) > 1
        ):
            continue
        score = max(
            SequenceMatcher(
                None, normalize(title), normalize(str(candidate.get(field) or ""))
            ).ratio()
            for field in ("title", "original_title")
        )
        if score >= 0.92:
            scored.append((score, candidate))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored or (len(scored) > 1 and scored[0][0] - scored[1][0] < 0.04):
        return None
    return scored[0][1]


def match_metadata(item):
    from app.modules.themoviedb.tmdbv3api import Search, Movie

    title = query_title(item)
    candidates = Search(language="en-US").movies(
        term=title, year=item.get("year") or None
    )
    if candidates is None:
        raise RuntimeError("TMDB search unavailable")
    match = select_match(item, candidates)
    if not match:
        return {"state": "unmatched", "message": "未可靠匹配，保留原始发行名称"}
    detail = Movie(language="zh-CN").details(int(match["id"]), append_to_response="")
    if not detail:
        raise RuntimeError("TMDB details unavailable")
    poster = str(detail.get("poster_path") or "")
    return {
        "state": "matched",
        "tmdb_id": int(match["id"]),
        "title": str(detail.get("title") or match.get("title") or title),
        "original_title": str(
            detail.get("original_title") or match.get("original_title") or ""
        ),
        "year": str(detail.get("release_date") or "")[:4],
        "overview": str(detail.get("overview") or "")[:1600],
        "rating": detail.get("vote_average"),
        "poster": "https://image.tmdb.org/t/p/w342" + poster
        if re.fullmatch(r"/[\w.-]+", poster)
        else "",
        "tmdb_url": "https://www.themoviedb.org/movie/" + str(match["id"]),
    }
