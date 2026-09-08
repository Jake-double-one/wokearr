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

# Breite der Badge relativ zur Posterbreite, in Prozent. Mindestbreite fest bei
# 20% verankert, sonst wird die Badge auf kleineren Postern schnell unleserlich.
BADGE_WIDTH_MIN_PERCENT = 20.0
try:
    BADGE_WIDTH_PERCENT = float(os.environ.get("BADGE_WIDTH_PERCENT", "20"))
except ValueError:
    BADGE_WIDTH_PERCENT = BADGE_WIDTH_MIN_PERCENT
BADGE_WIDTH_PERCENT = max(BADGE_WIDTH_PERCENT, BADGE_WIDTH_MIN_PERCENT)

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
# Original-Poster-Cache: unbebadgt, direkt von Plex' Agenten-Kandidaten geholt.
# An Plex' ratingKey gebunden, wird vom Autopilot fuer die ganze Bibliothek
# warmgehalten und um entfernte Titel bereinigt.
ORIGINALS_DIR = DATA_DIR / "originals"
ORIGINALS_DIR.mkdir(parents=True, exist_ok=True)
# Gebrandete Poster: Score bereits reingebrannt, aber noch nicht zu Plex
# hochgeladen - eigener Schritt, damit man sich das Ergebnis vor dem Push
# lokal ansehen kann (siehe render_branded_image/push_to_plex).
BRANDED_DIR = DATA_DIR / "branded"
BRANDED_DIR.mkdir(parents=True, exist_ok=True)
# Merkt sich pro ratingKey, mit welchem Score zuletzt gerendert bzw. zu Plex
# hochgeladen wurde - so erkennt der Autopilot neue/geaenderte Titel, ohne
# jedes Mal alles neu zu rendern/hochzuladen.
RENDERED_STATE_FILE = DATA_DIR / "rendered_state.json"
PUSHED_STATE_FILE = DATA_DIR / "pushed_state.json"
# ---------------------------------------------------------------------------

app = Flask(__name__)
JOBS = {}  # job_id -> {"state": "running"/"done"/"error", "progress": [n, total], "log": [...]}

_rebuild_lock = threading.Lock()
_last_rebuild_started = 0.0  # epoch seconds - schuetzt isitwokeornot.com vor zu haeufigen Abrufen
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


def _is_upload_poster(p) -> bool:
    """
    True, wenn dieser Plex-Poster-Kandidat ein manueller Upload ist (unserer
    oder ein fremder/frueherer, auch aus der Zeit vor dem Badge-Marker) -
    erkannt an Plex' eigenem Key-Schema ("upload://posters/..."). Alles
    andere (direkte TMDb-/Fanart-/TheTVDB-/Amazon-/Gracenote-URLs, Plex'
    interne Agenten-Referenz "metadata://posters/...") ist ein
    Agenten-/Original-Poster.

    Zuverlässiger als der Badge-Marker allein: der Marker erkennt nur Poster,
    die WIR seit seiner Einfuehrung selbst erzeugt haben, nicht Uploads von
    davor. Das Key-Schema ist dagegen unabhaengig vom Alter des Uploads.
    """
    key = getattr(p, "key", "") or ""
    return "upload://posters" in key or "upload%3A%2F%2Fposters" in key


