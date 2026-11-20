"""Score sync: lastmod-based skipping, per-URL state, retry, score changes."""
import datetime
import json
import time
from unittest.mock import MagicMock, patch

import pytest
import requests


@pytest.fixture
def bsc(make_app):
    make_app()
    import build_score_cache as module
    module.SLEEP_SECONDS = 0
    module.SITEMAP_RETRY_PAUSE = 0
    return module


def _result(url, lastmod, key, title, score):
    return {"key": key, "title": title, "score": score, "imdb_id": None, "slug": url.rsplit("/", 1)[-1],
            "media_type": "movie", "url": url, "lastmod": lastmod}, "ok"


def test_unresolvable_pages_are_not_refetched_every_run(bsc):
    sitemap = [("https://isitwokeornot.com/movie/good", "1"), ("https://isitwokeornot.com/movie/broken", "1")]
    calls = []

    def extract(url, lastmod):
        calls.append(url)
        return _result(url, lastmod, "movie:1", "Good", 42) if url.endswith("good") else (None, "no_review")

    with patch.object(bsc, "get_title_urls", return_value=sitemap), patch.object(bsc, "extract_one", side_effect=extract):
        cache, done = bsc.build_cache({})
        assert done == 2
        calls.clear()
        cache, done = bsc.build_cache(cache)
        assert done == 0 and calls == []
        state = json.loads(bsc.URL_STATE_FILE.read_text())
        for rec in state.values():
            rec["checked"] = time.time() - (bsc.FAILED_RETRY_DAYS + 1) * 86400
        bsc.URL_STATE_FILE.write_text(json.dumps(state))
        bsc.build_cache(cache)
        assert calls == ["https://isitwokeornot.com/movie/broken"]


