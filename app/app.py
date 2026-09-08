#!/usr/bin/env python3
"""
Wokearr - kleine lokale Web-UI (Radarr/Sonarr-Stil) fuer Ampel-Badges auf
Plex-Postern, Score-Quelle: isitwokeornot.com

Konfiguration erfolgt ausschliesslich ueber Umgebungsvariablen (siehe .env.example),
damit das Image ohne Aenderungen am Code auf GitHub/Docker Hub veroeffentlicht
werden kann.
"""
import io
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_file, render_template

from badge import add_badge  # noqa: E402

# ---------------------------------------------------------------------------
# CONFIG - kommt aus Umgebungsvariablen (siehe .env.example / docker-compose.yaml)
# ---------------------------------------------------------------------------
PLEX_URL = os.environ.get("PLEX_URL", "").rstrip("/")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
LIBRARY_SECTIONS = [s.strip() for s in os.environ.get("LIBRARY_SECTIONS", "Filme,Serien").split(",") if s.strip()]
BADGE_POSITION = os.environ.get("BADGE_POSITION", "top-right")
BADGE_LABEL_STYLE = os.environ.get("BADGE_LABEL_STYLE", "percent")
if BADGE_LABEL_STYLE not in ("percent", "woke"):
    BADGE_LABEL_STYLE = "percent"

# Plex behaelt bei jedem uploadPoster() die vorherige Version als Poster-Historie
# und loescht sie nie von selbst - laesst den Plex-Server sonst zuwachsen. Nach
# jedem Anwenden werden deshalb standardmaessig aeltere, selbst hochgeladene
# Versionen entfernt (Original-/Agent-Poster wie TMDb bleiben unangetastet).
CLEANUP_OLD_POSTERS = os.environ.get("CLEANUP_OLD_POSTERS", "true").strip().lower() not in ("false", "0", "no")

# Wie oft (Minuten) die Sitemap automatisch im Hintergrund auf neue/fehlende Titel
# geprueft wird. 0 = deaktiviert (Standard) - dann nur ueber die Buttons in der UI.
CACHE_AUTO_REFRESH_MINUTES = int(os.environ.get("CACHE_AUTO_REFRESH_MINUTES", "0") or "0")
# Mindestabstand zwischen zwei Sitemap-Abrufen (manuell oder automatisch), damit
# isitwokeornot.com nicht durch Spam-Klicks oder eine zu knappe Cron-Angabe
# ueberlastet wird.
CACHE_REBUILD_COOLDOWN_SECONDS = int(os.environ.get("CACHE_REBUILD_COOLDOWN_MINUTES", "5") or "5") * 60

DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = DATA_DIR / "score_cache.json"
# ---------------------------------------------------------------------------

app = Flask(__name__)
JOBS = {}  # job_id -> {"state": "running"/"done"/"error", "progress": [n, total], "log": [...]}

_rebuild_lock = threading.Lock()
_last_rebuild_started = 0.0  # epoch seconds - schuetzt isitwokeornot.com vor zu haeufigen Abrufen

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


def fetch_original_poster_bytes(plex, item) -> tuple[bytes, bool]:
    """
    Holt das unbebadgte Original-Poster direkt von Plex' Agenten-Kandidaten
    (z.B. TMDb) - NICHT das aktuell ausgewaehlte Poster, das ja unser eigener,
    bereits bebadgter Upload sein kann. Plex behaelt Agenten-Poster als
    Kandidaten dauerhaft (auch wenn ein eigener Upload ausgewaehlt ist), daher
    ist das normalerweise immer die zuverlaessige Quelle fuer "das Original".

    Gibt (bild_bytes, agent_poster_gefunden) zurueck. Faellt auf das aktuell
    ausgewaehlte Poster zurueck, falls kein Agenten-Kandidat gefunden wird
    (z.B. wenn Plex fuer den Titel keine TMDb-Metadaten mehr hat) - dabei kann
    theoretisch ein bereits bebadgtes Poster erneut bebadgt werden. In diesem
    Fall hilft ein "Metadaten aktualisieren" auf den Titel in Plex, damit
    Plex den Original-Kandidaten neu laedt.
    """
    try:
        for p in item.posters():
            provider = (getattr(p, "provider", None) or "").lower()
            key = getattr(p, "key", "") or ""
            is_upload = provider in ("local", "upload") or key.startswith("upload://") or key.startswith("/upload")
            if is_upload or not key:
                continue
            url = key if key.startswith("http") else plex.url(key, includeToken=True)
            return requests.get(url, timeout=20).content, True
    except Exception:
        pass
    poster_url = plex.url(item.thumb, includeToken=True)
    return requests.get(poster_url, timeout=20).content, False


