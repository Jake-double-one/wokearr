#!/usr/bin/env python3
"""
Wokearr - small local web UI (Radarr/Sonarr style) for traffic-light badges
on Plex posters, score source: isitwokeornot.com

Configuration is done entirely via environment variables (see .env.example),
so the image can be published to GitHub/Docker Hub without any code changes.
"""
import io
import json
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests
from flask import Flask, jsonify, request, send_file, render_template
from PIL import Image

from badge import add_badge, BADGE_MARKER  # noqa: E402

# ---------------------------------------------------------------------------
# CONFIG - comes from environment variables (see .env.example / docker-compose.yaml)
# ---------------------------------------------------------------------------
PLEX_URL = os.environ.get("PLEX_URL", "").rstrip("/")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
LIBRARY_SECTIONS = [s.strip() for s in os.environ.get("LIBRARY_SECTIONS", "Filme,Serien").split(",") if s.strip()]
BADGE_POSITION = os.environ.get("BADGE_POSITION", "top-right")
BADGE_LABEL_STYLE = os.environ.get("BADGE_LABEL_STYLE", "percent")
if BADGE_LABEL_STYLE not in ("percent", "woke"):
    BADGE_LABEL_STYLE = "percent"

# Badge width relative to poster width, in percent. Hard-floored at 20%,
# otherwise the badge quickly becomes unreadable on smaller posters.
BADGE_WIDTH_MIN_PERCENT = 20.0
try:
    BADGE_WIDTH_PERCENT = float(os.environ.get("BADGE_WIDTH_PERCENT", "20"))
except ValueError:
    BADGE_WIDTH_PERCENT = BADGE_WIDTH_MIN_PERCENT
BADGE_WIDTH_PERCENT = max(BADGE_WIDTH_PERCENT, BADGE_WIDTH_MIN_PERCENT)

# UI language (template, JS toasts, job logs/error messages in the browser).
# en-US is the default AND the fallback for individual missing keys in other
# languages (e.g. an incomplete third language contributed later).
DEFAULT_LANGUAGE = "en-US"
LOCALES_DIR = Path(__file__).parent / "locales"
LANGUAGE = os.environ.get("LANGUAGE", DEFAULT_LANGUAGE)


def _load_locale(lang: str) -> dict:
    path = LOCALES_DIR / f"{lang}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


_FALLBACK_TRANSLATIONS = _load_locale(DEFAULT_LANGUAGE)
if LANGUAGE != DEFAULT_LANGUAGE and not (LOCALES_DIR / f"{LANGUAGE}.json").exists():
    print(f"[i18n] No locale file found for LANGUAGE={LANGUAGE!r}, falling back to {DEFAULT_LANGUAGE}.",
          flush=True)
    # Fall LANGUAGE itself back too (not just individual keys) - otherwise
    # e.g. <html lang="fr"> would show on text that's actually all English.
    LANGUAGE = DEFAULT_LANGUAGE
# Per key: active language, else en-US - so every key is guaranteed to
# exist, without frontend/backend having to rebuild their own fallback logic.
TRANSLATIONS = {**_FALLBACK_TRANSLATIONS, **_load_locale(LANGUAGE)}


def t(key: str, **kwargs) -> str:
    template = TRANSLATIONS.get(key, key)
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        # A broken/inconsistent translation template must never crash a
        # request - show it unformatted rather than fail.
        return template

# Plex keeps the previous version of every uploaded poster as poster history
# and never deletes it on its own - otherwise the Plex server just keeps
# growing. So after every push, older self-uploaded versions are removed by
# default (original/agent posters like TMDb are left untouched).
CLEANUP_OLD_POSTERS = os.environ.get("CLEANUP_OLD_POSTERS", "true").strip().lower() not in ("false", "0", "no")

# Push/cleanup download several poster candidates from Plex per title (to
# check our badge marker) - I/O-heavy, hence parallelized like the cache
# build instead of processing one title after another.
POSTER_WORKERS = 4

