"""Hardening: secrets never leave the process, cross-site requests and foreign
URLs are refused, responses carry security headers."""
import json
import logging
import threading

import pytest
import requests
from unittest.mock import MagicMock, patch

from conftest import CSRF

TOKEN = "SuperSecretPlexToken123"


def _failing_plex_request():
    try:
        requests.get(f"http://127.0.0.1:1/library/metadata/5/thumb?X-Plex-Token={TOKEN}", timeout=2)
    except requests.RequestException as e:
        return e
    raise AssertionError("expected a connection error")


def test_plex_token_not_in_error_records(make_app):
    wa = make_app(PLEX_URL="http://127.0.0.1:1", PLEX_TOKEN=TOKEN)
    record = wa._error_record("push", _failing_plex_request())
    assert TOKEN not in record["message"]
    assert "X-Plex-Token=[redacted]" in record["message"]


def test_plex_token_not_in_log_output_or_tracebacks(make_app):
    make_app(PLEX_URL="http://127.0.0.1:1", PLEX_TOKEN=TOKEN)
    import logsetup
    stream = __import__("io").StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logsetup._Formatter("%(message)s"))
    logger = logging.getLogger("wokearr.test")
    logger.addHandler(handler)
    try:
        raise _failing_plex_request()
    except requests.RequestException:
        logger.exception("push failed for %s", f"http://x/?X-Plex-Token={TOKEN}")
    logger.removeHandler(handler)
    out = stream.getvalue()
    assert "Traceback" in out and TOKEN not in out


def test_registered_secret_values_redacted_anywhere(make_app):
    make_app(AUTH_METHOD="forms", AUTH_USERNAME="admin", AUTH_PASSWORD="hunter2-long",
             NOTIFY_URL="gotifys://push.example.com/AppTokenXYZ123")
    import logsetup
    text = "pw hunter2-long and gotify AppTokenXYZ123 in a message"
    assert logsetup.redact(text) == "pw [redacted] and gotify [redacted] in a message"


def test_short_secrets_are_not_used_for_blanket_redaction(make_app):
    make_app(AUTH_METHOD="basic", AUTH_USERNAME="a", AUTH_PASSWORD="b")
    import logsetup
    assert logsetup.redact("a b c") == "a b c"


def test_stored_run_history_is_scrubbed_on_startup(make_app, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    leaked = [{"trigger": "push", "started_at": "2026-10-01T10:00:00+00:00", "error": True,
               "errors": [{"stage": "push", "kind": "connection", "target": "Plex", "status": None,
                           "message": f"ConnectionError: ... url: /thumb?X-Plex-Token={TOKEN} (Caused by ...)"}]}]
    (data / "run_history.json").write_text(json.dumps(leaked), encoding="utf-8")
    make_app(PLEX_URL="http://plex.local:32400", PLEX_TOKEN=TOKEN)
    stored = (data / "run_history.json").read_text(encoding="utf-8")
    assert TOKEN not in stored
    assert json.loads(stored)[0]["errors"][0]["kind"] == "connection"


def test_cross_site_post_refused_without_login(make_app):
    wa = make_app()
    c = wa.app.test_client()
    # /api/sync-library answers 400 in demo mode without touching the network:
    # 403 = refused by the CSRF check, 400 = got past it
    r = c.post("/api/sync-library", data="x", content_type="text/plain")
    assert r.status_code == 403
    assert c.post("/api/sync-library", headers=CSRF).status_code == 400


def test_sitemap_entries_on_other_hosts_are_never_fetched(make_app):
    make_app()
    import build_score_cache as bsc
    bsc.SLEEP_SECONDS = 0
    fetched = []

    def fake_get(url, **kw):
        fetched.append(url)
        return MagicMock(status_code=404, text="", is_redirect=False)

    sitemap = [("http://192.168.1.1/admin", None), ("https://169.254.169.254/latest/", None),
               ("https://isitwokeornot.com.evil.example/x", None), ("https://user:pw@isitwokeornot.com/x", None),
               ("https://isitwokeornot.com/movie/ok", None)]
    with patch.object(bsc, "get_title_urls", return_value=sitemap), patch.object(bsc.requests, "get", side_effect=fake_get):
        bsc.build_cache({}, skip_existing=False)
    assert fetched == ["https://isitwokeornot.com/movie/ok"]


def test_redirects_only_followed_on_the_same_site(make_app):
    make_app()
    import build_score_cache as bsc
    hops = {
        "https://isitwokeornot.com/movie/a": ("https://isitwokeornot.com/movie/a-2024", 301),
        "https://isitwokeornot.com/movie/a-2024": ("http://192.168.1.1/", 302),
    }
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        location, status = hops[url]
        return MagicMock(status_code=status, is_redirect=True, headers={"Location": location})

    with patch.object(bsc.requests, "get", side_effect=fake_get):
        result, status = bsc.extract_one("https://isitwokeornot.com/movie/a", None)
    assert seen == ["https://isitwokeornot.com/movie/a", "https://isitwokeornot.com/movie/a-2024"]
    assert result is None and status == "foreign_redirect"


@pytest.mark.parametrize("url", ["javascript:alert(1)", "http://isitwokeornot.com/x", "https://evil.example/x", None])
def test_review_links_only_to_isitwokeornot(make_app, url):
    wa = make_app()
    assert wa._with_utm(url) is None


def test_review_link_keeps_utm(make_app):
    wa = make_app()
    assert wa._with_utm("https://isitwokeornot.com/movie/x").startswith("https://isitwokeornot.com/movie/x?utm_source=wokearr")


def test_security_headers_and_csp_nonce(make_app):
    wa = make_app()
    r = wa.app.test_client().get("/")
    csp = r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "same-origin"
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">' in r.get_data(as_text=True)
    assert "object-src 'none'" in csp and "base-uri 'none'" in csp and "unsafe-eval" not in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]


