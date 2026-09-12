#!/usr/bin/env python3
"""
Baut einen lokalen Cache TMDb-ID -> Woke-Score, indem alle Titel-URLs aus der
Sitemap von isitwokeornot.com geholt und deren JSON-LD "Review" ausgelesen wird.

Ergebnis: score_cache.json im gleichen Ordner, Format:
{
  "movie:1084242": {"title": "Zootopia 2", "score": 86, "imdb_id": "tt26443597", "slug": "zootopia-2"},
  "tv:223530":     {"title": "Star Trek: Starfleet Academy", "score": 87, ...},
  ...
}

Hinweis: robots.txt von isitwokeornot.com erlaubt das Crawlen normaler Seiten
(nur /api/ und /admin sind gesperrt). Trotzdem: SLEEP_SECONDS nicht auf 0 setzen,
das sind ca. 5.500 Titel-Requests - langsam und fair bleiben.

Normale (nicht-vollstaendige) Laeufe nutzen das <lastmod> der Sitemap, um nur
neue/geaenderte Reviews abzufragen (siehe build_cache) - Absprache mit dem
Betreiber, um wiederholte Laeufe schlank zu halten.
"""
import json
import re
import time
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import os

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ScoreCacheBuilder/0.1)"}
DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = DATA_DIR / "score_cache.json"
SLEEP_SECONDS = 0.3       # Pause zwischen Requests pro Worker
MAX_WORKERS = 4           # parallele Requests - hoeher = schneller, aber unfreundlicher
SITEMAP_URL = "https://isitwokeornot.com/sitemaps/titles-1.xml"


def get_title_urls() -> list[tuple[str, str | None]]:
    """Gibt (url, lastmod) je Sitemap-Eintrag zurueck. lastmod ist None, falls die
    Sitemap fuer diese URL keins angibt."""
    resp = requests.get(SITEMAP_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    entries = []
    for block in re.findall(r"<url>(.*?)</url>", resp.text, re.S):
        loc_match = re.search(r"<loc>([^<]+)</loc>", block)
        if not loc_match:
            continue
        lastmod_match = re.search(r"<lastmod>([^<]+)</lastmod>", block)
        entries.append((loc_match.group(1), lastmod_match.group(1) if lastmod_match else None))
    return entries


def extract_one(url: str, lastmod: str | None) -> dict | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code >= 400:
            return None
        html = r.text
        blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
        review = None
        for b in blocks:
            try:
                data = json.loads(b)
            except json.JSONDecodeError:
                continue
            if data.get("@type") == "Review":
                review = data
        if review is None:
            return None

        tmdb_match = re.search(r"themoviedb\.org/(movie|tv)/(\d+)", html)
        imdb_match = re.search(r"imdb\.com/title/(tt\d+)", html)
        if not tmdb_match:
            return None

        media_type, tmdb_id = tmdb_match.group(1), tmdb_match.group(2)
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        return {
            "key": f"{media_type}:{tmdb_id}",
            "title": review.get("itemReviewed", {}).get("name"),
            "score": review["reviewRating"]["ratingValue"],
            "imdb_id": imdb_match.group(1) if imdb_match else None,
            "slug": slug,
            "media_type": media_type,
            "url": url,
            "lastmod": lastmod,
        }
    except requests.RequestException:
        return None
    finally:
        time.sleep(SLEEP_SECONDS)


def build_cache(cache: dict, on_progress=None, skip_existing: bool = True) -> tuple[dict, int]:
    """
    Ergaenzt cache (in-place) um neue/geaenderte Titel aus der Sitemap - parallel mit
    MAX_WORKERS Workern.

    Bei skip_existing=True wird pro Sitemap-Eintrag deren <lastmod> mit dem zuletzt
    gespeicherten lastmod des passenden Cache-Eintrags verglichen (Bitte des
    isitwokeornot.com-Betreibers, um nicht bei jedem Lauf alle ~5.500 Reviews neu
    abzufragen): stimmen beide ueberein, wird die URL uebersprungen - nur neue Titel
    und Titel mit geaendertem lastmod werden tatsaechlich erneut geladen. Kennt die
    Sitemap fuer eine URL kein lastmod, oder hat der bestehende Cache-Eintrag noch
    keins (aeltere Eintraege vor Einfuehrung dieses Felds), gilt der Titel weiterhin
    als erledigt, sobald er einmal im Cache steht - das entspricht dem bisherigen
    Verhalten und vermeidet einen einmaligen Voll-Refetch nur wegen fehlender
    lastmod-Historie.

    on_progress(done, total) wird nach jedem verarbeiteten Titel aufgerufen (total
    zaehlt nur die tatsaechlich abzufragenden URLs, keine uebersprungenen).

    Gibt (cache, anzahl_verarbeiteter_urls) zurueck.
    """
    entries = get_title_urls()
    if skip_existing:
        known_lastmod_by_url = {e["url"]: e.get("lastmod") for e in cache.values() if e.get("url")}

        def needs_fetch(url: str, lastmod: str | None) -> bool:
            if url not in known_lastmod_by_url:
                return True
            known_lastmod = known_lastmod_by_url[url]
            if lastmod is None or known_lastmod is None:
                return False
            return lastmod != known_lastmod

        entries = [(u, lm) for u, lm in entries if needs_fetch(u, lm)]

    total = len(entries)
    done = 0
    if on_progress:
        on_progress(done, total)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(extract_one, u, lm): u for u, lm in entries}
        for fut in as_completed(futures):
            result = fut.result()
            done += 1
            if result:
                cache[result["key"]] = {k: v for k, v in result.items() if k != "key"}
            if on_progress:
                on_progress(done, total)

    return cache, done


def main():
    cache = {}
    if CACHE_FILE.exists():
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        print(f"Bestehenden Cache geladen ({len(cache)} Eintraege) - wird ergaenzt.")

    print("Hole Sitemap ...")

    def on_progress(done, total):
        if done == 0:
            print(f"{total} neue/fehlende Titel-URLs zu verarbeiten.")
        elif done % 200 == 0:
            print(f"  {done}/{total} verarbeitet, {len(cache)} im Cache")
            CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")

    cache, done = build_cache(cache, on_progress=on_progress)
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Fertig. {done} Titel neu verarbeitet, {len(cache)} insgesamt im Cache. -> {CACHE_FILE}")


if __name__ == "__main__":
    main()