def fetch_original_poster_bytes(plex, item) -> tuple[bytes, bool]:
    """
    Liefert das unbebadgte Original-Poster. Liest primaer aus dem lokalen
    Cache (ORIGINALS_DIR) - dort landet nur, was zuvor eindeutig als
    Agenten-Original verifiziert wurde, das Vertrauen ist also gerechtfertigt
    und ein erneuter Live-Check bei Plex nicht noetig.

    Nur wenn fuer diesen Titel noch nichts gecacht ist (z.B. neu in Plex),
    wird live bei Plex nachgeschaut: alle Poster-Kandidaten durchgehen,
    Uploads (siehe _is_upload_poster) grundsaetzlich ignorieren - auch
    unmarkierte aus der Zeit vor dem Badge-Marker, die sich sonst faelschlich
    als "Original" haetten durchschmuggeln koennen - und den ersten
    verbleibenden (Agenten-)Kandidaten nehmen. Der Badge-Marker dient hier nur
    noch als zusaetzliche Sicherheitspruefung auf den gewaehlten Kandidaten.

    Findet sich gar kein verwertbarer Kandidat, wird als letzter Ausweg das
    aktuell ausgewaehlte Poster verwendet (kann theoretisch schon bebadgt
    sein). Gibt (bild_bytes, original_gefunden) zurueck.
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
    Loescht (best effort) aeltere, manuell hochgeladene Poster-Versionen
    dieses Plex-Items (siehe _is_upload_poster) - alles ausser der aktuell
    ausgewaehlten. Erfasst damit auch Uploads von vor der Einfuehrung des
    Badge-Markers. Ein Loeschversuch auf einen Agenten-Poster (z.B. TMDb)
    kommt dank der Key-Pruefung erst gar nicht vor.

    Loggt jeden Kandidaten samt Entscheidung/Ergebnis nach stdout (sichtbar in
    den Container-Logs).
    """
    removed = 0
    try:
        candidates = list(item.posters())
    except Exception as e:
        print(f"[cleanup] {item.title}: item.posters() fehlgeschlagen: {e}", flush=True)
        return 0

    for p in candidates:
        key = getattr(p, "key", "?")
        if getattr(p, "selected", False):
            print(f"[cleanup] {item.title}: uebersprungen (aktuell ausgewaehlt) - {key}", flush=True)
            continue
        if not _is_upload_poster(p):
            print(f"[cleanup] {item.title}: uebersprungen (Agenten-Poster) - {key}", flush=True)
            continue
        try:
            p.delete()
            removed += 1
            print(f"[cleanup] {item.title}: geloescht - {key}", flush=True)
        except Exception as e:
            print(f"[cleanup] {item.title}: Loeschen fehlgeschlagen ({e}) - {key}", flush=True)
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
    Brennt den Score aus 'entry' auf das lokal gecachte Original-Poster von
    'item' (siehe ORIGINALS_DIR) und speichert das Ergebnis unter
    BRANDED_DIR/<ratingKey>.jpg - reine lokale Datei-Operation, kein
    Plex-Kontakt. Wer nachsehen will, ob ein Badge richtig aussieht, kann
    diese Datei direkt im Docker-Volume oeffnen, bevor irgendwas bei Plex
    landet.

    Ueberspringt das Rendern, wenn schon mit demselben Score gerendert wurde
    (RENDERED_STATE_FILE), ausser force=True. Braucht ein bereits gecachtes
    Original (siehe fetch_original_poster_bytes) - wirft sonst FileNotFoundError.
    """
    rk = str(item.ratingKey)
    branded_path = BRANDED_DIR / f"{rk}.jpg"
    rendered_state = _load_state_file(RENDERED_STATE_FILE)
    if not force and branded_path.exists() and rendered_state.get(rk) == entry["score"]:
        return branded_path

    original_path = ORIGINALS_DIR / f"{rk}.jpg"
    if not original_path.exists():
        raise FileNotFoundError(f"Kein Original-Poster fuer '{item.title}' im Cache - erst synchronisieren.")

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
    Laedt das lokal gebrannte Poster (siehe render_branded_image, wird bei
    Bedarf automatisch nachgerendert) zu Plex hoch und raeumt danach (falls
    aktiviert) alte eigene Uploads auf. Merkt sich den hochgeladenen Score
    (PUSHED_STATE_FILE). Von "Auf Plex uebertragen" und vom Autopilot
    gemeinsam genutzt.
    """
    branded_path = render_branded_image(item, entry, force=force)
    item.uploadPoster(filepath=str(branded_path))
    if CLEANUP_OLD_POSTERS:
        cleanup_old_uploaded_posters(plex, item)
    _record_state(PUSHED_STATE_FILE, item.ratingKey, entry["score"])
    return f"OK: {item.title}"


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


def _emit(log, msg, prefix="sync"):
    print(f"[{prefix}] {msg}", flush=True)
    if log:
        log(msg)


def score_sync(log=None) -> None:
    """Stufe 1: inkrementeller Score-Sync bei isitwokeornot.com. Eigenstaendig
    per Button "Score-Datenbank aktualisieren" oder Teil des Autopiloten."""
    wait = _reserve_rebuild_slot()
    if wait:
        _emit(log, f"Score-Sync uebersprungen (Cooldown, noch {int(wait)}s aktiv).")
        return
    import build_score_cache as bsc
    cache = load_cache()

    def on_progress(done, total):
        if done and done % 200 == 0:
            CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")

    cache, processed = bsc.build_cache(cache, on_progress=on_progress)
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    _emit(log, f"Score-Sync: {processed} neue Titel, {len(cache)} insgesamt im Cache.")