def test_nonce_changes_per_request(make_app):
    wa = make_app()
    c = wa.app.test_client()
    a = c.get("/").headers["Content-Security-Policy"]
    b = c.get("/").headers["Content-Security-Policy"]
    assert a != b


def test_finished_jobs_are_pruned(make_app):
    wa = make_app()
    for i in range(wa.JOBS_KEEP_FINISHED + 20):
        wa.JOBS[f"old-{i}"] = {"state": "done"}
    wa.JOBS["busy"] = {"state": "running"}
    wa._start_job("cleanup", "cleanup", lambda job, log: {})
    assert "busy" in wa.JOBS
    assert sum(1 for v in wa.JOBS.values() if v.get("state") != "running") <= wa.JOBS_KEEP_FINISHED + 1


def test_invalid_poster_id_is_404(make_app):
    wa = make_app(PLEX_URL="http://plex.local:32400", PLEX_TOKEN=TOKEN)
    c = wa.app.test_client()
    with patch.object(wa, "get_plex", side_effect=AssertionError("must not reach Plex")):
        for bad in ("abc", "12a", "..", "1e5"):
            assert c.get(f"/api/poster/{bad}").status_code == 404, bad


def test_run_as_notice_only_when_entrypoint_fell_back_to_root(make_app):
    """The web UI only says something when there is something to do."""
    html = make_app().app.test_client().get("/").get_data(as_text=True)
    assert "system-notice" not in html

    module = make_app(WOKEARR_RUN_AS_NOTICE="chown_failed", PUID="1000", PGID="1000")
    html = module.app.test_client().get("/").get_data(as_text=True)
    assert "system-notice" in html and "1000:1000" in html and str(module.DATA_DIR) in html

    html = make_app(WOKEARR_RUN_AS_NOTICE="<script>").app.test_client().get("/").get_data(as_text=True)
    assert "system-notice" not in html


def test_run_as_notice_escapes_bad_ids(make_app):
    module = make_app(WOKEARR_RUN_AS_NOTICE="invalid_ids", PUID="<b>x</b>")
    html = module.app.test_client().get("/").get_data(as_text=True)
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "<b>x</b>" not in html


def _page(body: str, size: int | None = None):
    """A streamed requests.Response stand-in for the score sync."""
    raw = body.encode("utf-8")
    resp = MagicMock(status_code=200, is_redirect=False, encoding="utf-8")
    resp.iter_content = lambda chunk_size: (raw[i:i + chunk_size] for i in range(0, len(raw), chunk_size))
    return resp


REVIEW_PAGE = ('<script type="application/ld+json">{"@type":"Review","reviewRating":{"ratingValue":%s},'
               '"itemReviewed":{"name":%s}}</script> themoviedb.org/movie/1')


