#!/usr/bin/env python3
"""
Wokearr - small local web UI (Radarr/Sonarr style) for traffic-light badges
on Plex posters, score source: isitwokeornot.com

Configuration is done entirely via environment variables (see .env.example),
so the image can be published to GitHub/Docker Hub without any code changes.
"""
import datetime
import io
import json
import logging
import re
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests
from croniter import croniter
from flask import Flask, jsonify, request, send_file, render_template
from PIL import Image

from badge import (  # noqa: E402
    add_badge,
    BADGE_MARKER,
    BAND_KEYS,
    BAND_MAX,
    COLOR_SCHEMES,
    DEFAULT_COLOR_SCHEME,
)
from logsetup import setup_logging, get_logger
from auth import Auth
import build_score_cache as bsc
import notify

setup_logging()
log_config = get_logger("config")
log_i18n = get_logger("i18n")
log_run = get_logger("run")
log_score = get_logger("score-sync")
log_plex = get_logger("plex-sync")
log_push = get_logger("push")
log_cleanup = get_logger("cleanup")
log_autopilot = get_logger("autopilot")
log_history = get_logger("history")
log_notify = get_logger("notify")

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

# Color palette for the five score bands: "standard" mirrors the colors
# isitwokeornot.com uses themselves, "modified" is the alternative
# green -> yellow -> orange -> red -> violet ramp (see badge.COLOR_SCHEMES).
BADGE_COLOR_SCHEME = os.environ.get("BADGE_COLOR_SCHEME", DEFAULT_COLOR_SCHEME).strip().lower()
if BADGE_COLOR_SCHEME not in COLOR_SCHEMES:
    log_config.warning("Unknown BADGE_COLOR_SCHEME=%r, falling back to %r. Available: %s.",
                       BADGE_COLOR_SCHEME, DEFAULT_COLOR_SCHEME, ", ".join(sorted(COLOR_SCHEMES)))
    BADGE_COLOR_SCHEME = DEFAULT_COLOR_SCHEME

# Version/build info, injected as build args by the GitHub Action (see
# Dockerfile). Outside of Docker these stay unset - shown as "dev" then.
APP_VERSION = os.environ.get("APP_VERSION", "").strip() or "dev"
BUILD_DATE = os.environ.get("BUILD_DATE", "").strip()

# Project home, linked from the version in the footer. A tagged version points
# at its release notes, anything else (latest/dev) at the repository.
REPO_URL = "https://github.com/Jake-double-one/wokearr"
VERSION_URL = (
    f"{REPO_URL}/releases/tag/{APP_VERSION}"
    if re.fullmatch(r"v\d+(\.\d+)*", APP_VERSION)
    else REPO_URL
)

# How long run-protocol entries are kept: "<number><unit>" with d(ays),
# w(eeks) or m(onths, 30 days). "0" keeps everything (up to the hard cap).
RUN_HISTORY_RETENTION_DEFAULT = "4w"
_RETENTION_UNIT_DAYS = {"d": 1, "w": 7, "m": 30}


def _parse_retention_days(raw: str) -> int:
    """'3d'/'2w'/'6m' -> number of days. 0 means "keep everything"; anything
    unparseable falls back to the default instead of dropping the protocol."""
    value = (raw or "").strip().lower()
    if value in ("", "0"):
        return 0
    match = re.fullmatch(r"(\d+)\s*([dwm])", value)
    if not match:
        log_config.warning("Unparseable RUN_HISTORY_RETENTION=%r, using %r. Expected e.g. 3d, 2w, 6m.",
                           raw, RUN_HISTORY_RETENTION_DEFAULT)
        return _parse_retention_days(RUN_HISTORY_RETENTION_DEFAULT)
    return int(match.group(1)) * _RETENTION_UNIT_DAYS[match.group(2)]


RUN_HISTORY_RETENTION_DAYS = _parse_retention_days(
    os.environ.get("RUN_HISTORY_RETENTION", RUN_HISTORY_RETENTION_DEFAULT)
)

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
    log_i18n.warning("No locale file found for LANGUAGE=%r, falling back to %s.", LANGUAGE, DEFAULT_LANGUAGE)
    # Fall LANGUAGE itself back too (not just individual keys) - otherwise
    # e.g. <html lang="fr"> would show on text that's actually all English.
    LANGUAGE = DEFAULT_LANGUAGE
# Per key: active language, else en-US - so every key is guaranteed to
# exist, without frontend/backend having to rebuild their own fallback logic.
TRANSLATIONS = {**_FALLBACK_TRANSLATIONS, **_load_locale(LANGUAGE)}


def _format(translations: dict, key: str, **kwargs) -> str:
    template = translations.get(key, key)
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        # A broken/inconsistent translation template must never crash a
        # request - show it unformatted rather than fail.
        return template


def t(key: str, **kwargs) -> str:
    return _format(TRANSLATIONS, key, **kwargs)


def t_log(key: str, **kwargs) -> str:
    """Same text in English, for the container log: one language there no
    matter what the UI is set to, so logs stay greppable and can be shared
    in an issue as-is."""
    return _format(_FALLBACK_TRANSLATIONS, key, **kwargs)

# Plex keeps the previous version of every uploaded poster as poster history
# and never deletes it on its own - otherwise the Plex server just keeps
# growing. So after every push, older self-uploaded versions are removed by
# default (original/agent posters like TMDb are left untouched).
CLEANUP_OLD_POSTERS = os.environ.get("CLEANUP_OLD_POSTERS", "true").strip().lower() not in ("false", "0", "no")

# Push/cleanup download several poster candidates from Plex per title (to
# check our badge marker) - I/O-heavy, hence parallelized like the cache
# build instead of processing one title after another.
POSTER_WORKERS = 4