# Autopilot interval (minutes): score sync, maintain the original-poster
# cache, clean up removed titles, AND automatically badge and upload
# new/changed titles to Plex - all in one cadence. 0 = disabled (default),
# then only via the buttons in the UI. For a true "runs on its own" setup,
# e.g. set to 60. Replaces the earlier CACHE_AUTO_REFRESH_MINUTES (scores only).
AUTO_SYNC_INTERVAL_MINUTES = int(os.environ.get("AUTO_SYNC_INTERVAL_MINUTES", "0") or "0")
# Minimum gap between two sitemap fetches (manual or automatic), so
# isitwokeornot.com isn't overloaded by spam clicks or a too-tight cron
# schedule.
CACHE_REBUILD_COOLDOWN_SECONDS = int(os.environ.get("CACHE_REBUILD_COOLDOWN_MINUTES", "5") or "5") * 60

DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = DATA_DIR / "score_cache.json"
# Original-poster cache: unbadged, fetched directly from Plex's agent
# candidates. Keyed by Plex's ratingKey, kept warm for the whole library by
# the autopilot and cleaned up for removed titles.
ORIGINALS_DIR = DATA_DIR / "originals"
ORIGINALS_DIR.mkdir(parents=True, exist_ok=True)
# Branded posters: score already burned in, but not yet uploaded to Plex -
# its own step, so the result can be checked locally before the push (see
# render_branded_image/push_to_plex).
BRANDED_DIR = DATA_DIR / "branded"
BRANDED_DIR.mkdir(parents=True, exist_ok=True)
# Tracks, per ratingKey, which score was last rendered/uploaded to Plex - so
# the autopilot recognizes new/changed titles without re-rendering/
# re-uploading everything every time.
RENDERED_STATE_FILE = DATA_DIR / "rendered_state.json"
PUSHED_STATE_FILE = DATA_DIR / "pushed_state.json"
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.jinja_env.globals["t"] = t
JOBS = {}  # job_id -> {"state": "running"/"done"/"error", "progress": [n, total], "log": [...]}

_rebuild_lock = threading.Lock()
_last_rebuild_started = 0.0  # epoch seconds - protects isitwokeornot.com from too-frequent fetches
_state_lock = threading.Lock()

PLEX_TYPE_TO_CACHE_PREFIX = {"movie": "movie", "show": "tv"}

DEMO_ITEMS = [
    {"ratingKey": "demo-1", "title": "Wasteman", "year": 2023, "score": 7, "type": "movie",
     "poster": "https://image.tmdb.org/t/p/w500/zWnh25eaz68JEfHim07dmgb2IJL.jpg", "sourceUrl": None},
    {"ratingKey": "demo-2", "title": "Zootopia 2", "year": 2025, "score": 86, "type": "movie",
     "poster": "https://image.tmdb.org/t/p/w500/oJ7g2CifqpStmoYQyaLQgEU32qO.jpg", "sourceUrl": None},
    {"ratingKey": "demo-3", "title": "Star Trek: Starfleet Academy", "year": 2026, "score": 87, "type": "show",
     "poster": "https://image.tmdb.org/t/p/w500/m4JnADIJkF5ck6jq7GUEcPBKxd0.jpg", "sourceUrl": None},
]


def demo_mode() -> bool:
    return not (PLEX_URL and PLEX_TOKEN)


def load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    return json.loads(CACHE_FILE.read_text(encoding="utf-8"))


def get_plex():
    from plexapi.server import PlexServer
    return PlexServer(PLEX_URL, PLEX_TOKEN)


def tmdb_id_from_item(item):
    for guid in getattr(item, "guids", []):
        if guid.id.startswith("tmdb://"):
            return guid.id.split("tmdb://", 1)[1]
    return None


# UTM parameters on links to review pages on isitwokeornot.com, so the
# operator can see how much traffic Wokearr sends them (at their own
# request).
REVIEW_LINK_UTM = {"utm_source": "wokearr", "utm_medium": "referral", "utm_campaign": "poster_badge"}


def _with_utm(url: str | None) -> str | None:
    if not url:
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query.update(REVIEW_LINK_UTM)
    return urlunsplit(parts._replace(query=urlencode(query)))