@pytest.mark.parametrize("raw, expected", [
    ("86", 86), ('"86"', 86), ("86.0", 86), ("0", 0), ("100", 100),
    ('"86<b>"', None), ("150", None), ("-1", None), ("86.5", None), ("true", None), ("null", None), ('""', None),
])
def test_scores_from_review_pages_are_validated(make_app, raw, expected):
    make_app()
    import build_score_cache as bsc
    bsc.SLEEP_SECONDS = 0
    with patch.object(bsc.requests, "get", return_value=_page(REVIEW_PAGE % (raw, '"X"'))):
        entry, status = bsc.extract_one("https://isitwokeornot.com/movie/x", None)
    if expected is None:
        assert entry is None and status == "bad_review"
    else:
        assert status == "ok" and entry["score"] == expected and type(entry["score"]) is int


def test_review_page_shapes_never_crash_the_sync(make_app):
    make_app()
    import build_score_cache as bsc
    bsc.SLEEP_SECONDS = 0
    pages = {
        "array": '<script type="application/ld+json">[{"@type":"Review"}]</script>',
        "scalar": '<script type="application/ld+json">42</script>',
        "no_rating": '<script type="application/ld+json">{"@type":"Review"}</script> themoviedb.org/movie/1',
        "item_not_object": (REVIEW_PAGE % ("5", "5")).replace('{"name":5}', "5"),
    }
    for name, html in pages.items():
        with patch.object(bsc.requests, "get", return_value=_page(html)):
            entry, status = bsc.extract_one("https://isitwokeornot.com/movie/x", None)
        assert status in ("no_review", "bad_review", "ok"), name
        if entry:
            assert entry["title"] is None


def test_titles_are_one_line_and_capped(make_app):
    make_app()
    import build_score_cache as bsc
    bsc.SLEEP_SECONDS = 0
    title = json.dumps("Evil\n2026-01-01 ERROR [auth] forged " + "x" * 500)
    with patch.object(bsc.requests, "get", return_value=_page(REVIEW_PAGE % ("1", title))):
        entry, _ = bsc.extract_one("https://isitwokeornot.com/movie/x", None)
    assert "\n" not in entry["title"] and len(entry["title"]) == bsc.MAX_TITLE_LENGTH


def test_oversized_review_page_is_a_request_error(make_app):
    make_app()
    import build_score_cache as bsc
    bsc.SLEEP_SECONDS = 0
    with patch.object(bsc, "MAX_PAGE_BYTES", 1000), \
            patch.object(bsc.requests, "get", return_value=_page("x" * 5000)):
        assert bsc.extract_one("https://isitwokeornot.com/movie/x", None) == (None, "request_error")


def test_log_lines_cannot_be_forged_with_line_breaks(make_app):
    make_app()
    import logsetup
    stream = __import__("io").StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logsetup._Formatter("%(levelname)s %(message)s"))
    logger = logging.getLogger("wokearr.test2")
    logger.addHandler(handler)
    logger.warning("Rendered: %s", "Title\nERROR [auth] Failed login for user 'x'")
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("with traceback")
    logger.removeHandler(handler)
    lines = stream.getvalue().splitlines()
    assert lines[0] == "WARNING Rendered: Title\\nERROR [auth] Failed login for user 'x'"
    assert lines[1] == "ERROR with traceback" and "Traceback" in lines[2]


def test_plex_token_travels_as_a_header_not_in_the_url(make_app, tmp_path):
    wa = make_app(PLEX_URL="http://plex.local:32400", PLEX_TOKEN=TOKEN)
    plex = MagicMock()
    plex.url = lambda key, includeToken=None: f"http://plex.local:32400{key}"
    plex.fetchItem.return_value = MagicMock(thumb="/library/metadata/7/thumb/1")
    calls = []

    def fake_get(url, **kw):
        calls.append((url, kw.get("headers") or {}))
        return _page("jpegdata")

    with patch.object(wa, "get_plex", return_value=plex), patch.object(wa.requests, "get", side_effect=fake_get):
        assert wa.app.test_client().get("/api/poster/7").status_code == 200
        wa._poster_candidate_bytes(plex, MagicMock(key="/library/metadata/7/thumb/2"))
        wa.fetch_original_poster_bytes(plex, MagicMock(ratingKey=8, thumb="/library/metadata/8/thumb/1",
                                                       posters=lambda: []))
    assert len(calls) == 3
    for url, headers in calls:
        assert TOKEN not in url and headers.get("X-Plex-Token") == TOKEN


