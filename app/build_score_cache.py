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


def get_title_urls() -> list[str]:
    resp = requests.get(SITEMAP_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return re.findall(r"<loc>([^<]+)</loc>", resp.text)


def extract_one(url: str) -> dict | None:
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
        }
    except requests.RequestException:
        return None
    finally:
        time.sleep(SLEEP_SECONDS)


def main():
    print("Hole Sitemap ...")
    urls = get_title_urls()
    print(f"{len(urls)} Titel-URLs gefunden.")

    cache = {}
    if CACHE_FILE.exists():
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        print(f"Bestehenden Cache geladen ({len(cache)} Eintraege) - wird ergaenzt/aktualisiert.")

    done, failed = 0, 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(extract_one, u): u for u in urls}
        for fut in as_completed(futures):
            result = fut.result()
            done += 1
            if result:
                cache[result["key"]] = {k: v for k, v in result.items() if k != "key"}
            else:
                failed += 1
            if done % 200 == 0:
                print(f"  {done}/{len(urls)} verarbeitet, {failed} ohne Score, {len(cache)} im Cache")
                CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")

    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Fertig. {len(cache)} Titel mit Score im Cache, {failed} ohne Treffer. -> {CACHE_FILE}")


if __name__ == "__main__":
    main()