# Autopilot schedule as a standard 5-field cron expression: score sync,
# maintain the original-poster cache, clean up removed titles, AND
# automatically badge and upload new/changed titles to Plex - all in one
# cadence, at whatever times the expression fires. Empty or "0" = disabled
# (default), then only via the buttons in the UI. E.g. "0 * * * *" for
# hourly, "0 3 * * *" for daily at 3 AM. Replaces the earlier
# AUTO_SYNC_INTERVAL_MINUTES (fixed-minute interval since container start).
AUTO_SYNC_CRON = os.environ.get("AUTO_SYNC_CRON", "").strip()
if AUTO_SYNC_CRON in ("", "0"):
    AUTO_SYNC_CRON = None
elif not croniter.is_valid(AUTO_SYNC_CRON):
    log_config.error("Invalid AUTO_SYNC_CRON=%r (not a valid 5-field cron expression) - autopilot disabled.",
                     AUTO_SYNC_CRON)
    AUTO_SYNC_CRON = None
# Minimum gap between two sitemap fetches (manual or automatic), so
# isitwokeornot.com isn't overloaded by spam clicks or a too-tight cron
# schedule.
CACHE_REBUILD_COOLDOWN_SECONDS = int(os.environ.get("CACHE_REBUILD_COOLDOWN_MINUTES", "5") or "5") * 60

# Notifications for autopilot runs (see notify.py for the URL formats):
# NOTIFY_URL is where to, NOTIFY_ON what for - "error" (a stage failed)
# and/or "changes" (new titles badged, scores of your titles changed).
NOTIFY_TARGET = None
_notify_url = os.environ.get("NOTIFY_URL", "").strip()
if _notify_url:
    try:
        NOTIFY_TARGET = notify.parse_url(_notify_url)
    except ValueError as e:
        log_config.error("Invalid NOTIFY_URL - notifications disabled: %s", e)
NOTIFY_ON_CHOICES = {"error", "changes"}
NOTIFY_ON = {v.strip().lower() for v in os.environ.get("NOTIFY_ON", "error,changes").split(",") if v.strip()}
if NOTIFY_ON - NOTIFY_ON_CHOICES:
    log_config.warning("Unknown NOTIFY_ON value(s) %s ignored. Available: error, changes.",
                       ", ".join(sorted(NOTIFY_ON - NOTIFY_ON_CHOICES)))
    NOTIFY_ON &= NOTIFY_ON_CHOICES

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
# re-uploading everything every time. Stored as [score, badge fingerprint]
# (see _badge_fingerprint), so a changed badge setting also takes effect.
RENDERED_STATE_FILE = DATA_DIR / "rendered_state.json"
PUSHED_STATE_FILE = DATA_DIR / "pushed_state.json"
# Every title in the Plex library as a cache key ("movie:123"), scored or not -
# written by the Plex comparison, read by the score sync to tell which score
# changes concern your own library (see _report_score_changes).
LIBRARY_INDEX_FILE = DATA_DIR / "library_index.json"
# Protocol of the last runs, shown in the footer (see _record_run).
RUN_HISTORY_FILE = DATA_DIR / "run_history.json"
# Hard cap, independent of RUN_HISTORY_RETENTION - a safety net so a very
# long retention combined with a very tight cron can't grow without bound.
RUN_HISTORY_MAX_ENTRIES = 500
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.jinja_env.globals["t"] = t
app.jinja_env.globals["language"] = LANGUAGE
# Optional login (AUTH_METHOD none/basic/forms) - see auth.py
auth = Auth(app, DATA_DIR, t)
JOBS = {}  # job_id -> {"state": "running"/"done"/"error", "progress": [n, total], "log": [...]}

