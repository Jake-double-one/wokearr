"""The "Update all" button: the autopilot's run by hand (see app.api_run)."""
import time
from unittest.mock import patch

from conftest import CSRF

TOKEN = "SuperSecretPlexToken123"


def _wait(wa, job_id):
    for _ in range(200):
        if wa.JOBS[job_id]["state"] != "running":
            return wa.JOBS[job_id]
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_run_all_runs_the_three_stages_and_pushes_only_changes(make_app):
    wa = make_app(PLEX_URL="http://plex.local:32400", PLEX_TOKEN=TOKEN)
    calls = []
    with patch.object(wa, "score_sync", side_effect=lambda **kw: calls.append("score_sync") or {"scores_updated": 2}), \
            patch.object(wa, "sync_library", side_effect=lambda **kw: calls.append("sync") or {"matched": 3, "missing_originals": ["X"]}), \
            patch.object(wa, "push_pending_to_plex", side_effect=lambda **kw: calls.append(("push", kw.get("force"))) or {"pushed": 1, "push_failed": 0}):
        r = wa.app.test_client().post("/api/run", headers=CSRF)
        assert r.status_code == 200
        job = _wait(wa, r.get_json()["job_id"])
    assert calls == ["score_sync", "sync", ("push", None)], "not forced - only new or changed titles"
    assert job["state"] == "done" and job["progress"] == [3, 3] and job["missing_originals"] == ["X"]
    run = wa.load_runs()[-1]
    assert run["trigger"] == "manual" and run["pushed"] == 1 and "error" not in run


def test_run_all_carries_on_after_a_failed_score_sync_and_reports_it(make_app):
    wa = make_app(PLEX_URL="http://plex.local:32400", PLEX_TOKEN=TOKEN)
    with patch.object(wa, "score_sync", side_effect=RuntimeError("sitemap down")), \
            patch.object(wa, "sync_library", return_value={"matched": 3}), \
            patch.object(wa, "push_pending_to_plex", return_value={"pushed": 0, "push_failed": 0}) as push:
        r = wa.app.test_client().post("/api/run", headers=CSRF)
        job = _wait(wa, r.get_json()["job_id"])
    assert push.called, "Plex stages still run with the stored scores"
    assert job["state"] == "error" and job["error"]
    run = wa.load_runs()[-1]
    assert run["error"] is True and run["errors"][0]["stage"] == "score_sync"


def test_run_all_demo_and_csrf(make_app):
    c = make_app().app.test_client()
    assert c.post("/api/run", headers=CSRF).status_code == 400
    c = make_app(PLEX_URL="http://plex.local:32400", PLEX_TOKEN=TOKEN).app.test_client()
    assert c.post("/api/run").status_code == 403


def test_top_bar_has_one_primary_button_and_a_menu(make_app):
    html = make_app().app.test_client().get("/").get_data(as_text=True)
    assert 'id="btn-run-all"' in html and 'class="btn btn-primary"' in html
    assert html.count("btn-primary") == 1, "only one primary action"
    for item in ("btn-rebuild", "btn-rebuild-full", "btn-sync-library", "btn-apply-all", "btn-cleanup-posters"):
        assert f'id="{item}" class="menu-item"' in html, item
    assert html.count("data-needs-plex") == 4