def cleanup_old_uploaded_posters(item) -> int:
    """
    Loescht (best effort) aeltere, selbst hochgeladene Poster-Versionen dieses
    Plex-Items - alles ausser der aktuell ausgewaehlten. Agenten-Poster (TMDb
    usw.) lassen sich ueber die Plex-API ohnehin nicht loeschen, nur abwaehlen;
    ein Loeschversuch dort schlaegt einfach folgenlos fehl. Die aktuell
    ausgewaehlte Version wird nie angefasst.
    """
    removed = 0
    try:
        for p in item.posters():
            if getattr(p, "selected", False):
                continue
            provider = (getattr(p, "provider", None) or "").lower()
            key = getattr(p, "key", "") or ""
            is_upload = provider in ("local", "upload") or key.startswith("upload://") or key.startswith("/upload")
            if not is_upload:
                continue
            try:
                p.delete()
                removed += 1
            except Exception:
                pass
    except Exception:
        pass
    return removed


def _reserve_rebuild_slot() -> float:
    """
    Reserviert einen Cache-Rebuild-Lauf, falls der Cooldown seit dem letzten Lauf
    abgelaufen ist. Gibt 0 zurueck (und reserviert), wenn ein Lauf starten darf,
    sonst die verbleibende Wartezeit in Sekunden.
    """
    global _last_rebuild_started
    with _rebuild_lock:
        elapsed = time.time() - _last_rebuild_started
        if elapsed < CACHE_REBUILD_COOLDOWN_SECONDS:
            return CACHE_REBUILD_COOLDOWN_SECONDS - elapsed
        _last_rebuild_started = time.time()
        return 0.0


def _auto_refresh_loop():
    interval = CACHE_AUTO_REFRESH_MINUTES * 60
    while True:
        time.sleep(interval)
        if _reserve_rebuild_slot():
            continue  # Cooldown noch aktiv (z.B. gerade erst manuell aktualisiert) - naechster Tick
        try:
            import build_score_cache as bsc
            cache = load_cache()

            def on_progress(done, total):
                if done and done % 200 == 0:
                    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")

            cache, processed = bsc.build_cache(cache, on_progress=on_progress)
            CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"[auto-refresh] {processed} neue Titel verarbeitet, {len(cache)} insgesamt im Cache.", flush=True)
        except Exception as e:
            print(f"[auto-refresh] Fehler: {e}", flush=True)


if CACHE_AUTO_REFRESH_MINUTES > 0:
    threading.Thread(target=_auto_refresh_loop, daemon=True).start()


@app.route("/")
def index():
    return render_template("index.html", demo=demo_mode(), badge_label_style=BADGE_LABEL_STYLE)


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
                "sourceUrl": entry.get("url"),
            })
    return jsonify({"demo": False, "items": items})


@app.route("/api/poster/<rating_key>")
def api_poster(rating_key):
    """Proxied Poster - haelt den Plex-Token aus dem Browser raus."""
    demo = next((d for d in DEMO_ITEMS if d["ratingKey"] == rating_key), None)
    if demo:
        img = requests.get(demo["poster"], timeout=15).content
        return send_file(io.BytesIO(img), mimetype="image/jpeg")

    plex = get_plex()
    item = plex.fetchItem(int(rating_key))
    poster_url = plex.url(item.thumb, includeToken=True)
    img = requests.get(poster_url, timeout=15).content
    return send_file(io.BytesIO(img), mimetype="image/jpeg")