_rebuild_lock = threading.Lock()
_last_rebuild_started = 0.0  # epoch seconds - protects isitwokeornot.com from too-frequent fetches
_previous_rebuild_started = 0.0  # restored by _release_rebuild_slot when a sitemap fetch fails
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

    Logs every deletion to the container log; the candidates that are kept
    only at LOG_LEVEL=DEBUG.
    """
    removed = 0
    try:
        candidates = list(item.posters())
    except Exception as e:
        log_cleanup.warning("%s: listing posters failed: %s", _display_title(item), _describe(e))
        return 0

    for p in candidates:
        key = getattr(p, "key", "?")
        if getattr(p, "selected", False):
            log_cleanup.debug("%s: kept (currently selected) - %s", _display_title(item), key)
            continue
        if not _is_upload_poster(p):
            log_cleanup.debug("%s: kept (agent poster) - %s", _display_title(item), key)
            continue
        try:
            p.delete()
            removed += 1
            log_cleanup.info("%s: deleted old upload - %s", _display_title(item), key)
        except Exception as e:
            log_cleanup.warning("%s: deleting old upload failed: %s - %s", _display_title(item), _describe(e), key)
    return removed


def _load_state_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _record_state(path: Path, rating_key, value) -> None:
    with _state_lock:
        state = _load_state_file(path)
        state[str(rating_key)] = value
        path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def band_styles() -> dict:
    """Band key -> {"color", "text"} for the active palette, handed to the
    template as CSS variables. The text color follows the background's
    perceived brightness, so both palettes stay legible without a hardcoded
    special case per color (the old CSS only special-cased yellow)."""
    styles = {}
    for key in BAND_KEYS:
        r, g, b = COLOR_SCHEMES[BADGE_COLOR_SCHEME][key]
        brightness = 0.299 * r + 0.587 * g + 0.114 * b
        styles[key] = {
            "color": f"#{r:02x}{g:02x}{b:02x}",
            "text": "#2a1e00" if brightness > 150 else "#ffffff",
        }
    return styles


def _badge_fingerprint() -> str:
    """
    Short fingerprint of every setting that is burned into the image. Stored
    alongside the score in rendered_state/pushed_state, so changing a badge
    setting re-renders AND re-uploads on the next sync - the score alone
    wouldn't change, so otherwise old badges would silently stay put.
    """
    return f"{BADGE_COLOR_SCHEME}|{BADGE_POSITION}|{BADGE_LABEL_STYLE}|{BADGE_WIDTH_PERCENT:g}"


def _state_value(score) -> list:
    """State entry for a score under the current badge settings. Entries
    written before this existed were plain numbers and simply compare as
    unequal - which correctly triggers a one-time re-render."""
    return [score, _badge_fingerprint()]


def _prune_runs(runs: list) -> list:
    """Drops entries older than RUN_HISTORY_RETENTION_DAYS, then enforces the
    hard cap. Entries without a usable timestamp are kept - better a stray
    line in the protocol than silently throwing data away."""
    if RUN_HISTORY_RETENTION_DAYS:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=RUN_HISTORY_RETENTION_DAYS
        )
        kept = []
        for run in runs:
            try:
                started = datetime.datetime.fromisoformat(run["started_at"])
            except (KeyError, TypeError, ValueError):
                kept.append(run)
                continue
            if started >= cutoff:
                kept.append(run)
        runs = kept
    return runs[-RUN_HISTORY_MAX_ENTRIES:]


def load_runs() -> list:
    if not RUN_HISTORY_FILE.exists():
        return []
    try:
        runs = json.loads(RUN_HISTORY_FILE.read_text(encoding="utf-8"))
        return runs if isinstance(runs, list) else []
    except Exception:
        return []


# Stat keys in the order they're summarized in the container log's
# "Finished" line (the UI has its own copy in app.js).
_RUN_SUMMARY_STATS = (
    "matched", "scores_updated", "originals_fetched", "rendered",
    "pushed", "orphans_removed", "plex_posters_removed",
)
# Caps for the per-run detail lists, so one unusual run (e.g. the first sync
# of a big library) can't bloat the protocol file.
RUN_DETAIL_LIST_MAX = 50


def _run_started(trigger: str) -> datetime.datetime:
    log_run.info("Started: %s", t_log(f"status.trigger.{trigger}"))
    return datetime.datetime.now(datetime.timezone.utc)


def _run_summary(entry: dict) -> str:
    parts = [f"{entry[key]} {t_log(f'status.stat.{key}')}" for key in _RUN_SUMMARY_STATS if entry.get(key)]
    if entry.get("score_changes"):
        parts.append(f"{len(entry['score_changes'])} {t_log('status.stat.score_changes')}")
    if entry.get("push_failed"):
        parts.append(f"{entry['push_failed']} {t_log('status.stat.errors')}")
    return ", ".join(parts) or t_log("status.no_changes")


def _record_run(trigger: str, started: datetime.datetime, stats: dict, errors: list | None = None) -> dict:
    """Appends one run to the protocol and writes its "Finished" line to the
    container log. Best-effort: a broken protocol file must never abort the
    actual sync.

    errors holds one record per failed stage (see _error_record). "error":
    True is kept alongside for protocol entries written before errors
    existed - the frontend reads both."""
    finished = datetime.datetime.now(datetime.timezone.utc)
    entry = {
        "trigger": trigger,
        "started_at": started.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 1),
        **{k: v for k, v in stats.items() if v is not None},
    }
    for key in ("score_changes", "new_titles", "missing_originals"):
        if isinstance(entry.get(key), list):
            entry[key] = entry[key][:RUN_DETAIL_LIST_MAX]
    if errors:
        entry["errors"] = errors
        entry["error"] = True

    label = t_log(f"status.trigger.{trigger}")
    if errors:
        log_run.warning("Finished with errors: %s in %.1fs - %s; %s", label, entry["duration_seconds"],
                        _run_summary(entry), "; ".join(_error_summary(e, t_log) for e in errors))
    else:
        log_run.info("Finished: %s in %.1fs - %s", label, entry["duration_seconds"], _run_summary(entry))

    try:
        with _state_lock:
            runs = _prune_runs(load_runs() + [entry])
            RUN_HISTORY_FILE.write_text(json.dumps(runs, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log_history.exception("Could not write run protocol: %s", e)
    return entry


def _describe(exc: BaseException) -> str:
    """'ReadTimeout: ...' - exception type plus message, since the message
    alone is often ambiguous (sometimes just a URL, sometimes empty)."""
    text = f"{type(exc).__name__}: {exc}"
    cause = exc.__cause__
    if cause is not None and cause is not exc:
        text += f" (caused by {type(cause).__name__}: {cause})"
    return text


def _root_error(exc: BaseException) -> BaseException:
    """First exception in the cause chain that says what actually went wrong
    on the wire - a requests or plexapi error - else exc itself. Our own
    wrappers (e.g. build_score_cache.SitemapError) sit on top of those."""
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, requests.RequestException) or type(current).__module__.startswith("plexapi"):
            return current
        current = current.__cause__ or current.__context__
    return exc


# Who a stage talks to - the fallback when an error doesn't carry a URL.
_STAGE_DEFAULT_TARGET = {"score_sync": "isitwokeornot.com", "sync": "Plex", "push": "Plex", "cleanup": "Plex"}


def _error_target(err: BaseException, stage: str) -> str:
    request_obj = getattr(err, "request", None)
    url = getattr(request_obj, "url", None) if request_obj is not None else None
    host = urlsplit(url).hostname if url else None
    plex_host = urlsplit(PLEX_URL).hostname if PLEX_URL else None
    if host and host == plex_host:
        return "Plex"
    if host:
        return host
    if type(err).__module__.startswith("plexapi"):
        return "Plex"
    return _STAGE_DEFAULT_TARGET.get(stage, "?")


def _error_record(stage: str, exc: BaseException) -> dict:
    """What the protocol and a notification get about a failed stage: a
    coarse kind the UI can put into words in any language, what couldn't be
    reached, and the raw message for whoever needs the details."""
    err = _root_error(exc)
    status = None
    if isinstance(err, requests.Timeout):
        kind = "timeout"
    elif isinstance(err, requests.ConnectionError):
        kind = "connection"
    elif isinstance(err, requests.HTTPError):
        kind = "http"
        status = getattr(getattr(err, "response", None), "status_code", None)
    elif type(err).__module__.startswith("plexapi"):
        kind = "plex_auth" if type(err).__name__ == "Unauthorized" else "plex"
    else:
        kind = "other"
    return {
        "stage": stage,
        "kind": kind,
        "target": _error_target(err, stage),
        "status": status,
        "message": _describe(exc)[:500],
    }


def _error_summary(record: dict, translate=None) -> str:
    """'Score database failed: isitwokeornot.com not responding (timeout)'."""
    translate = translate or t
    reason = translate(f"error.{record.get('kind', 'other')}",
                       target=record.get("target") or "?", status=record.get("status") or "?")
    return translate("status.stage_failed", stage=translate(f"status.trigger.{record.get('stage')}"), reason=reason)


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
    if not force and branded_path.exists() and rendered_state.get(rk) == _state_value(entry["score"]):
        return branded_path

    original_path = ORIGINALS_DIR / f"{rk}.jpg"
    if not original_path.exists():
        raise FileNotFoundError(t("render.missing_original", title=_display_title(item)))

    add_badge(
        str(original_path),
        entry["score"],
        str(branded_path),
        position=BADGE_POSITION,
        label_style=BADGE_LABEL_STYLE,
        width_percent=BADGE_WIDTH_PERCENT,
        color_scheme=BADGE_COLOR_SCHEME,
    )
    _record_state(RENDERED_STATE_FILE, rk, _state_value(entry["score"]))
    return branded_path


def push_to_plex(plex, item, entry: dict, force: bool = False) -> None:
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
    _record_state(PUSHED_STATE_FILE, item.ratingKey, _state_value(entry["score"]))


def _current_library_items(plex) -> dict:
    """Returns {ratingKey: (plex_item, cache_prefix)} for all configured
    libraries - the basis for the Plex comparison in the autopilot.

    A library name Plex doesn't know is logged along with the names it does
    know - a typo in LIBRARY_SECTIONS otherwise just looks like an empty
    library. Connection problems are raised instead of skipped: silently
    treating an unreachable Plex as "no titles" would make the orphan cleanup
    delete every cached poster."""
    items = {}
    for section_name in LIBRARY_SECTIONS:
        try:
            section = plex.library.section(section_name)
        except requests.RequestException:
            raise
        except Exception as e:
            log_plex.warning("Library %r not found in Plex (%s) - check LIBRARY_SECTIONS. Libraries in Plex: %s",
                             section_name, _describe(e), _plex_section_names(plex))
            continue
        prefix = PLEX_TYPE_TO_CACHE_PREFIX.get(section.type)
        if not prefix:
            log_plex.warning("Library %r is of type %r - only movie and show libraries are supported, skipping it.",
                             section_name, section.type)
            continue
        section_items = section.all()
        log_plex.debug("Library %r: %d titles", section_name, len(section_items))
        for it in section_items:
            items[str(it.ratingKey)] = (it, prefix)
    return items


def _plex_section_names(plex) -> str:
    try:
        return ", ".join(repr(s.title) for s in plex.library.sections()) or "none"
    except Exception:
        return "unknown"


def _seasons_for_show(item) -> list:
    """
    Returns all Plex season objects of a show (best-effort - an error here
    must never abort processing of the show itself, just skip its seasons
    for this cycle). Seasons have their own distinct poster in Plex (usually
    agent-provided, e.g. from TheTVDB) but no TMDb ID of their own, so they
    can't be matched against the score cache independently - callers badge
    them with the same score as their parent show instead.
    """
    try:
        return list(item.seasons())
    except Exception as e:
        log_plex.warning("%s: listing seasons failed, skipping them this run: %s", item.title, _describe(e))
        return []


def _display_title(item) -> str:
    """'<Show> – <Season>' for a season (parentTitle is already loaded with
    the season, no extra Plex request), otherwise just the item's own title."""
    parent_title = getattr(item, "parentTitle", None)
    return f"{parent_title} – {item.title}" if parent_title else item.title