def _is_own_badge(img_bytes: bytes | None) -> bool | None:
    """
    Detects from the burned-in JPEG comment whether this image was produced
    by add_badge() (see badge.BADGE_MARKER) - more reliable than relying on
    Plex/plexapi metadata like "provider" (which turned out not to be).

    True = definitely our marker, False = definitely no marker (clean),
    None = undecidable (e.g. an aborted/broken download). The None case must
    NEVER be treated like False, otherwise a download hiccup on an already-
    badged poster of ours could make it falsely pass as a "clean original"
    and get badged again.
    """
    if img_bytes is None:
        return None
    try:
        with Image.open(io.BytesIO(img_bytes)) as im:
            return im.info.get("comment") == BADGE_MARKER
    except Exception:
        return None


def _poster_candidate_bytes(plex, p) -> bytes | None:
    key = getattr(p, "key", "") or ""
    if not key:
        return None
    url = key if key.startswith("http") else plex.url(key, includeToken=True)
    try:
        return requests.get(url, timeout=20).content
    except requests.RequestException:
        return None


def _is_upload_poster(p) -> bool:
    """
    True if this Plex poster candidate is a manual upload (ours or someone
    else's/from before, including from before the badge marker existed) -
    detected via Plex's own key scheme ("upload://posters/..."). Anything
    else (direct TMDb/Fanart/TheTVDB/Amazon/Gracenote URLs, Plex's internal
    agent reference "metadata://posters/...") is an agent/original poster.

    More reliable than the badge marker alone: the marker only recognizes
    posters WE generated ourselves since it was introduced, not uploads from
    before that. The key scheme, by contrast, is independent of the upload's age.
    """
    key = getattr(p, "key", "") or ""
    return "upload://posters" in key or "upload%3A%2F%2Fposters" in key


def fetch_original_poster_bytes(plex, item) -> tuple[bytes, bool]:
    """
    Returns the unbadged original poster. Reads primarily from the local
    cache (ORIGINALS_DIR) - only things previously and unambiguously verified
    as an agent original land there, so the trust is justified and a fresh
    live check against Plex isn't needed.

    Only if nothing is cached yet for this title (e.g. new in Plex) does it
    check live against Plex: go through all poster candidates, always ignore
    uploads (see _is_upload_poster) - including unmarked ones from before the
    badge marker existed, which could otherwise have falsely smuggled
    themselves through as an "original" - and take the first remaining
    (agent) candidate. The badge marker here only serves as an extra safety
    check on the chosen candidate.

    If no usable candidate is found at all, the currently selected poster is
    used as a last resort (may theoretically already be badged). Returns
    (image_bytes, original_found).
    """
    cached = ORIGINALS_DIR / f"{item.ratingKey}.jpg"
    if cached.exists():
        return cached.read_bytes(), True

    try:
        for p in item.posters():
            if _is_upload_poster(p):
                continue
            data = _poster_candidate_bytes(plex, p)
            if data is None or _is_own_badge(data) is True:
                continue
            cached.write_bytes(data)
            return data, True
    except Exception:
        pass

    poster_url = plex.url(item.thumb, includeToken=True)
    return requests.get(poster_url, timeout=20).content, False


def cleanup_old_uploaded_posters(plex, item) -> int:
    """
    Deletes (best effort) older, manually uploaded poster versions of this
    Plex item (see _is_upload_poster) - everything except the currently
    selected one. This also catches uploads from before the badge marker
    existed. A deletion attempt on an agent poster (e.g. TMDb) can't happen
    at all thanks to the key check.

    Logs every candidate along with the decision/result to stdout (visible
    in the container logs).
    """
    removed = 0
    try:
        candidates = list(item.posters())
    except Exception as e:
        print(f"[cleanup] {item.title}: item.posters() failed: {e}", flush=True)
        return 0

    for p in candidates:
        key = getattr(p, "key", "?")
        if getattr(p, "selected", False):
            print(f"[cleanup] {item.title}: skipped (currently selected) - {key}", flush=True)
            continue
        if not _is_upload_poster(p):
            print(f"[cleanup] {item.title}: skipped (agent poster) - {key}", flush=True)
            continue
        try:
            p.delete()
            removed += 1
            print(f"[cleanup] {item.title}: deleted - {key}", flush=True)
        except Exception as e:
            print(f"[cleanup] {item.title}: delete failed ({e}) - {key}", flush=True)
    return removed