def test_poster_candidates_on_private_addresses_are_ignored(make_app):
    wa = make_app(PLEX_URL="http://plex.local:32400", PLEX_TOKEN=TOKEN)
    resolved = {"image.tmdb.org": "151.101.1.1", "evil.example": "192.168.1.1", "meta.example": "169.254.169.254"}

    def fake_getaddrinfo(host, port, **kw):
        return [(None, None, None, None, (resolved.get(host, host), port))]

    fetched = []
    with patch.object(wa.socket, "getaddrinfo", side_effect=fake_getaddrinfo), \
            patch.object(wa.requests, "get", side_effect=lambda url, **kw: fetched.append(url) or _page("img")):
        for key in ("http://evil.example/p.jpg", "https://meta.example/latest", "http://127.0.0.1:32400/x",
                    "https://user:pw@image.tmdb.org/x", "ftp://image.tmdb.org/x"):
            assert wa._poster_candidate_bytes(MagicMock(), MagicMock(key=key)) is None, key
        assert wa._poster_candidate_bytes(MagicMock(), MagicMock(key="https://image.tmdb.org/t/p/w500/a.jpg")) == b"img"
    assert fetched == ["https://image.tmdb.org/t/p/w500/a.jpg"]


def test_healthz_says_nothing_about_the_installation(make_app):
    r = make_app(PLEX_URL="http://plex.local:32400", PLEX_TOKEN=TOKEN).app.test_client().get("/healthz")
    assert r.get_json() == {"status": "ok"}


def test_request_bodies_are_capped(make_app):
    wa = make_app(PLEX_URL="http://plex.local:32400", PLEX_TOKEN=TOKEN)
    r = wa.app.test_client().post("/api/apply", data="x" * (2 * 1024 * 1024), headers=CSRF,
                                  content_type="application/json")
    assert r.status_code == 413


def test_jobs_run_one_at_a_time_and_a_flood_is_refused(make_app):
    wa = make_app(PLEX_URL="http://plex.local:32400", PLEX_TOKEN=TOKEN)
    release = threading.Event()
    order = []

    def slow(job, log):
        order.append("first-start")
        release.wait(5)
        order.append("first-end")
        return {}

    def quick(job, log):
        order.append("second")
        return {}

    first = wa._start_job("cleanup", "cleanup", slow)
    second = wa._start_job("cleanup", "cleanup", quick)
    assert first and second
    for _ in range(wa.JOBS_MAX_WAITING):
        wa._start_job("cleanup", "cleanup", quick)
    assert wa._start_job("cleanup", "cleanup", quick) is None, "queue is full"
    c = wa.app.test_client()
    assert c.post("/api/sync-library", headers=CSRF).status_code == 429
    release.set()
    for _ in range(100):
        if wa.JOBS[second]["state"] == "done" and wa._jobs_waiting == 0:
            break
        threading.Event().wait(0.05)
    assert order[:3] == ["first-start", "first-end", "second"]
    assert wa._jobs_waiting == 0


def test_orphan_cleanup_waits_when_a_library_could_not_be_read(make_app):
    wa = make_app(PLEX_URL="http://plex.local:32400", PLEX_TOKEN=TOKEN, LIBRARY_SECTIONS="Filme,Serien")
    (wa.ORIGINALS_DIR / "999.jpg").write_bytes(b"old")
    movie = MagicMock(ratingKey=1, title="A", guids=[MagicMock(id="tmdb://1")])
    movies = MagicMock(type="movie"); movies.all.return_value = [movie]

    def section(name):
        if name == "Serien":
            raise RuntimeError("renamed")
        return movies

    plex = MagicMock()
    plex.library.section.side_effect = section
    with patch.object(wa, "get_plex", return_value=plex):
        stats = wa.sync_library()
    assert stats["orphans_removed"] == 0 and (wa.ORIGINALS_DIR / "999.jpg").exists()
    plex.library.section.side_effect = lambda name: movies
    with patch.object(wa, "get_plex", return_value=plex):
        stats = wa.sync_library()
    assert stats["orphans_removed"] == 1 and not (wa.ORIGINALS_DIR / "999.jpg").exists()