@app.route("/api/rebuild-cache", methods=["POST"])
def api_rebuild_cache():
    payload = request.get_json(silent=True) or {}
    full = bool(payload.get("full"))

    wait = _reserve_rebuild_slot()
    if wait:
        return jsonify({
            "error": f"Bitte kurz warten: naechster Sitemap-Abruf erst in {int(wait) + 1}s moeglich (Cooldown "
                     f"schuetzt isitwokeornot.com vor zu haeufigen Anfragen)."
        }), 429

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"state": "running", "progress": [0, 0], "log": ["Sitemap wird geladen..."]}

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
            JOBS[job_id]["log"].append(f"Fertig: {processed} Titel verarbeitet, {len(cache)} insgesamt im Cache.")
        except Exception as e:
            JOBS[job_id]["state"] = "error"
            JOBS[job_id]["log"].append(str(e))

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/apply", methods=["POST"])
def api_apply():
    if demo_mode():
        return jsonify({"error": "Demo-Modus: PLEX_URL/PLEX_TOKEN als Umgebungsvariablen setzen, um wirklich anzuwenden."}), 400

    payload = request.get_json(force=True)
    rating_keys = payload.get("ratingKeys", [])
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"state": "running", "progress": [0, len(rating_keys)], "log": []}

    def run():
        cache = load_cache()
        plex = get_plex()
        import tempfile
        for i, rk in enumerate(rating_keys, 1):
            try:
                item = plex.fetchItem(int(rk))
                tmdb_id = tmdb_id_from_item(item)
                prefix = PLEX_TYPE_TO_CACHE_PREFIX.get(item.type)
                entry = cache.get(f"{prefix}:{tmdb_id}") if prefix else None
                if not entry:
                    continue

                # Immer das unbebadgte Original von Plex' Agenten-Kandidaten holen
                # (nie das aktuell ausgewaehlte Poster - das kann unser eigener,
                # bereits bebadgter Upload sein) - verhindert doppelte Badges.
                img_bytes, found_original = fetch_original_poster_bytes(plex, item)

                with tempfile.TemporaryDirectory() as tmp:
                    src = Path(tmp) / "src.jpg"
                    dst = Path(tmp) / "dst.jpg"
                    src.write_bytes(img_bytes)
                    add_badge(str(src), entry["score"], str(dst), position=BADGE_POSITION, label_style=BADGE_LABEL_STYLE)
                    item.uploadPoster(filepath=str(dst))
                    if CLEANUP_OLD_POSTERS:
                        cleanup_old_uploaded_posters(item)
                if found_original:
                    JOBS[job_id]["log"].append(f"OK: {item.title}")
                else:
                    msg = (
                        f"OK (mit Warnung): {item.title} - kein TMDb-Original in Plex gefunden, "
                        f"aktuelles Poster wurde als Basis genutzt (evtl. weiterhin doppelter Badge). "
                        f"In Plex 'Metadaten aktualisieren' auf den Titel anwenden und danach erneut "
                        f"'Anwenden' klicken."
                    )
                    JOBS[job_id]["log"].append(msg)
                    print(f"[apply] {msg}", flush=True)
            except Exception as e:
                JOBS[job_id]["log"].append(f"Fehler bei {rk}: {e}")
            JOBS[job_id]["progress"] = [i, len(rating_keys)]
        JOBS[job_id]["state"] = "done"

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/cleanup-posters", methods=["POST"])
def api_cleanup_posters():
    """Geht einmalig die ganze Bibliothek durch und entfernt alte, selbst
    hochgeladene Poster-Versionen aus Plex (siehe cleanup_old_uploaded_posters).
    Unabhaengig von CLEANUP_OLD_POSTERS immer verfuegbar, da explizit ausgeloest."""
    if demo_mode():
        return jsonify({"error": "Demo-Modus: keine Plex-Bibliothek zum Aufraeumen."}), 400

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
            removed_total = 0
            for i, item in enumerate(items, 1):
                try:
                    removed_total += cleanup_old_uploaded_posters(item)
                except Exception as e:
                    JOBS[job_id]["log"].append(f"Fehler bei {getattr(item, 'title', '?')}: {e}")
                JOBS[job_id]["progress"] = [i, len(items)]
            JOBS[job_id]["state"] = "done"
            JOBS[job_id]["log"].append(f"Fertig: {removed_total} alte Poster-Versionen in Plex entfernt.")
        except Exception as e:
            JOBS[job_id]["state"] = "error"
            JOBS[job_id]["log"].append(str(e))

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/job/<job_id>")
def api_job(job_id):
    return jsonify(JOBS.get(job_id, {"state": "unknown"}))


if __name__ == "__main__":
    # Nur fuer lokale Entwicklung ausserhalb von Docker - im Container laeuft gunicorn (siehe Dockerfile)
    print("Demo-Modus:", demo_mode())
    app.run(host="0.0.0.0", port=5005, debug=True)