def _load_state_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _record_state(path: Path, rating_key, score) -> None:
    with _state_lock:
        state = _load_state_file(path)
        state[str(rating_key)] = score
        path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def render_branded_image(item, entry: dict, force: bool = False) -> Path:
    """
    Burns the score from 'entry' onto the locally cached original poster of
    'item' (see ORIGINALS_DIR) and saves the result under
    BRANDED_DIR/<ratingKey>.jpg - a pure local file operation, no Plex
    contact. Anyone who wants to check whether a badge looks right can open
    this file directly in the Docker volume before anything reaches Plex.

    Skips rendering if already rendered with the same score
    (RENDERED_STATE_FILE), unless force=True. Needs an already-cached
    original (see fetch_original_poster_bytes) - otherwise raises FileNotFoundError.
    """
    rk = str(item.ratingKey)
    branded_path = BRANDED_DIR / f"{rk}.jpg"
    rendered_state = _load_state_file(RENDERED_STATE_FILE)
    if not force and branded_path.exists() and rendered_state.get(rk) == entry["score"]:
        return branded_path

    original_path = ORIGINALS_DIR / f"{rk}.jpg"
    if not original_path.exists():
        raise FileNotFoundError(t("render.missing_original", title=item.title))

    add_badge(
        str(original_path),
        entry["score"],
        str(branded_path),
        position=BADGE_POSITION,
        label_style=BADGE_LABEL_STYLE,
        width_percent=BADGE_WIDTH_PERCENT,
    )
    _record_state(RENDERED_STATE_FILE, rk, entry["score"])
    return branded_path


def push_to_plex(plex, item, entry: dict, force: bool = False) -> str:
    """
    Uploads the locally branded poster (see render_branded_image, rendered
    on demand if needed) to Plex and afterwards (if enabled) cleans up old
    self-uploads. Tracks the uploaded score (PUSHED_STATE_FILE). Shared by
    "Push to Plex" and the autopilot.
    """
    branded_path = render_branded_image(item, entry, force=force)
    item.uploadPoster(filepath=str(branded_path))
    if CLEANUP_OLD_POSTERS:
        cleanup_old_uploaded_posters(plex, item)
    _record_state(PUSHED_STATE_FILE, item.ratingKey, entry["score"])
    return t("push.ok", title=item.title)


def _current_library_items(plex) -> dict:
    """Returns {ratingKey: (plex_item, cache_prefix)} for all configured
    libraries - the basis for the Plex comparison in the autopilot."""
    items = {}
    for section_name in LIBRARY_SECTIONS:
        try:
            section = plex.library.section(section_name)
        except Exception:
            continue
        prefix = PLEX_TYPE_TO_CACHE_PREFIX.get(section.type)
        if not prefix:
            continue
        for it in section.all():
            items[str(it.ratingKey)] = (it, prefix)
    return items


def _emit(log, msg, prefix="sync"):
    print(f"[{prefix}] {msg}", flush=True)
    if log:
        log(msg)


def score_sync(log=None) -> None:
    """Stage 1: incremental score sync against isitwokeornot.com. Triggered
    standalone via the "Update Score Database" button or as part of the autopilot."""
    wait = _reserve_rebuild_slot()
    if wait:
        _emit(log, t("score_sync.cooldown", seconds=int(wait)))
        return
    import build_score_cache as bsc
    cache = load_cache()

    def on_progress(done, total):
        if done and done % 200 == 0:
            CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")

    cache, processed = bsc.build_cache(cache, on_progress=on_progress)
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    _emit(log, t("score_sync.done", processed=processed, total=len(cache)))


