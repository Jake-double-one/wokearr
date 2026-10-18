"""Score sync: lastmod-based skipping, per-URL state, retry, score changes."""
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


def test_score_changes_reported(bsc):
    cache = {"movie:1": {"title": "Barbie", "score": 72, "url": "https://isitwokeornot.com/movie/barbie", "lastmod": "a"}}
    changes = []
    with patch.object(bsc, "get_title_urls", return_value=[("https://isitwokeornot.com/movie/barbie", "b")]), \
         patch.object(bsc, "extract_one", side_effect=lambda u, lm: _result(u, lm, "movie:1", "Barbie", 81)):
        bsc.build_cache(cache, changes=changes)
    assert changes == [{"key": "movie:1", "title": "Barbie", "old": 72, "new": 81}]


def test_sitemap_retry_then_error(bsc):
    good = MagicMock(status_code=200, text="<url><loc>https://isitwokeornot.com/movie/a</loc></url>", content=b"x")
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