def _emit(log, logger, key: str, level: int = logging.INFO, **kwargs) -> None:
    """One message, two audiences: the job log shown in the browser gets it
    in the UI language, the container log in English (see t_log)."""
    logger.log(level, t_log(key, **kwargs))
    if log:
        log(t(key, **kwargs))


def _save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_library_index() -> set:
    try:
        return set(json.loads(LIBRARY_INDEX_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return set()


def _save_library_index(keys: set) -> None:
    try:
        LIBRARY_INDEX_FILE.write_text(json.dumps(sorted(keys)), encoding="utf-8")
    except OSError as e:
        log_plex.warning("Could not write %s: %s", LIBRARY_INDEX_FILE.name, e)


def _report_score_changes(changes: list) -> list:
    """Logs every score isitwokeornot.com changed, and returns the changes for
    titles in your own Plex library (see LIBRARY_INDEX_FILE, written by the
    Plex comparison) - the ones worth a line in the protocol and a
    notification. Before the first Plex comparison, nothing counts as yours."""
    library = _load_library_index()
    relevant = []
    for change in sorted(changes, key=lambda c: (c.get("title") or "").lower()):
        in_library = change["key"] in library
        log_score.info("Score changed: %s (%s) %s -> %s%s", change.get("title") or "?", change["key"],
                       change["old"], change["new"], " [in your library]" if in_library else "")
        if in_library:
            relevant.append({"title": change.get("title") or change["key"],
                             "old": change["old"], "new": change["new"]})
    return relevant


def score_sync(log=None, full: bool = False, progress=None, slot_reserved: bool = False) -> dict:
    """Stage 1: score sync against isitwokeornot.com - incremental, or with
    full=True every title again. Triggered via the "Update Score Database"/
    "Full Rebuild" buttons or as part of the autopilot. Returns stats for the
    run protocol (see _record_run).

    If the sitemap fetch itself fails, the cooldown slot is handed back (see
    _release_rebuild_slot): no review page was requested, so there's nothing
    to protect isitwokeornot.com from, and the next attempt shouldn't have to
    wait out the cooldown."""
    if not slot_reserved:
        wait = _reserve_rebuild_slot()
        if wait:
            _emit(log, log_score, "score_sync.cooldown", seconds=int(wait))
            return {"skipped_cooldown": True}
    cache = load_cache()
    changes = []

    def on_progress(done, total):
        if progress:
            progress(done, total)
        if done and done % 200 == 0:
            _save_cache(cache)

    log_score.info("%s score sync, %d titles in the local database.", "Full" if full else "Incremental", len(cache))
    try:
        cache, processed = bsc.build_cache(cache, on_progress=on_progress, skip_existing=not full, changes=changes)
    except bsc.SitemapError:
        _release_rebuild_slot()
        raise
    _save_cache(cache)
    _emit(log, log_score, "score_sync.done", processed=processed, total=len(cache))
    return {"scores_updated": processed, "scores_total": len(cache), "score_changes": _report_score_changes(changes)}


def sync_library(log=None) -> dict:
    """
    Stage 2 - Plex comparison, without uploading anything to Plex:
      - fetch missing original posters for titles with a known score (ORIGINALS_DIR)
      - render the branded version from them, unless already done with the
        current score (BRANDED_DIR, see render_branded_image)
      - delete local original/branded files for titles no longer in the
        (already fetched here anyway) Plex library ("orphans")

    Returns stats for the run protocol, including "missing_originals": the
    titles for which no TMDb original was found. That list is evaluated
    independent of language, so the frontend popup isn't reliant on
    translated log text.
    """
    if demo_mode():
        _emit(log, log_plex, "sync.demo_mode")
        return {"missing_originals": []}

    cache = load_cache()
    plex = get_plex()
    library = _current_library_items(plex)
    valid_keys = set(library.keys())

    # Shows only carry one score for the whole series, but each season has
    # its own distinct poster in Plex - badge every season with its show's
    # score too, so browsing into a show doesn't hit unbadged season tiles.
    # Seasons have no TMDb ID of their own, so they piggyback on their
    # show's match instead of being looked up independently.
    titled_entries = []
    matched_titles = 0  # movies/shows only - seasons inherit and would inflate this
    library_index = set()  # every title in Plex as a cache key, scored or not
    for rk, (item, prefix) in library.items():
        tmdb_id = tmdb_id_from_item(item)
        if not tmdb_id:
            log_plex.debug("%s: no TMDb ID in Plex, can't be matched.", item.title)
            continue
        library_index.add(f"{prefix}:{tmdb_id}")
        entry = cache.get(f"{prefix}:{tmdb_id}")
        if not entry:
            continue
        titled_entries.append((rk, item, entry))
        matched_titles += 1
        if prefix == "tv":
            for season in _seasons_for_show(item):
                season_rk = str(season.ratingKey)
                valid_keys.add(season_rk)
                titled_entries.append((season_rk, season, entry))
    if library:
        _save_library_index(library_index)
    log_plex.info("Plex library: %d titles, %d with a score (%d posters incl. seasons).",
                  len(library), matched_titles, len(titled_entries))

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
                        warnings.append(_display_title(item))
                    else:
                        log_plex.debug("Fetched original poster: %s", _display_title(item))
                except Exception as e:
                    _emit(log, log_plex, "sync.poster_fetch_error", level=logging.WARNING,
                          title=_display_title(item), error=_describe(e))
        _emit(log, log_plex, "sync.posters_fetched", count=len(to_warm))
    if warnings:
        _emit(log, log_plex, "sync.missing_originals_warning", level=logging.WARNING, titles=", ".join(warnings))

    # Render the branded version for new/changed titles
    rendered_state = _load_state_file(RENDERED_STATE_FILE)
    to_render = [
        (item, entry) for rk, item, entry in titled_entries
        if (ORIGINALS_DIR / f"{rk}.jpg").exists()
        and (
            rendered_state.get(rk) != _state_value(entry["score"])
            or not (BRANDED_DIR / f"{rk}.jpg").exists()
        )
    ]
    rendered = 0
    for item, entry in to_render:
        try:
            render_branded_image(item, entry)
            rendered += 1
            log_plex.debug("Rendered: %s (%s%%)", _display_title(item), entry["score"])
        except Exception as e:
            _emit(log, log_plex, "sync.render_error", level=logging.WARNING, title=_display_title(item), error=_describe(e))
    if rendered:
        _emit(log, log_plex, "sync.rendered_count", count=rendered)

    # Remove orphans (titles no longer in Plex) - in both local folders. Not
    # when Plex returned no titles at all: that's far more likely a problem
    # (every library name wrong, an empty response) than a library that was
    # really emptied, and would otherwise wipe every cached poster.
    removed = 0
    if library:
        for folder in (ORIGINALS_DIR, BRANDED_DIR):
            for f in folder.glob("*.jpg"):
                if f.stem not in valid_keys:
                    try:
                        f.unlink()
                        removed += 1
                        log_plex.debug("Removed orphan: %s/%s", folder.name, f.name)
                    except OSError as e:
                        log_plex.warning("Could not remove orphan %s/%s: %s", folder.name, f.name, e)
        if removed:
            _emit(log, log_plex, "sync.orphans_removed", count=removed)
    else:
        log_plex.warning("Plex returned no titles - skipping the orphan cleanup as a precaution.")

    if not to_warm and not to_render and not removed:
        _emit(log, log_plex, "sync.nothing_new")

    return {
        "matched": matched_titles,
        "originals_fetched": len(to_warm),
        "rendered": rendered,
        "orphans_removed": removed,
        "missing_originals": warnings,
    }


def push_pending_to_plex(log=None, progress=None, force: bool = False, rating_keys=None) -> dict:
    """
    Stage 3 - uploads posters to Plex. Without rating_keys: the whole
    library, but by default (force=False) only titles that are new or whose
    score has changed since the last push (PUSHED_STATE_FILE) - so the
    autopilot does nothing when the state is unchanged. With rating_keys:
    only these titles; force=True (e.g. clicking "Push to Plex") uploads
    them again regardless, independent of the last pushed score - e.g. to
    force everything to re-upload after changing a badge setting.
    progress(done, total) is an optional callback for a progress indicator in the UI.

    Besides the counts, returns "new_titles" (movies/shows that got their
    very first badge in Plex, for the protocol and notifications) and, if any
    upload failed, "push_error": the first failure as an error record, so
    the protocol can say why instead of just how many.
    """
    if demo_mode():
        _emit(log, log_push, "push.demo_mode")
        return {"pushed": 0, "push_failed": 0}

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
        if force or pushed_state.get(rk) != _state_value(entry["score"]):
            to_push.append((item, entry))
        # Push the show's seasons along with it - same score, own poster
        # (see _seasons_for_show). Applies whether pushing the whole library
        # or just this one show via rating_keys.
        if prefix == "tv":
            for season in _seasons_for_show(item):
                season_rk = str(season.ratingKey)
                if force or pushed_state.get(season_rk) != _state_value(entry["score"]):
                    to_push.append((season, entry))

    if not to_push:
        _emit(log, log_push, "push.nothing_to_push")
        if progress:
            progress(0, 0)
        return {"pushed": 0, "push_failed": 0}

    log_push.info("Uploading %d poster(s) to Plex%s.", len(to_push), " (forced)" if force else "")
    progress_lock = threading.Lock()
    done = 0
    pushed = 0
    failed = 0
    first_error = None
    new_titles = []

    def process(item, entry):
        nonlocal done, pushed, failed, first_error
        rk = str(item.ratingKey)
        try:
            push_to_plex(plex, item, entry, force)
        except Exception as e:
            _emit(log, log_push, "push.error", level=logging.WARNING, title=_display_title(item), error=_describe(e))
            log_push.debug("Traceback for %s:", _display_title(item), exc_info=True)
            with progress_lock:
                failed += 1
                if first_error is None:
                    first_error = _error_record("push", e)
        else:
            _emit(log, log_push, "push.ok", title=_display_title(item))
            with progress_lock:
                pushed += 1
                # Movies/shows only (seasons aren't in library), and only if
                # this title had never been uploaded before
                if rk in library and rk not in pushed_state:
                    new_titles.append(_display_title(item))
        finally:
            if progress:
                with progress_lock:
                    done += 1
                    progress(done, len(to_push))

    with ThreadPoolExecutor(max_workers=POSTER_WORKERS) as pool:
        futures = [pool.submit(process, item, entry) for item, entry in to_push]
        for f in as_completed(futures):
            pass

    return {
        "pushed": pushed,
        "push_failed": failed,
        "new_titles": sorted(new_titles, key=str.lower),
        "push_error": first_error,
    }


def autonomous_sync(log=None, progress=None) -> dict:
    """Autopilot: all three stages in sequence (score sync, Plex comparison
    incl. rendering, push), recorded as one entry in the run protocol.
    Usable individually for manual intervention: see score_sync/
    sync_library/push_pending_to_plex.

    The stages fail independently: if isitwokeornot.com is unreachable, the
    run carries on with the scores already in the local database, so new
    Plex titles still get their badge. Only a failed Plex comparison skips
    the push - it needs the same Plex connection that just failed."""
    def _progress(step):
        if progress:
            progress(step, 3)

    started = _run_started("autopilot")
    stats, errors = {}, []

    def run_stage(stage, fn) -> bool:
        try:
            stats.update(fn())
            return True
        except Exception as e:
            record = _error_record(stage, e)
            errors.append(record)
            log_autopilot.exception("%s", _error_summary(record, t_log))
            return False

    try:
        if not run_stage("score_sync", lambda: score_sync(log=log)):
            log_autopilot.warning("Continuing with the scores already in the local database.")
        _progress(1)
        if run_stage("sync", lambda: sync_library(log=log)):
            _progress(2)
            run_stage("push", lambda: push_pending_to_plex(log=log))
        else:
            log_autopilot.warning("Skipping the push to Plex - the Plex comparison didn't complete.")
        _progress(3)
    finally:
        push_error = stats.pop("push_error", None)
        if push_error:
            errors.append(push_error)
        _record_run("autopilot", started, stats, errors)
        _notify_run(stats, errors)
    return stats


def _reserve_rebuild_slot() -> float:
    """
    Reserves a cache-rebuild run if the cooldown since the last run has
    elapsed. Returns 0 (and reserves) if a run may start, otherwise the
    remaining wait time in seconds.
    """
    global _last_rebuild_started, _previous_rebuild_started
    with _rebuild_lock:
        elapsed = time.time() - _last_rebuild_started
        if elapsed < CACHE_REBUILD_COOLDOWN_SECONDS:
            return CACHE_REBUILD_COOLDOWN_SECONDS - elapsed
        _previous_rebuild_started = _last_rebuild_started
        _last_rebuild_started = time.time()
        return 0.0


def _release_rebuild_slot() -> None:
    """Undoes the last reservation - for a run that never got to request a
    single review page (see score_sync)."""
    global _last_rebuild_started
    with _rebuild_lock:
        _last_rebuild_started = _previous_rebuild_started
    log_score.info("Cooldown released - the sitemap fetch failed, so the next attempt may start right away.")


def _notify_run(stats: dict, errors: list) -> None:
    """Sends one notification for an autopilot run, if NOTIFY_URL is set and
    the run has something NOTIFY_ON asks for. Best-effort: never raises."""
    if not NOTIFY_TARGET:
        return
    try:
        changes = stats.get("score_changes") or []
        new_titles = stats.get("new_titles") or []
        report_errors = bool(errors) and "error" in NOTIFY_ON
        report_changes = bool(changes or new_titles) and "changes" in NOTIFY_ON
        if not (report_errors or report_changes):
            return

        blocks = []
        if report_errors:
            blocks.append("\n".join(f"{_error_summary(e)}\n{e['message']}" for e in errors))
        if report_changes:
            lines = []
            if new_titles:
                lines.append(t("notify.new_titles", titles=_join_capped(new_titles)))
            if changes:
                lines.append(t("notify.score_changes", changes=_join_capped(
                    [f"{c['title']} {c['old']} → {c['new']}" for c in changes])))
            blocks.append("\n".join(lines))

        notify.send(
            NOTIFY_TARGET,
            title=t("notify.title_error") if report_errors else t("notify.title_changes"),
            message="\n\n".join(blocks),
            high_priority=report_errors,
        )
    except Exception:
        log_notify.exception("Could not send notification")


def _join_capped(items: list, limit: int = 10) -> str:
    shown = ", ".join(items[:limit])
    if len(items) > limit:
        shown += " " + t("notify.more", count=len(items) - limit)
    return shown


# The time the autopilot loop is currently waiting for (local, tz-aware) -
# shown in the footer. Taken from the loop itself rather than recomputed, so
# the footer can never disagree with what actually happens.
_next_auto_run = None


def _auto_sync_loop():
    global _next_auto_run
    while True:
        now = datetime.datetime.now()
        next_run = croniter(AUTO_SYNC_CRON, now).get_next(datetime.datetime)
        _next_auto_run = next_run.astimezone()
        log_autopilot.debug("Next run: %s", _next_auto_run.isoformat(timespec="minutes"))
        sleep_seconds = (next_run - datetime.datetime.now()).total_seconds()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
        try:
            autonomous_sync()
        except Exception:
            # Stage failures are handled inside autonomous_sync - this only
            # catches the unforeseen, so the loop itself never dies
            log_autopilot.exception("Unexpected error outside of the sync stages")


def _log_startup() -> None:
    """A short summary of the effective configuration - the first thing to
    look at when something behaves unexpectedly. Never logs secrets."""
    tz_name = os.environ.get("TZ", "").strip()
    offset = datetime.datetime.now().astimezone().strftime("%z")
    offset = f"UTC{offset[:3]}:{offset[3:]}"
    log_config.info("Wokearr %s%s - language %s, log level %s, time zone %s (%s)",
                    APP_VERSION, f" (build {BUILD_DATE})" if BUILD_DATE else "", LANGUAGE,
                    logging.getLevelName(get_logger("config").getEffectiveLevel()), tz_name or "UTC", offset)
    if tz_name in ("", "UTC", "Etc/UTC"):
        # Compose passes Etc/UTC when TZ isn't set, so treat that as "unset"
        log_config.info("Log times and AUTO_SYNC_CRON use UTC. Set TZ (e.g. Europe/Berlin) to use your local time.")
    elif not _tz_is_known(tz_name):
        log_config.warning("TZ=%r is not a known time zone - falling back to UTC. Use a name like Europe/Berlin.",
                           tz_name)
    if demo_mode():
        log_config.info("Demo mode: PLEX_URL/PLEX_TOKEN not set.")
    else:
        log_config.info("Plex: %s, libraries: %s", urlsplit(PLEX_URL).netloc or PLEX_URL, ", ".join(LIBRARY_SECTIONS))
    if AUTO_SYNC_CRON:
        first_run = croniter(AUTO_SYNC_CRON, datetime.datetime.now()).get_next(datetime.datetime)
        log_config.info("Autopilot: %r, next run %s", AUTO_SYNC_CRON, first_run.strftime("%Y-%m-%d %H:%M"))
    else:
        log_config.info("Autopilot: disabled (AUTO_SYNC_CRON not set)")
    if NOTIFY_TARGET:
        log_config.info("Notifications: %s, on: %s", NOTIFY_TARGET.display, ", ".join(sorted(NOTIFY_ON)))
    else:
        log_config.info("Notifications: disabled (NOTIFY_URL not set)")
    auth.log_startup()


def _tz_is_known(name: str) -> bool:
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return True
    except Exception:
        return False


_log_startup()
if AUTO_SYNC_CRON:
    threading.Thread(target=_auto_sync_loop, daemon=True).start()


@app.route("/")
def index():
    return render_template(
        "index.html",
        demo=demo_mode(),
        badge_label_style=BADGE_LABEL_STYLE,
        language=LANGUAGE,
        i18n=TRANSLATIONS,
        bands=BAND_KEYS,
        band_styles=band_styles(),
        band_max=dict(zip(BAND_KEYS, BAND_MAX)),
        # Only forms has a session to end - basic can't log out, the browser keeps the login
        show_logout=auth.active and auth.method == "forms",
        auth_method=auth.method if auth.active else "none",
    )


@app.route("/favicon.ico")
def favicon():
    """Many clients (bookmarks, dashboards, link previews) ask for the icon at
    the root instead of reading the <link> tags."""
    return app.send_static_file("favicon.ico")


@app.route("/healthz")
def healthz():
    """Always reachable without login (Docker's health check). Reports a
    misconfigured login as unhealthy, so it shows up in Portainer instead of
    only as a locked page."""
    body = {
        "status": "ok",
        "demo_mode": demo_mode(),
        "version": APP_VERSION,
        "build_date": BUILD_DATE or None,
        "auth": auth.method if not auth.error else "misconfigured",
    }
    if auth.error:
        body["status"] = "error"
        return jsonify(body), 503
    return jsonify(body)


@app.route("/api/status")
def api_status():
    """Version/build info, the next autopilot run and the run protocol for
    the footer. Newest run first, so the frontend doesn't have to reverse
    the list."""
    return jsonify({
        "version": APP_VERSION,
        "build_date": BUILD_DATE or None,
        "version_url": VERSION_URL,
        "color_scheme": BADGE_COLOR_SCHEME,
        "next_run": _next_auto_run.isoformat() if AUTO_SYNC_CRON and _next_auto_run else None,
        "runs": list(reversed(load_runs())),
    })


def _release_date(item) -> str | None:
    """ISO date for sorting by release in the grid; Plex's exact date when it
    has one, else just the year."""
    released = getattr(item, "originallyAvailableAt", None)
    if released:
        try:
            return released.date().isoformat() if hasattr(released, "date") else str(released)[:10]
        except Exception:
            pass
    year = getattr(item, "year", None)
    return f"{year:04d}-01-01" if isinstance(year, int) else None


@app.route("/api/library")
def api_library():
    if demo_mode():
        return jsonify({"demo": True, "items": DEMO_ITEMS})

    cache = load_cache()
    plex = get_plex()
    items = []
    for rk, (it, prefix) in _current_library_items(plex).items():
        tmdb_id = tmdb_id_from_item(it)
        if not tmdb_id:
            continue
        entry = cache.get(f"{prefix}:{tmdb_id}")
        if not entry:
            continue
        items.append({
            "ratingKey": it.ratingKey,
            "title": it.title,
            # Plex's own sort title ("Matrix, The") - sorts the way Plex does
            "sortTitle": getattr(it, "titleSort", None) or it.title,
            "year": it.year,
            "released": _release_date(it),
            "score": entry["score"],
            "type": "movie" if prefix == "movie" else "show",
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


# Which logger a failed manual job is reported under, by stage
_STAGE_LOGGERS = {"score_sync": log_score, "sync": log_plex, "push": log_push, "cleanup": log_cleanup}


def _start_job(trigger: str, stage: str, work, total: int = 0, initial_log=None) -> str:
    """Runs work(job, job_log) -> stats in a background thread, the same way
    for every button: records the run in the protocol, and turns an
    exception into an error record - logged with its traceback in the
    container, summarized in the user's language in the browser."""
    job_id = str(uuid.uuid4())
    job = {"state": "running", "progress": [0, total], "log": list(initial_log or [])}
    JOBS[job_id] = job

    def job_log(msg):
        job["log"].append(msg)

    def run():
        started = _run_started(trigger)
        stats, errors = {}, []
        try:
            stats = work(job, job_log) or {}
            job["state"] = "done"
        except Exception as e:
            record = _error_record(stage, e)
            errors.append(record)
            _STAGE_LOGGERS.get(stage, log_run).exception("%s", _error_summary(record, t_log))
            job["state"] = "error"
            job["error"] = _error_summary(record)
            job_log(job["error"])
        finally:
            push_error = stats.pop("push_error", None)
            if push_error:
                errors.append(push_error)
            _record_run(trigger, started, stats, errors)

    threading.Thread(target=run, daemon=True).start()
    return job_id


@app.route("/api/rebuild-cache", methods=["POST"])
def api_rebuild_cache():
    payload = request.get_json(silent=True) or {}
    full = bool(payload.get("full"))

    # Reserved here rather than in score_sync, so a click during the cooldown
    # gets its answer right away instead of starting a job that does nothing
    wait = _reserve_rebuild_slot()
    if wait:
        return jsonify({"error": t("rebuild_cache.cooldown_error", seconds=int(wait) + 1)}), 429

    def work(job, job_log):
        return score_sync(
            log=job_log,
            full=full,
            progress=lambda done, total: job.update(progress=[done, total]),
            slot_reserved=True,
        )

    job_id = _start_job("score_sync_full" if full else "score_sync", "score_sync", work,
                        initial_log=[t("rebuild_cache.loading")])
    return jsonify({"job_id": job_id})


@app.route("/api/sync-library", methods=["POST"])
def api_sync_library():
    """Stage 2, manual: Plex comparison - fetch original posters, render
    branded versions, clean up removed titles. No push to Plex (see
    /api/apply for that)."""
    if demo_mode():
        return jsonify({"error": t("api.sync_library.demo_error")}), 400

    def work(job, job_log):
        stats = sync_library(log=job_log)
        job["missing_originals"] = stats.get("missing_originals", [])
        return stats

    return jsonify({"job_id": _start_job("sync", "sync", work)})


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

    def work(job, job_log):
        return push_pending_to_plex(
            log=job_log,
            progress=lambda done, total: job.update(progress=[done, total]),
            force=True,
            rating_keys=rating_keys,
        )

    return jsonify({"job_id": _start_job("push", "push", work, total=len(rating_keys))})


@app.route("/api/cleanup-posters", methods=["POST"])
def api_cleanup_posters():
    """Goes through the whole library once and removes old, self-uploaded
    poster versions from Plex (see cleanup_old_uploaded_posters). Always
    available regardless of CLEANUP_OLD_POSTERS, since it's explicitly triggered."""
    if demo_mode():
        return jsonify({"error": t("api.cleanup.demo_error")}), 400

    def work(job, job_log):
        plex = get_plex()
        items = []
        for it, _prefix in _current_library_items(plex).values():
            items.append(it)
            if getattr(it, "type", None) == "show":
                items.extend(_seasons_for_show(it))
        job["progress"] = [0, len(items)]
        log_cleanup.info("Checking %d items for old poster uploads.", len(items))

        progress_lock = threading.Lock()
        done = 0
        removed_total = 0

        def process(item):
            nonlocal done, removed_total
            try:
                removed = cleanup_old_uploaded_posters(plex, item)
            except Exception as e:
                removed = 0
                title = _display_title(item) if hasattr(item, "title") else "?"
                _emit(job_log, log_cleanup, "cleanup.item_error", level=logging.WARNING, title=title, error=_describe(e))
            with progress_lock:
                done += 1
                removed_total += removed
                job["progress"] = [done, len(items)]

        with ThreadPoolExecutor(max_workers=POSTER_WORKERS) as pool:
            futures = [pool.submit(process, item) for item in items]
            for f in as_completed(futures):
                pass

        _emit(job_log, log_cleanup, "cleanup.done", count=removed_total)
        return {"plex_posters_removed": removed_total}

    return jsonify({"job_id": _start_job("cleanup", "cleanup", work)})


@app.route("/api/job/<job_id>")
def api_job(job_id):
    return jsonify(JOBS.get(job_id, {"state": "unknown"}))


if __name__ == "__main__":
    # Only for local development outside of Docker - gunicorn runs in the container (see Dockerfile)
    log_config.info("Demo mode: %s", demo_mode())
    app.run(host="0.0.0.0", port=5005, debug=True)
