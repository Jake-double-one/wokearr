"""The button on a tile: score fetched again, badge rendered, pushed - for
that title only (see app.refresh_titles)."""
import json
from unittest.mock import MagicMock, patch

from conftest import CSRF

TOKEN = "SuperSecretPlexToken123"


def _library(wa, movie):
    section = MagicMock(type="movie")
    section.all.return_value = [movie]
    plex = MagicMock()
    plex.library.section.return_value = section
    return plex


def test_refresh_fetches_one_review_page_and_pushes_that_title(make_app):
    wa = make_app(PLEX_URL="http://plex.local:32400", PLEX_TOKEN=TOKEN, LIBRARY_SECTIONS="Filme")
    wa.CACHE_FILE.write_text(json.dumps({"movie:1": {"title": "A", "score": 10, "url": "https://isitwokeornot.com/movie/a",
                                                     "lastmod": "2026-01-01"}}), encoding="utf-8")
    (wa.ORIGINALS_DIR / "7.jpg").write_bytes(b"jpg")
    movie = MagicMock(ratingKey=7, title="A", guids=[MagicMock(id="tmdb://1")], parentTitle=None)
    fetched, pushed = [], {}

    def fake_extract(url, lastmod):
        fetched.append((url, lastmod))
        return {"key": "movie:1", "title": "A", "score": 55, "imdb_id": None, "slug": "a", "media_type": "movie",
                "url": url, "lastmod": lastmod}, "ok"

    def fake_push(log=None, progress=None, force=False, rating_keys=None):
        pushed.update(force=force, rating_keys=rating_keys)
        return {"pushed": 1, "push_failed": 0}

    with patch.object(wa, "get_plex", return_value=_library(wa, movie)), \
            patch.object(wa.bsc, "extract_one", side_effect=fake_extract), \
            patch.object(wa, "push_pending_to_plex", side_effect=fake_push):
        stats = wa.refresh_titles(rating_keys=["7", "999"])
    assert fetched == [("https://isitwokeornot.com/movie/a", "2026-01-01")], "just this title's page"
    assert pushed == {"force": True, "rating_keys": ["7"]}
    assert wa.load_cache()["movie:1"]["score"] == 55
    assert stats["scores_updated"] == 1 and stats["pushed"] == 1


def test_refresh_keeps_the_stored_score_when_the_page_fails(make_app):
    wa = make_app(PLEX_URL="http://plex.local:32400", PLEX_TOKEN=TOKEN, LIBRARY_SECTIONS="Filme")
    wa.CACHE_FILE.write_text(json.dumps({"movie:1": {"title": "A", "score": 10, "url": "https://isitwokeornot.com/movie/a"}}),
                             encoding="utf-8")
    (wa.ORIGINALS_DIR / "7.jpg").write_bytes(b"jpg")
    movie = MagicMock(ratingKey=7, title="A", guids=[MagicMock(id="tmdb://1")], parentTitle=None)
    messages = []
    with patch.object(wa, "get_plex", return_value=_library(wa, movie)), \
            patch.object(wa.bsc, "extract_one", return_value=(None, "http_503")), \
            patch.object(wa, "push_pending_to_plex", return_value={"pushed": 1, "push_failed": 0}):
        stats = wa.refresh_titles(log=messages.append, rating_keys=["7"])
    assert wa.load_cache()["movie:1"]["score"] == 10
    assert stats["scores_updated"] == 0 and stats["pushed"] == 1, "still pushed with the stored score"
    assert any("http_503" in m for m in messages)