def sync_library(log=None) -> list[str]:
    """
    Stage 2 - Plex comparison, without uploading anything to Plex:
      - fetch missing original posters for titles with a known score (ORIGINALS_DIR)
      - render the branded version from them, unless already done with the
        current score (BRANDED_DIR, see render_branded_image)
      - delete local original/branded files for titles no longer in the
        (already fetched here anyway) Plex library ("orphans")

    Returns the titles for which no TMDb original was found (see
    "missing_originals" in api_sync_library) - evaluated independent of
    language, so the frontend popup isn't reliant on translated log text.
    """
    if demo_mode():
        _emit(log, t("sync.demo_mode"))
        return []

    cache = load_cache()
    plex = get_plex()
    library = _current_library_items(plex)
    valid_keys = set(library.keys())

    titled_entries = []
    for rk, (item, prefix) in library.items():
        tmdb_id = tmdb_id_from_item(item)
        entry = cache.get(f"{prefix}:{tmdb_id}") if tmdb_id else None
        if entry:
            titled_entries.append((rk, item, entry))

    # Fetch original posters where none is cached yet
    to_warm = [(item, entry) for rk, item, entry in titled_entries if not (ORIGINALS_DIR / f"{rk}.jpg").exists()]
    warnings = []
    if to_warm:
        with ThreadPoolExecutor(max_workers=POSTER_WORKERS) as pool:
            futures = {pool.submit(fetch_original_poster_bytes, plex, item): item for item, _ in to_warm}
            for f in as_completed(futures):
                item = futures[f]
                try:
                    _, found = f.result()
                    if not found:
                        warnings.append(item.title)
                except Exception as e:
                    _emit(log, t("sync.poster_fetch_error", title=item.title, error=e))
        _emit(log, t("sync.posters_fetched", count=len(to_warm)))
    if warnings:
        _emit(log, t("sync.missing_originals_warning", titles=", ".join(warnings)))

    # Render the branded version for new/changed titles
    rendered_state = _load_state_file(RENDERED_STATE_FILE)
    to_render = [
        (item, entry) for rk, item, entry in titled_entries
        if (ORIGINALS_DIR / f"{rk}.jpg").exists() and rendered_state.get(rk) != entry["score"]
    ]
    rendered = 0
    for item, entry in to_render:
        try:
            render_branded_image(item, entry)
            rendered += 1
        except Exception as e:
            _emit(log, t("sync.render_error", title=item.title, error=e))
    if rendered:
        _emit(log, t("sync.rendered_count", count=rendered))

    # Remove orphans (titles no longer in Plex) - in both local folders
    removed = 0
    for folder in (ORIGINALS_DIR, BRANDED_DIR):
        for f in folder.glob("*.jpg"):
            if f.stem not in valid_keys:
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
    if removed:
        _emit(log, t("sync.orphans_removed", count=removed))

    if not to_warm and not to_render and not removed:
        _emit(log, t("sync.nothing_new"))

    return warnings


def push_pending_to_plex(log=None, progress=None, force: bool = False, rating_keys=None) -> None:
    """
    Stage 3 - uploads posters to Plex. Without rating_keys: the whole
    library, but by default (force=False) only titles that are new or whose
    score has changed since the last push (PUSHED_STATE_FILE) - so the
    autopilot does nothing when the state is unchanged. With rating_keys:
    only these titles; force=True (e.g. clicking "Push to Plex") uploads
    them again regardless, independent of the last pushed score - e.g. to
    force everything to re-upload after changing a badge setting.
    progress(done, total) is an optional callback for a progress indicator in the UI.
    """
    if demo_mode():
        _emit(log, t("push.demo_mode"))
        return

    cache = load_cache()
    plex = get_plex()
    library = _current_library_items(plex)

    if rating_keys is not None:
        selection = {rk: library[rk] for rk in rating_keys if rk in library}
    else:
        selection = library

    pushed_state = _load_state_file(PUSHED_STATE_FILE)
    to_push = []
    for rk, (item, prefix) in selection.items():
        tmdb_id = tmdb_id_from_item(item)
        entry = cache.get(f"{prefix}:{tmdb_id}") if tmdb_id else None
        if not entry:
            continue
        if force or pushed_state.get(rk) != entry["score"]:
            to_push.append((item, entry))

    if not to_push:
        _emit(log, t("push.nothing_to_push"))
        if progress:
            progress(0, 0)
        return

    progress_lock = threading.Lock()
    done = 0

    def process(item, entry):
        nonlocal done
        try:
            _emit(log, push_to_plex(plex, item, entry, force))
        except Exception as e:
            _emit(log, t("push.error", title=item.title, error=e))
        finally:
            if progress:
                with progress_lock:
                    done += 1
                    progress(done, len(to_push))

    with ThreadPoolExecutor(max_workers=POSTER_WORKERS) as pool:
        futures = [pool.submit(process, item, entry) for item, entry in to_push]
        for f in as_completed(futures):
            pass