def _iso(seconds_ago):
    moment = datetime.datetime.fromtimestamp(time.time() - seconds_ago, datetime.timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _run(bsc, sitemap, outcome, cache=None):
    """One build_cache run; returns the URLs that were actually fetched."""
    calls = []

    def extract(url, lastmod):
        calls.append(url)
        return outcome(url, lastmod)

    with patch.object(bsc, "get_title_urls", return_value=sitemap), patch.object(bsc, "extract_one", side_effect=extract):
        bsc.build_cache({} if cache is None else cache)
    return calls


def _age_last_check(bsc, seconds):
    """Pretends the last run was that long ago."""
    state = json.loads(bsc.URL_STATE_FILE.read_text())
    for rec in state.values():
        rec["checked"] -= seconds
        if "failed_since" in rec:
            rec["failed_since"] -= seconds
    bsc.URL_STATE_FILE.write_text(json.dumps(state))


def test_page_that_was_incomplete_when_first_fetched_is_picked_up_soon(bsc):
    """A review page fetched minutes after it was published, before its TMDb
    link was there: the next attempt comes after an hour, not after a week."""
    url = "https://isitwokeornot.com/movie/example-movie"
    sitemap = [(url, _iso(120))]
    cache = {}
    assert _run(bsc, sitemap, lambda u, lm: (None, "no_tmdb_id"), cache) == [url]
    assert _run(bsc, sitemap, lambda u, lm: (None, "no_tmdb_id"), cache) == []   # same hour: left alone
    _age_last_check(bsc, 3700)
    assert _run(bsc, sitemap, lambda u, lm: _result(u, lm, "movie:1", "Example Movie", 80), cache) == [url]
    assert cache["movie:1"]["score"] == 80
    assert "failed_since" not in json.loads(bsc.URL_STATE_FILE.read_text())[url]


def test_pause_doubles_up_to_a_week(bsc):
    url = "https://isitwokeornot.com/movie/example-movie"
    sitemap = [(url, _iso(60))]
    cache = {}
    fail = lambda u, lm: (None, "no_review")  # noqa: E731
    assert _run(bsc, sitemap, fail, cache) == [url]
    waited = []
    for _ in range(12):
        hours = 0
        while not _run(bsc, sitemap, fail, cache):
            _age_last_check(bsc, 3600)
            hours += 1
            assert hours <= bsc.FAILED_RETRY_DAYS * 24
        waited.append(hours)
    assert waited[0] == 1                                   # the first retry comes after an hour
    assert waited == sorted(waited)                          # then ever more rarely
    assert all(later <= 2 * earlier + 1 for earlier, later in zip(waited, waited[1:]))  # at most doubling
    assert waited[-1] == bsc.FAILED_RETRY_DAYS * 24          # and never rarer than weekly


def test_record_from_an_older_version_heals_without_waiting_a_week(bsc):
    """State written before failed_since existed: failed two minutes after
    the page changed, last checked two days ago - due on the next run."""
    url = "https://isitwokeornot.com/movie/example-movie"
    old_page = "https://isitwokeornot.com/movie/example-old"
    now = time.time()
    bsc.URL_STATE_FILE.write_text(json.dumps({
        url: {"lastmod": _iso(2 * 86400 + 120), "status": "no_tmdb_id", "checked": now - 2 * 86400},
        # failing for months: stays on the weekly rhythm
        old_page: {"lastmod": _iso(90 * 86400), "status": "no_review", "checked": now - 2 * 86400},
    }))
    state = json.loads(bsc.URL_STATE_FILE.read_text())
    sitemap = [(u, state[u]["lastmod"]) for u in (url, old_page)]
    assert _run(bsc, sitemap, lambda u, lm: _result(u, lm, "movie:1", "Example Movie", 80)) == [url]


def test_transient_failure_is_retried_on_the_next_run(bsc):
    """The site not answering during one run must not park old pages for a week."""
    url = "https://isitwokeornot.com/movie/example-movie"
    sitemap = [(url, _iso(200 * 86400))]
    cache = {}
    assert _run(bsc, sitemap, lambda u, lm: (None, "request_error"), cache) == [url]
    _age_last_check(bsc, 3700)
    assert _run(bsc, sitemap, lambda u, lm: _result(u, lm, "movie:1", "Example Movie", 80), cache) == [url]


def test_unreadable_lastmod_keeps_the_weekly_rhythm_for_old_records(bsc):
    url = "https://isitwokeornot.com/movie/example-movie"
    bsc.URL_STATE_FILE.write_text(json.dumps({url: {"lastmod": "1", "status": "no_review", "checked": time.time() - 86400}}))
    assert _run(bsc, [(url, "1")], lambda u, lm: (None, "no_review")) == []


def test_score_changes_reported(bsc):
    cache = {"movie:1": {"title": "Example Movie", "score": 72, "url": "https://isitwokeornot.com/movie/example-movie", "lastmod": "a"}}
    changes = []
    with patch.object(bsc, "get_title_urls", return_value=[("https://isitwokeornot.com/movie/example-movie", "b")]), \
         patch.object(bsc, "extract_one", side_effect=lambda u, lm: _result(u, lm, "movie:1", "Example Movie", 81)):
        bsc.build_cache(cache, changes=changes)
    assert changes == [{"key": "movie:1", "title": "Example Movie", "old": 72, "new": 81}]


def test_sitemap_retry_then_error(bsc):
    body = b"<url><loc>https://isitwokeornot.com/movie/a</loc></url>"
    good = MagicMock(status_code=200, encoding="utf-8", iter_content=lambda chunk_size: iter([body]))
    good.raise_for_status = lambda: None
    attempts = []

    def flaky(*a, **k):
        attempts.append(1)
        if len(attempts) == 1:
            raise requests.ReadTimeout("slow")
        return good

    with patch.object(bsc.requests, "get", side_effect=flaky):
        assert bsc.get_title_urls() == [("https://isitwokeornot.com/movie/a", None)]
    with patch.object(bsc.requests, "get", side_effect=requests.ConnectionError("down")):
        with pytest.raises(bsc.SitemapError):
            bsc.get_title_urls()


def test_failed_sitemap_releases_cooldown(make_app):
    wa = make_app()
    import build_score_cache as module
    wa.CACHE_REBUILD_COOLDOWN_SECONDS = 300
    with patch.object(module, "get_title_urls", side_effect=module.SitemapError("down")):
        with pytest.raises(module.SitemapError):
            wa.score_sync()
    assert wa._reserve_rebuild_slot() == 0.0