def sync_library(log=None) -> None:
    """
    Stufe 2 - Plex-Abgleich, ohne irgendetwas zu Plex hochzuladen:
      - fehlende Original-Poster fuer Titel mit Score nachladen (ORIGINALS_DIR)
      - daraus die gebrandete Version rendern, sofern noch nicht mit dem
        aktuellen Score geschehen (BRANDED_DIR, siehe render_branded_image)
      - lokale Original-/Branded-Dateien fuer Titel loeschen, die nicht mehr
        in der (hier ohnehin abgefragten) Plex-Bibliothek stehen ("Leichen")
    """
    if demo_mode():
        _emit(log, "Demo-Modus: kein Plex konfiguriert.")
        return

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

    # Original-Poster nachladen, wo noch keins gecacht ist
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
                    _emit(log, f"Fehler beim Original-Poster holen ({item.title}): {e}")
        _emit(log, f"Original-Poster: {len(to_warm)} neue geholt.")
    if warnings:
        _emit(log, "Warnung: kein TMDb-Original in Plex gefunden fuer: " + ", ".join(warnings) +
              " - aktuelles Poster wurde als Basis genutzt (evtl. doppelter Badge). "
              "Fix: in Plex 'Metadaten aktualisieren', danach erneut synchronisieren.")

    # Gebrandete Version fuer neue/geaenderte Titel rendern
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
            _emit(log, f"Fehler beim Rendern ({item.title}): {e}")
    if rendered:
        _emit(log, f"Gebrandete Poster gerendert: {rendered}.")

    # Leichen entfernen (Titel nicht mehr in Plex) - in beiden lokalen Ordnern
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
        _emit(log, f"Leichen entfernt: {removed} Datei(en) fuer nicht mehr vorhandene Titel.")

    if not to_warm and not to_render and not removed:
        _emit(log, "Synchronisiert: nichts Neues.")


def push_pending_to_plex(log=None, progress=None, force: bool = False, rating_keys=None) -> None:
    """
    Stufe 3 - laedt Poster zu Plex hoch. Ohne rating_keys: die ganze
    Bibliothek, aber standardmaessig (force=False) nur Titel, die neu sind
    oder deren Score sich seit dem letzten Push geaendert hat
    (PUSHED_STATE_FILE) - so macht der Autopilot bei unveraendertem Zustand
    nichts. Mit rating_keys: nur diese Titel; force=True (z.B. Klick auf
    "Auf Plex uebertragen") laedt sie in jedem Fall neu hoch, unabhaengig vom
    zuletzt gepushten Score - z.B. um nach einer geaenderten Badge-Einstellung
    alles neu zu erzwingen. progress(done, total) ist ein optionaler Callback
    fuer eine Fortschrittsanzeige in der UI.
    """
    if demo_mode():
        _emit(log, "Demo-Modus: kein Plex zum Uebertragen.")
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
        _emit(log, "Nichts zu uebertragen.")
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
            _emit(log, f"Fehler beim Uebertragen ({item.title}): {e}")
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
    """Autopilot: alle drei Stufen hintereinander (Score-Sync, Plex-Abgleich
    inkl. Rendern, Push). Fuer manuelles Eingreifen einzeln nutzbar: siehe
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
    """
    Poster fuer die Grid-Ansicht. Kommt aus dem lokalen Original-Cache
    (ORIGINALS_DIR) - Wokearr zeigt also immer das eigene, saubere Original
    (der Score kommt als reines CSS-Overlay obendrauf, siehe app.js), egal
    was gerade tatsaechlich als Poster in Plex ausgewaehlt ist. Faellt nur
    zurueck auf Plex' aktuelles Poster, falls fuer diesen Titel noch kein
    Original gecacht ist (z.B. vor dem ersten "Jetzt synchronisieren").

    Explizit nicht cachebar (Cache-Control: no-store), da sich die Datei durch
    Sync/Autopilot jederzeit aendern kann, die URL selbst aber gleich bleibt.
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


@app.route("/api/sync-library", methods=["POST"])
def api_sync_library():
    """Stufe 2 manuell: Plex-Abgleich - Original-Poster nachladen, gebrandete
    Version rendern, entfernte Titel aufraeumen. Kein Push zu Plex (siehe
    /api/apply dafuer)."""
    if demo_mode():
        return jsonify({"error": "Demo-Modus: keine Plex-Bibliothek zum Abgleichen."}), 400

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"state": "running", "progress": [0, 0], "log": []}

    def run():
        try:
            sync_library(log=lambda msg: JOBS[job_id]["log"].append(msg))
            JOBS[job_id]["state"] = "done"
        except Exception as e:
            JOBS[job_id]["state"] = "error"
            JOBS[job_id]["log"].append(str(e))

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/apply", methods=["POST"])
def api_apply():
    """Stufe 3 manuell ("Auf Plex uebertragen"): laedt die gebrandeten Poster
    der angegebenen Titel zu Plex hoch - erzwungen (force=True), unabhaengig
    davon, ob der Score sich seit dem letzten Push geaendert hat. Rendert bei
    Bedarf automatisch nach (siehe push_to_plex)."""
    if demo_mode():
        return jsonify({"error": "Demo-Modus: PLEX_URL/PLEX_TOKEN als Umgebungsvariablen setzen, um wirklich zu uebertragen."}), 400

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


@app.route("/api/job/<job_id>")
def api_job(job_id):
    return jsonify(JOBS.get(job_id, {"state": "unknown"}))


if __name__ == "__main__":
    # Nur fuer lokale Entwicklung ausserhalb von Docker - im Container laeuft gunicorn (siehe Dockerfile)
    print("Demo-Modus:", demo_mode())
    app.run(host="0.0.0.0", port=5005, debug=True)