def autonomous_sync(log=None, progress=None) -> None:
    """Autopilot: all three stages in sequence (score sync, Plex comparison
    incl. rendering, push). Usable individually for manual intervention: see
    score_sync/sync_library/push_pending_to_plex."""
    def _progress(step):
        if progress:
            progress(step, 3)

    score_sync(log=log)
    _progress(1)
    sync_library(log=log)
    _progress(2)
    push_pending_to_plex(log=log)
    _progress(3)


def _reserve_rebuild_slot() -> float:
    """
    Reserves a cache-rebuild run if the cooldown since the last run has
    elapsed. Returns 0 (and reserves) if a run may start, otherwise the
    remaining wait time in seconds.
    """
    global _last_rebuild_started
    with _rebuild_lock:
        elapsed = time.time() - _last_rebuild_started
        if elapsed < CACHE_REBUILD_COOLDOWN_SECONDS:
            return CACHE_REBUILD_COOLDOWN_SECONDS - elapsed
        _last_rebuild_started = time.time()
        return 0.0


def _auto_sync_loop():
    interval = AUTO_SYNC_INTERVAL_MINUTES * 60
    while True:
        time.sleep(interval)
        try:
            autonomous_sync()
        except Exception as e:
            print(f"[auto-sync] Unexpected error: {e}", flush=True)


if AUTO_SYNC_INTERVAL_MINUTES > 0:
    threading.Thread(target=_auto_sync_loop, daemon=True).start()


@app.route("/")
def index():
    return render_template(
        "index.html",
        demo=demo_mode(),
        badge_label_style=BADGE_LABEL_STYLE,
        language=LANGUAGE,
        i18n=TRANSLATIONS,
    )


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok", "demo_mode": demo_mode()})


@app.route("/api/library")
def api_library():
    if demo_mode():
        return jsonify({"demo": True, "items": DEMO_ITEMS})

    cache = load_cache()
    plex = get_plex()
    items = []
    for section_name in LIBRARY_SECTIONS:
        try:
            section = plex.library.section(section_name)
        except Exception:
            continue
        prefix = PLEX_TYPE_TO_CACHE_PREFIX.get(section.type)
        if not prefix:
            continue
        for it in section.all():
            tmdb_id = tmdb_id_from_item(it)
            if not tmdb_id:
                continue
            entry = cache.get(f"{prefix}:{tmdb_id}")
            if not entry:
                continue
            items.append({
                "ratingKey": it.ratingKey,
                "title": it.title,
                "year": it.year,
                "score": entry["score"],
                "type": section.type,
                "sourceUrl": _with_utm(entry.get("url")),
            })
    return jsonify({"demo": False, "items": items})


