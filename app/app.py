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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_file, render_template
from PIL import Image

from badge import add_badge, BADGE_MARKER  # noqa: E402

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

# Anwenden/Aufraeumen lesen pro Titel mehrere Poster-Kandidaten von Plex herunter
# (um unseren Badge-Marker zu pruefen) - I/O-lastig, daher parallel wie beim
# Cache-Aufbau statt einen Titel nach dem anderen abzuarbeiten.
POSTER_WORKERS = 4

# Autopilot-Intervall (Minuten): Score-Sync, Original-Poster-Cache pflegen,
# entfernte Titel aufraeumen UND neue/geaenderte Titel automatisch badgen und
# nach Plex hochladen - alles in einem Takt. 0 = deaktiviert (Standard), dann
# nur ueber die Buttons in der UI. Fuer echten "faehrt von allein"-Betrieb z.B.
# auf 60 setzen. Ersetzt das fruehere CACHE_AUTO_REFRESH_MINUTES (nur Scores).
AUTO_SYNC_INTERVAL_MINUTES = int(os.environ.get("AUTO_SYNC_INTERVAL_MINUTES", "0") or "0")
# Mindestabstand zwischen zwei Sitemap-Abrufen (manuell oder automatisch), damit
# isitwokeornot.com nicht durch Spam-Klicks oder eine zu knappe Cron-Angabe
# ueberlastet wird.
CACHE_REBUILD_COOLDOWN_SECONDS = int(os.environ.get("CACHE_REBUILD_COOLDOWN_MINUTES", "5") or "5") * 60

DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = DATA_DIR / "score_cache.json"
# Original-Poster-Cache: die primaere Quelle fuer "Anwenden" (siehe
# fetch_original_poster_bytes) - an Plex' ratingKey gebunden, wird vom Autopilot
# fuer die ganze Bibliothek warmgehalten und um entfernte Titel bereinigt.
ORIGINALS_DIR = DATA_DIR / "originals"
ORIGINALS_DIR.mkdir(parents=True, exist_ok=True)
# Merkt sich pro ratingKey, mit welchem Score zuletzt gebadgt wurde - so erkennt
# der Autopilot, welche Titel neu sind oder sich im Score geaendert haben, ohne
# jedes Mal alles neu zu badgen.
APPLIED_STATE_FILE = DATA_DIR / "applied_state.json"
# ---------------------------------------------------------------------------

app = Flask(__name__)
JOBS = {}  # job_id -> {"state": "running"/"done"/"error", "progress": [n, total], "log": [...]}

_rebuild_lock = threading.Lock()
_last_rebuild_started = 0.0  # epoch seconds - schuetzt isitwokeornot.com vor zu haeufigen Abrufen
_applied_state_lock = threading.Lock()

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


