from datetime import date
import pytest
from blurayreleasecalendar.calendar import parse_calendar
from blurayreleasecalendar.metadata import select_match

# Minimal public calendar record fixture, including JS apostrophe escaping.
FIXTURE = """movies[0] = {id: 410867, casing: 'SteelBook', title: '28 Days Later 4K', edition: 'Collector\\'s Edition', title_keywords: '28-Days-Later-4K', studio: 'Sony Pictures', year: '2002', yearend: '2002', releasedate: 'September 01, 2026'};
movies[1] = {id: 11, title: 'Other', year: '2020', releasedate: 'October 01, 2026'};
window.evil = 'never execute';
"""


def test_real_calendar_js_literals_are_parsed_without_execution():
    rows = parse_calendar(FIXTURE, "2026-09")
    assert len(rows) == 1
    assert rows[0]["edition"] == "Collector's Edition"
    assert rows[0]["format"] == "4K UHD"
    assert rows[0]["release_date"] == "2026-09-01" and rows[0]["year"] == "2002"
    assert rows[0]["source_url"].endswith("/410867/")
    with pytest.raises(ValueError):
        parse_calendar("<title>Just a moment...</title>", "2026-09")


def test_metadata_matching_rejects_remakes_ambiguity_and_collections():
    item = {"title": "The Thing 4K", "year": "1982", "year_end": "1982"}
    good = {"id": 1, "title": "The Thing", "release_date": "1982-06-25"}
    remake = {"id": 2, "title": "The Thing", "release_date": "2011-01-01"}
    assert select_match(item, [remake, good]) is good
    assert select_match(item, [remake]) is None
    assert select_match(item, [good, {**good, "id": 3}]) is None
    assert select_match({**item, "title": "The Thing Collection"}, [good]) is None


def test_source_outage_preserves_last_successful_calendar(plugin, monkeypatch):
    engine = plugin._engine
    month = date.today().strftime("%Y-%m")
    engine.cache.set("month:" + month, [{"id": "1"}])
    with engine.cache.connect() as db:
        db.execute("UPDATE cache SET updated=updated-22000")
    monkeypatch.setattr(
        "blurayreleasecalendar.engine.fetch_calendar",
        lambda *a: (_ for _ in ()).throw(RuntimeError("secret URL")),
    )
    rows, updated, warning = engine.releases(month)
    assert (
        rows == [{"id": "1"}]
        and updated > 0
        and "缓存" in warning
        and "secret" not in warning
    )


def test_read_only_authenticated_routes_and_validation(plugin, client):
    base = "/api/v1/plugin/BlurayReleaseCalendar"
    routes = plugin.get_api()
    assert all(r.get("auth") == "bear" for r in routes if r["path"] != "/ui")
    assert plugin.get_module() == {} and plugin.get_service() == []
    assert client.get(base + "/ui").status_code == 200
    assert client.get(base + "/releases?month=2026-99").status_code == 400
    assert (
        client.post(
            base + "/match",
            json={
                "month": date.today().strftime("%Y-%m"),
                "ids": ["https://evil.example"],
            },
        ).status_code
        == 400
    )
    assert client.post(base + "/match", content=b"x" * 4097).status_code == 413
    plugin._config["enabled"] = False
    assert client.get(base + "/releases").status_code == 409


def test_filter_and_metadata_cache_use_film_identity(plugin, client):
    engine = plugin._engine
    month = date.today().strftime("%Y-%m")
    rows = [
        {
            "id": "1",
            "title": "Movie 4K",
            "year": "2000",
            "year_end": "2000",
            "release_date": month + "-01",
            "format": "4K UHD",
        },
        {
            "id": "2",
            "title": "Movie",
            "year": "2000",
            "year_end": "2000",
            "release_date": month + "-02",
            "format": "Blu-ray",
        },
    ]
    engine.cache.set("month:" + month, rows)
    engine.cache.set(engine.meta_key(rows[0]), {"state": "matched", "title": "电影"})
    r = client.get("/api/v1/plugin/BlurayReleaseCalendar/releases?format=bluray").json()
    assert r["total"] == 1 and r["items"][0]["metadata"]["title"] == "电影"


def test_tmdb_empty_response_is_retryable_not_cached_as_unmatched(plugin, monkeypatch):
    import sys
    from types import ModuleType
    from blurayreleasecalendar.metadata import match_metadata

    sdk = ModuleType("app.modules.themoviedb.tmdbv3api")

    class Search:
        def __init__(self, **kwargs):
            pass

        def movies(self, **kwargs):
            return None

    sdk.Search = Search
    sdk.Movie = Search
    monkeypatch.setitem(sys.modules, sdk.__name__, sdk)
    item = {"id": "1", "title": "Movie", "year": "2000"}
    with pytest.raises(RuntimeError):
        match_metadata(item)
    assert plugin._engine.match_one(item)["state"] == "error"
    assert plugin._engine.metadata(item)["state"] == "pending"
