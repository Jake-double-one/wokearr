"""Hardening: secrets never leave the process, cross-site requests and foreign
URLs are refused, responses carry security headers."""
import json
import logging

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
