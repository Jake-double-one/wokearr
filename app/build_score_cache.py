#!/usr/bin/env python3
"""
Builds a local cache of TMDb ID -> woke score by fetching all title URLs from
isitwokeornot.com's sitemap and reading their JSON-LD "Review" block.

Result: score_cache.json in the same folder, format:
{
  "movie:1084242": {"title": "Zootopia 2", "score": 86, "imdb_id": "tt26443597", "slug": "zootopia-2"},
  "tv:223530":     {"title": "Star Trek: Starfleet Academy", "score": 87, ...},
  ...
}

Note: isitwokeornot.com's robots.txt allows crawling normal pages (only
/api/ and /admin are blocked). Still: don't set SLEEP_SECONDS to 0, that's
about 5,500 title requests - stay slow and fair.

Normal (non-full) runs use the sitemap's <lastmod> to only re-fetch
new/changed reviews (see build_cache) - agreed with the operator, to keep
repeated runs lean.
"""
import json
import re
import time
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import os

from logsetup import get_logger, setup_logging

log = get_logger("score-sync")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ScoreCacheBuilder/0.1)"}
DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = DATA_DIR / "score_cache.json"
URL_STATE_FILE = DATA_DIR / "url_state.json"
FAILED_RETRY_DAYS = 7     # how long a URL that yielded no score is left alone
SLEEP_SECONDS = 0.3       # pause between requests per worker
MAX_WORKERS = 4           # parallel requests - higher = faster, but less polite
SITEMAP_URL = "https://isitwokeornot.com/sitemaps/titles-1.xml"
SITEMAP_TIMEOUT = 30          # seconds per attempt
SITEMAP_ATTEMPTS = 2          # one retry - a single slow response shouldn't cost the whole run
SITEMAP_RETRY_PAUSE = 15      # seconds between attempts


class SitemapError(Exception):
    """The sitemap couldn't be fetched at all - raised before any review page
    is requested. The original requests exception is the __cause__."""


def _is_retryable(exc: requests.RequestException) -> bool:
    """Timeouts, connection errors and server-side trouble (5xx, 429) may be
    gone a few seconds later; a 404 or 403 won't be."""
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status is not None and (status >= 500 or status == 429)