@app.route("/api/poster/<rating_key>")
def api_poster(rating_key):
    """
    Poster for the grid view. Comes from the local original cache
    (ORIGINALS_DIR) - so Wokearr always shows its own, clean original (the
    score is a pure CSS overlay on top, see app.js), regardless of what's
    actually selected as the poster in Plex right now. Only falls back to
    Plex's current poster if no original is cached yet for this title (e.g.
    before the first "Sync Now").

    Explicitly not cacheable (Cache-Control: no-store), since the file can
    change at any time via sync/autopilot while the URL itself stays the same.
    """
    demo = next((d for d in DEMO_ITEMS if d["ratingKey"] == rating_key), None)
    if demo:
        img = requests.get(demo["poster"], timeout=15).content
        resp = send_file(io.BytesIO(img), mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    cached = ORIGINALS_DIR / f"{rating_key}.jpg"
    if cached.exists():
        resp = send_file(cached, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    plex = get_plex()
    item = plex.fetchItem(int(rating_key))
    poster_url = plex.url(item.thumb, includeToken=True)
    img = requests.get(poster_url, timeout=15).content
    resp = send_file(io.BytesIO(img), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/rebuild-cache", methods=["POST"])
def api_rebuild_cache():
    payload = request.get_json(silent=True) or {}
    full = bool(payload.get("full"))

    wait = _reserve_rebuild_slot()
    if wait:
        return jsonify({"error": t("rebuild_cache.cooldown_error", seconds=int(wait) + 1)}), 429

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"state": "running", "progress": [0, 0], "log": [t("rebuild_cache.loading")]}

    def run():
        try:
            import build_score_cache as bsc
            cache = load_cache()

            def on_progress(done, total):
                JOBS[job_id]["progress"] = [done, total]
                if done and done % 200 == 0:
                    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")

            cache, processed = bsc.build_cache(cache, on_progress=on_progress, skip_existing=not full)
            CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
            JOBS[job_id]["state"] = "done"
            JOBS[job_id]["log"].append(t("rebuild_cache.done", processed=processed, total=len(cache)))
        except Exception as e:
            JOBS[job_id]["state"] = "error"
            JOBS[job_id]["log"].append(str(e))

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/sync-library", methods=["POST"])
def api_sync_library():
    """Stage 2, manual: Plex comparison - fetch original posters, render
    branded versions, clean up removed titles. No push to Plex (see
    /api/apply for that)."""
    if demo_mode():
        return jsonify({"error": t("api.sync_library.demo_error")}), 400

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"state": "running", "progress": [0, 0], "log": []}

    def run():
        try:
            missing_originals = sync_library(log=lambda msg: JOBS[job_id]["log"].append(msg))
            JOBS[job_id]["missing_originals"] = missing_originals
            JOBS[job_id]["state"] = "done"
        except Exception as e:
            JOBS[job_id]["state"] = "error"
            JOBS[job_id]["log"].append(str(e))

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/apply", methods=["POST"])
def api_apply():
    """Stage 3, manual ("Push to Plex"): uploads the branded posters of the
    given titles to Plex - forced (force=True), regardless of whether the
    score has changed since the last push. Renders on demand automatically
    if needed (see push_to_plex)."""
    if demo_mode():
        return jsonify({"error": t("api.apply.demo_error")}), 400

    payload = request.get_json(force=True)
    rating_keys = [str(rk) for rk in payload.get("ratingKeys", [])]
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"state": "running", "progress": [0, len(rating_keys)], "log": []}

    def run():
        try:
            push_pending_to_plex(
                log=lambda msg: JOBS[job_id]["log"].append(msg),
                progress=lambda done, total: JOBS[job_id].update(progress=[done, total]),
                force=True,
                rating_keys=rating_keys,
            )
            JOBS[job_id]["state"] = "done"
        except Exception as e:
            JOBS[job_id]["state"] = "error"
            JOBS[job_id]["log"].append(str(e))

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/cleanup-posters", methods=["POST"])
def api_cleanup_posters():
    """Goes through the whole library once and removes old, self-uploaded
    poster versions from Plex (see cleanup_old_uploaded_posters). Always
    available regardless of CLEANUP_OLD_POSTERS, since it's explicitly triggered."""
    if demo_mode():
        return jsonify({"error": t("api.cleanup.demo_error")}), 400

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"state": "running", "progress": [0, 0], "log": []}

    def run():
        try:
            plex = get_plex()
            items = []
            for section_name in LIBRARY_SECTIONS:
                try:
                    items.extend(plex.library.section(section_name).all())
                except Exception:
                    continue
            JOBS[job_id]["progress"] = [0, len(items)]

            progress_lock = threading.Lock()
            done = 0
            removed_total = 0

            def process(item):
                nonlocal done, removed_total
                try:
                    removed = cleanup_old_uploaded_posters(plex, item)
                except Exception as e:
                    removed = 0
                    JOBS[job_id]["log"].append(t("cleanup.item_error", title=getattr(item, "title", "?"), error=e))
                with progress_lock:
                    done += 1
                    removed_total += removed
                    JOBS[job_id]["progress"] = [done, len(items)]

            with ThreadPoolExecutor(max_workers=POSTER_WORKERS) as pool:
                futures = [pool.submit(process, item) for item in items]
                for f in as_completed(futures):
                    pass

            JOBS[job_id]["state"] = "done"
            JOBS[job_id]["log"].append(t("cleanup.done", count=removed_total))
        except Exception as e:
            JOBS[job_id]["state"] = "error"
            JOBS[job_id]["log"].append(str(e))

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/job/<job_id>")
def api_job(job_id):
    return jsonify(JOBS.get(job_id, {"state": "unknown"}))


if __name__ == "__main__":
    # Only for local development outside of Docker - gunicorn runs in the container (see Dockerfile)
    print("Demo mode:", demo_mode())
    app.run(host="0.0.0.0", port=5005, debug=True)