def test_refresh_endpoint(make_app):
    wa = make_app()
    c = wa.app.test_client()
    assert c.post("/api/refresh", headers=CSRF, json={"ratingKeys": ["7"]}).status_code == 400, "demo mode"
    wa = make_app(PLEX_URL="http://plex.local:32400", PLEX_TOKEN=TOKEN)
    c = wa.app.test_client()
    assert c.post("/api/refresh", json={"ratingKeys": ["7"]}).status_code == 403, "needs the CSRF header"
    with patch.object(wa, "refresh_titles", return_value={"pushed": 0, "push_failed": 0}) as refresh:
        r = c.post("/api/refresh", headers=CSRF, json={"ratingKeys": ["7"]})
        assert r.status_code == 200 and "job_id" in r.get_json()
        for _ in range(100):
            if wa.JOBS[r.get_json()["job_id"]]["state"] != "running":
                break
            __import__("time").sleep(0.02)
    assert refresh.call_args.kwargs["rating_keys"] == ["7"]


def _jpeg(marker: bool) -> bytes:
    """A real tiny JPEG - with Wokearr's badge marker, or clean."""
    import io
    from PIL import Image
    buf = io.BytesIO()
    kwargs = {"comment": b"wokearr-badge"} if marker else {}
    Image.new("RGB", (8, 12), (10, 20, 30)).save(buf, format="JPEG", **kwargs)
    return buf.getvalue()


def test_a_title_without_agent_poster_uses_its_current_poster(make_app):
    """Plex has no agent artwork for the title (a season, typically): the
    current poster becomes the base - if it isn't one of our badges."""
    wa = make_app(PLEX_URL="http://plex.local:32400", PLEX_TOKEN=TOKEN)
    season = MagicMock(ratingKey=42, title="Season 4", parentTitle="Lupin", thumb="/library/metadata/42/thumb/1",
                       posters=lambda: [])
    with patch.object(wa, "_plex_get", return_value=_jpeg(marker=False)):
        data, found = wa.fetch_original_poster_bytes(MagicMock(), season)
    assert data and found is False
    assert (wa.ORIGINALS_DIR / "42.jpg").exists(), "cached, so it can be rendered and pushed"


def test_a_title_whose_only_poster_is_our_badge_is_skipped_without_failing_the_run(make_app):
    wa = make_app(PLEX_URL="http://plex.local:32400", PLEX_TOKEN=TOKEN)
    season = MagicMock(ratingKey=43, title="Season 5", parentTitle="Lupin", thumb="/library/metadata/43/thumb/1",
                       posters=lambda: [])
    with patch.object(wa, "_plex_get", return_value=_jpeg(marker=True)):
        assert wa.fetch_original_poster_bytes(MagicMock(), season) == (None, False)
    assert not (wa.ORIGINALS_DIR / "43.jpg").exists()

    # push: a warning, not an error - the run stays green
    messages = []
    with patch.object(wa, "_plex_get", return_value=_jpeg(marker=True)):
        try:
            wa.push_to_plex(MagicMock(), season, {"score": 50})
        except wa.NoOriginalPoster:
            pass
        else:
            raise AssertionError("expected NoOriginalPoster")
    show = MagicMock(ratingKey=7, title="Lupin", guids=[MagicMock(id="tmdb://1")], parentTitle=None,
                     seasons=lambda: [season])
    section = MagicMock(type="show")
    section.all.return_value = [show]
    plex = MagicMock()
    plex.library.section.return_value = section
    wa.CACHE_FILE.write_text(json.dumps({"tv:1": {"title": "Lupin", "score": 50, "url": "https://isitwokeornot.com/tv/l"}}),
                             encoding="utf-8")
    (wa.ORIGINALS_DIR / "7.jpg").write_bytes(_jpeg(marker=False))
    with patch.object(wa, "get_plex", return_value=plex), patch.object(wa, "_plex_get", return_value=_jpeg(marker=True)), \
            patch.object(wa, "add_badge", side_effect=lambda src, score, out, **kw: open(out, "wb").write(b"x")):
        stats = wa.push_pending_to_plex(log=messages.append, rating_keys=["7"])
    assert stats["pushed"] == 1 and stats["push_failed"] == 0 and stats["push_error"] is None
    assert any("Season 5" in m and ("bersprungen" in m or "Skipped" in m) for m in messages)