def _fetch_sitemap() -> str:
    for attempt in range(1, SITEMAP_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            resp = requests.get(SITEMAP_URL, headers=HEADERS, timeout=SITEMAP_TIMEOUT)
            resp.raise_for_status()
            log.debug("Sitemap fetched in %.1fs (%d KB).", time.monotonic() - started, len(resp.content) // 1024)
            return resp.text
        except requests.RequestException as e:
            elapsed = time.monotonic() - started
            if attempt < SITEMAP_ATTEMPTS and _is_retryable(e):
                log.warning("Sitemap fetch failed after %.1fs (attempt %d/%d): %s: %s - retrying in %ds.",
                            elapsed, attempt, SITEMAP_ATTEMPTS, type(e).__name__, e, SITEMAP_RETRY_PAUSE)
                time.sleep(SITEMAP_RETRY_PAUSE)
                continue
            raise SitemapError(
                f"Sitemap fetch failed after {elapsed:.1f}s (attempt {attempt}/{SITEMAP_ATTEMPTS}): {SITEMAP_URL}"
            ) from e
    raise AssertionError("unreachable")


def get_title_urls() -> list[tuple[str, str | None]]:
    """Returns (url, lastmod) per sitemap entry. lastmod is None if the
    sitemap doesn't give one for that URL. Raises SitemapError if the sitemap
    can't be fetched, after one retry for transient errors."""
    text = _fetch_sitemap()
    entries = []
    for block in re.findall(r"<url>(.*?)</url>", text, re.S):
        loc_match = re.search(r"<loc>([^<]+)</loc>", block)
        if not loc_match:
            continue
        lastmod_match = re.search(r"<lastmod>([^<]+)</lastmod>", block)
        entries.append((loc_match.group(1), lastmod_match.group(1) if lastmod_match else None))
    return entries


def extract_one(url: str, lastmod: str | None) -> tuple[dict | None, str]:
    """Fetches one review page. Returns (entry, status); entry is None unless
    status is "ok". The status is kept per URL (see URL_STATE_FILE) so a page
    that yields no usable score isn't re-fetched on every single run."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code >= 400:
            return None, f"http_{r.status_code}"
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
            return None, "no_review"

        tmdb_match = re.search(r"themoviedb\.org/(movie|tv)/(\d+)", html)
        imdb_match = re.search(r"imdb\.com/title/(tt\d+)", html)
        if not tmdb_match:
            return None, "no_tmdb_id"

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
        }, "ok"
    except requests.RequestException:
        return None, "request_error"
    except (KeyError, TypeError, ValueError):
        # Review block present, but not in the shape we expect
        return None, "bad_review"
    finally:
        time.sleep(SLEEP_SECONDS)


def load_url_state() -> dict:
    """Per-URL record of the last fetch attempt: {url: {lastmod, status, checked}}."""
    if not URL_STATE_FILE.exists():
        return {}
    try:
        state = json.loads(URL_STATE_FILE.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_url_state(state: dict) -> None:
    try:
        URL_STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("Could not write %s: %s", URL_STATE_FILE.name, e)


def build_cache(cache: dict, on_progress=None, skip_existing: bool = True,
                changes: list | None = None) -> tuple[dict, int]:
    """
    Extends cache (in-place) with new/changed titles from the sitemap - in
    parallel with MAX_WORKERS workers.

    With skip_existing=True, each sitemap entry's <lastmod> is compared
    against the lastmod of the last fetch attempt for that URL (at the
    isitwokeornot.com operator's request, to avoid re-fetching all ~5,500
    reviews on every run): if both match, the URL is skipped - only new
    titles and titles with a changed lastmod are actually re-fetched. If the
    sitemap has no lastmod for a URL, or we have none stored for it (older
    entries from before this field existed), the title is still considered
    done once it's known - this matches the previous behavior and avoids a
    one-off full refetch just because of missing lastmod history.

    The attempt is tracked per URL in URL_STATE_FILE, not just via the cache
    entries: a page that yields no usable score (no Review block, no TMDb ID,
    an HTTP error, or a second review page for a TMDb ID we already have)
    never lands in the cache, and would otherwise count as "new" and be
    re-fetched on every single run, forever. Such URLs are retried at most
    every FAILED_RETRY_DAYS days, so a temporary glitch still heals itself.

    on_progress(done, total) is called after each processed title (total
    only counts the URLs actually being fetched, not the skipped ones).

    If a list is passed as changes, every title whose score differs from the
    one already cached is appended to it as {key, title, old, new} - new
    titles aren't changes and aren't included.

    Returns (cache, number_of_urls_processed). Raises SitemapError if the
    sitemap can't be fetched - nothing has been requested from the review
    pages at that point.
    """
    entries = get_title_urls()
    sitemap_total = len(entries)
    url_state = load_url_state()
    now = time.time()

    if skip_existing:
        # Fall back to the cache entries for installs that don't have a
        # url_state.json yet - otherwise updating would trigger one full
        # refetch of everything already known.
        known_lastmod_by_url = {e["url"]: e.get("lastmod") for e in cache.values() if e.get("url")}

        def needs_fetch(url: str, lastmod: str | None) -> bool:
            record = url_state.get(url)
            if record is None:
                if url not in known_lastmod_by_url:
                    return True
                known_lastmod = known_lastmod_by_url[url]
            else:
                if record.get("status") != "ok":
                    checked = record.get("checked") or 0
                    if now - checked >= FAILED_RETRY_DAYS * 86400:
                        return True
                known_lastmod = record.get("lastmod")
            if lastmod is None or known_lastmod is None:
                return False
            return lastmod != known_lastmod

        entries = [(u, lm) for u, lm in entries if needs_fetch(u, lm)]
    log.info("Sitemap: %d review pages, %d to fetch.", sitemap_total, len(entries))

    total = len(entries)
    done = 0
    unresolved = {}
    if on_progress:
        on_progress(done, total)

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(extract_one, u, lm): (u, lm) for u, lm in entries}
        for fut in as_completed(futures):
            url, lastmod = futures[fut]
            results.append((url, lastmod, *fut.result()))
            done += 1
            if on_progress:
                on_progress(done, total)

    # Applied in URL order, not in the order the threads happened to finish, so
    # that a TMDb ID claimed by two review pages always resolves the same way.
    for url, lastmod, result, status in sorted(results, key=lambda r: r[0]):
        if result:
            key = result["key"]
            if cache.get(key, {}).get("url") not in (None, url):
                # Two review pages share one TMDb ID - only one of them can own
                # the cache entry, so note the other as a known duplicate
                # instead of re-fetching it on every run.
                status = "duplicate_tmdb_id"
            else:
                old = cache.get(key)
                if old is None:
                    log.debug("New review: %s (%s) %s%%", result.get("title") or "?", key, result["score"])
                elif old.get("score") != result["score"] and changes is not None:
                    changes.append({"key": key, "title": result.get("title") or old.get("title"),
                                    "old": old.get("score"), "new": result["score"]})
                cache[key] = {k: v for k, v in result.items() if k != "key"}
        if status != "ok":
            unresolved[status] = unresolved.get(status, 0) + 1
            log.debug("No usable score (%s): %s", status, url)
        url_state[url] = {"lastmod": lastmod, "status": status, "checked": now}

    if entries:
        save_url_state(url_state)
    if unresolved:
        summary = ", ".join(f"{count}x {status}" for status, count in sorted(unresolved.items()))
        log.info("%d URLs without a usable score (%s) - retried in %d days at the earliest.",
                 sum(unresolved.values()), summary, FAILED_RETRY_DAYS)

    return cache, done


def main():
    setup_logging()
    cache = {}
    if CACHE_FILE.exists():
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        log.info("Loaded existing cache (%d entries) - extending it.", len(cache))

    def on_progress(done, total):
        if done and done % 200 == 0:
            log.info("%d/%d processed, %d in cache", done, total, len(cache))
            CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")

    cache, done = build_cache(cache, on_progress=on_progress)
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Done. %d titles newly processed, %d total in cache -> %s", done, len(cache), CACHE_FILE)


if __name__ == "__main__":
    main()