def _is_own_badge(img_bytes: bytes | None) -> bool | None:
    """
    Erkennt am eingebrannten JPEG-Kommentar, ob dieses Bild von add_badge()
    erzeugt wurde (siehe badge.BADGE_MARKER) - zuverlaessiger als sich auf
    Plex/plexapi-Metadaten wie "provider" zu verlassen (die waren es nicht).

    True = eindeutig unser Marker, False = eindeutig kein Marker (sauber),
    None = nicht entscheidbar (z.B. abgebrochener/kaputter Download). Der
    None-Fall darf NIE wie False behandelt werden, sonst kann ein Download-
    Aussetzer bei einem eigenen, bereits bebadgten Poster dazu fuehren, dass
    es faelschlich als "sauberes Original" durchgeht und erneut bebadgt wird.
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


def fetch_original_poster_bytes(plex, item) -> tuple[bytes, bool]:
    """
    Liefert das unbebadgte Original-Poster. Liest primaer aus dem lokalen
    Cache (ORIGINALS_DIR) - dort landet nur, was zuvor eindeutig als "kein
    eigener Badge" verifiziert wurde (siehe _is_own_badge), das Vertrauen ist
    also gerechtfertigt und ein erneuter Live-Check bei Plex nicht noetig.

    Nur wenn fuer diesen Titel noch nichts gecacht ist (z.B. neu in Plex),
    wird live bei Plex nachgeschaut: alle Poster-Kandidaten durchgehen und den
    ersten nehmen, der eindeutig NICHT unseren Marker traegt. Kandidaten, bei
    denen sich das nicht sicher entscheiden laesst (kaputter Download), werden
    uebersprungen statt riskiert - siehe _is_own_badge.

    Findet sich gar kein verwertbarer Kandidat, wird als letzter Ausweg das
    aktuell ausgewaehlte Poster verwendet (kann theoretisch schon bebadgt
    sein). Gibt (bild_bytes, original_gefunden) zurueck.
    """
    cached = ORIGINALS_DIR / f"{item.ratingKey}.jpg"
    if cached.exists():
        return cached.read_bytes(), True

    try:
        for p in item.posters():
            data = _poster_candidate_bytes(plex, p)
            if _is_own_badge(data) is not False:
                continue
            cached.write_bytes(data)
            return data, True
    except Exception:
        pass

    poster_url = plex.url(item.thumb, includeToken=True)
    return requests.get(poster_url, timeout=20).content, False


def cleanup_old_uploaded_posters(plex, item) -> int:
    """
    Loescht (best effort) aeltere Poster-Versionen dieses Plex-Items, die
    unseren eigenen Badge-Marker tragen (siehe _is_own_badge) - alles ausser
    der aktuell ausgewaehlten. Ein Loeschversuch auf einen Agenten-Poster
    (z.B. TMDb) kommt dank der Marker-Pruefung erst gar nicht vor; wuerde er
    doch versucht, schlaegt er bei Plex einfach folgenlos fehl.
    """
    removed = 0
    try:
        for p in item.posters():
            if getattr(p, "selected", False):
                continue
            data = _poster_candidate_bytes(plex, p)
            if _is_own_badge(data) is not True:
                continue
            try:
                p.delete()
                removed += 1
            except Exception:
                pass
    except Exception:
        pass
    return removed


def _load_applied_state() -> dict:
    if not APPLIED_STATE_FILE.exists():
        return {}
    try:
        return json.loads(APPLIED_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _record_applied_score(rating_key, score) -> None:
    """Merkt sich, mit welchem Score dieser Titel zuletzt gebadgt wurde -
    Grundlage dafuer, dass der Autopilot nur neue/geaenderte Titel anfasst."""
    with _applied_state_lock:
        state = _load_applied_state()
        state[str(rating_key)] = score
        APPLIED_STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def apply_badge_to_item(plex, item, entry: dict) -> str:
    """
    Brennt den Badge fuer 'entry' (Cache-Eintrag mit u.a. "score") auf das
    Original-Poster von 'item' und laedt das Ergebnis nach Plex hoch. Raeumt
    danach (falls aktiviert) alte eigene Uploads auf und merkt sich den
    angewendeten Score (siehe _record_applied_score). Von manuellem "Anwenden"
    und vom Autopilot (autonomous_sync) gemeinsam genutzt.

    Gibt eine Log-Zeile zurueck; enthaelt "kein TMDb-Original in Plex gefunden",
    falls kein sauberer Original-Kandidat gefunden wurde (Warnsignal fuer evtl.
    weiterhin doppelten Badge).
    """
    import tempfile

    img_bytes, found_original = fetch_original_poster_bytes(plex, item)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src.jpg"
        dst = Path(tmp) / "dst.jpg"
        src.write_bytes(img_bytes)
        add_badge(str(src), entry["score"], str(dst), position=BADGE_POSITION, label_style=BADGE_LABEL_STYLE)
        item.uploadPoster(filepath=str(dst))
        if CLEANUP_OLD_POSTERS:
            cleanup_old_uploaded_posters(plex, item)

    _record_applied_score(item.ratingKey, entry["score"])

    if found_original:
        return f"OK: {item.title}"
    return (
        f"OK (mit Warnung): {item.title} - kein TMDb-Original in Plex gefunden, "
        f"aktuelles Poster wurde als Basis genutzt (evtl. weiterhin doppelter Badge). "
        f"In Plex 'Metadaten aktualisieren' auf den Titel anwenden und danach erneut anwenden."
    )


def _current_library_items(plex) -> dict:
    """Liefert {ratingKey: (plex_item, cache_prefix)} fuer alle konfigurierten
    Bibliotheken - Grundlage fuer den Plex-Abgleich im Autopilot."""
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


def autonomous_sync(log=None, progress=None) -> None:
    """
    Ein kompletter Autopilot-Durchlauf, in dieser Reihenfolge:
      1. Score-Sync (inkrementell) bei isitwokeornot.com.
      2. Plex-Abgleich: fuer Titel mit Score, die noch kein lokales Original
         haben, das saubere Original von Plex holen und cachen.
      3. Leichen entfernen: lokale Original-Cache-Dateien fuer Titel loeschen,
         die nicht mehr in der (in Schritt 2 ohnehin abgefragten) Plex-
         Bibliothek vorkommen - kein zusaetzlicher Plex-Request noetig.
      4. Neue oder im Score geaenderte Titel automatisch badgen und nach Plex
         hochladen (erkannt ueber APPLIED_STATE_FILE).

    log(text) und progress(stufe, gesamt) sind optionale Callbacks fuer die
    manuelle "Jetzt synchronisieren"-UI; der Cron-Loop ruft ohne sie auf.
    """
    def _log(msg):
        print(f"[auto-sync] {msg}", flush=True)
        if log:
            log(msg)

    def _progress(step, total=4):
        if progress:
            progress(step, total)

    # Stufe 1: Score-Sync
    wait = _reserve_rebuild_slot()
    if wait:
        _log(f"Score-Sync uebersprungen (Cooldown, noch {int(wait)}s aktiv).")
    else:
        import build_score_cache as bsc
        cache = load_cache()

        def on_progress(done, total):
            if done and done % 200 == 0:
                CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")

        cache, processed = bsc.build_cache(cache, on_progress=on_progress)
        CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
        _log(f"Score-Sync: {processed} neue Titel, {len(cache)} insgesamt im Cache.")
    _progress(1)

    if demo_mode():
        _log("Demo-Modus: kein Plex konfiguriert, Stufen 2-4 uebersprungen.")
        _progress(4)
        return

    cache = load_cache()
    plex = get_plex()
    library = _current_library_items(plex)
    valid_keys = set(library.keys())

    # Stufe 2: fehlende Original-Poster nachladen
    to_warm = []
    for rk, (item, prefix) in library.items():
        tmdb_id = tmdb_id_from_item(item)
        entry = cache.get(f"{prefix}:{tmdb_id}") if tmdb_id else None
        if entry and not (ORIGINALS_DIR / f"{rk}.jpg").exists():
            to_warm.append(item)
    if to_warm:
        with ThreadPoolExecutor(max_workers=POSTER_WORKERS) as pool:
            futures = [pool.submit(fetch_original_poster_bytes, plex, it) for it in to_warm]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as e:
                    _log(f"Fehler beim Original-Poster holen: {e}")
        _log(f"Poster-Cache: {len(to_warm)} neue Original(e) geholt.")
    _progress(2)

    # Stufe 3: Leichen entfernen (Titel nicht mehr in Plex)
    removed = 0
    for f in ORIGINALS_DIR.glob("*.jpg"):
        if f.stem not in valid_keys:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        _log(f"Leichen entfernt: {removed} Original(e) fuer nicht mehr vorhandene Titel.")
    _progress(3)

    # Stufe 4: neue/geaenderte Titel automatisch anwenden
    applied_state = _load_applied_state()
    to_apply = []
    for rk, (item, prefix) in library.items():
        tmdb_id = tmdb_id_from_item(item)
        entry = cache.get(f"{prefix}:{tmdb_id}") if tmdb_id else None
        if entry and applied_state.get(rk) != entry["score"]:
            to_apply.append((item, entry))
    if to_apply:
        _log(f"Autopilot wendet {len(to_apply)} neue/geaenderte Titel an...")
        with ThreadPoolExecutor(max_workers=POSTER_WORKERS) as pool:
            futures = [pool.submit(apply_badge_to_item, plex, it, entry) for it, entry in to_apply]
            for f in as_completed(futures):
                try:
                    _log(f.result())
                except Exception as e:
                    _log(f"Fehler beim automatischen Anwenden: {e}")
    else:
        _log("Autopilot: keine neuen/geaenderten Titel.")
    _progress(4)


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


def _auto_sync_loop():
    interval = AUTO_SYNC_INTERVAL_MINUTES * 60
    while True:
        time.sleep(interval)
        try:
            autonomous_sync()
        except Exception as e:
            print(f"[auto-sync] Unerwarteter Fehler: {e}", flush=True)


if AUTO_SYNC_INTERVAL_MINUTES > 0:
    threading.Thread(target=_auto_sync_loop, daemon=True).start()


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

        progress_lock = threading.Lock()
        done = 0

        def process(rk):
            nonlocal done
            try:
                item = plex.fetchItem(int(rk))
                tmdb_id = tmdb_id_from_item(item)
                prefix = PLEX_TYPE_TO_CACHE_PREFIX.get(item.type)
                entry = cache.get(f"{prefix}:{tmdb_id}") if prefix else None
                if not entry:
                    return
                msg = apply_badge_to_item(plex, item, entry)
                JOBS[job_id]["log"].append(msg)
                if "kein TMDb-Original" in msg:
                    print(f"[apply] {msg}", flush=True)
            except Exception as e:
                JOBS[job_id]["log"].append(f"Fehler bei {rk}: {e}")
            finally:
                with progress_lock:
                    done += 1
                    JOBS[job_id]["progress"] = [done, len(rating_keys)]

        with ThreadPoolExecutor(max_workers=POSTER_WORKERS) as pool:
            futures = [pool.submit(process, rk) for rk in rating_keys]
            for f in as_completed(futures):
                pass
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

            progress_lock = threading.Lock()
            done = 0
            removed_total = 0

            def process(item):
                nonlocal done, removed_total
                try:
                    removed = cleanup_old_uploaded_posters(plex, item)
                except Exception as e:
                    removed = 0
                    JOBS[job_id]["log"].append(f"Fehler bei {getattr(item, 'title', '?')}: {e}")
                with progress_lock:
                    done += 1
                    removed_total += removed
                    JOBS[job_id]["progress"] = [done, len(items)]

            with ThreadPoolExecutor(max_workers=POSTER_WORKERS) as pool:
                futures = [pool.submit(process, item) for item in items]
                for f in as_completed(futures):
                    pass

            JOBS[job_id]["state"] = "done"
            JOBS[job_id]["log"].append(f"Fertig: {removed_total} alte Poster-Versionen in Plex entfernt.")
        except Exception as e:
            JOBS[job_id]["state"] = "error"
            JOBS[job_id]["log"].append(str(e))

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/auto-sync", methods=["POST"])
def api_auto_sync():
    """Stoesst denselben Autopilot-Durchlauf (autonomous_sync) sofort manuell
    an, den auch AUTO_SYNC_INTERVAL_MINUTES im Hintergrund ausfuehrt - zum
    Testen/Erzwingen, ohne auf den naechsten Cron-Tick warten zu muessen."""
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"state": "running", "progress": [0, 4], "log": []}

    def run():
        try:
            autonomous_sync(
                log=lambda msg: JOBS[job_id]["log"].append(msg),
                progress=lambda step, total: JOBS[job_id].update(progress=[step, total]),
            )
            JOBS[job_id]["state"] = "done"
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
