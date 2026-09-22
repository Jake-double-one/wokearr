# Changelog

All notable changes to Wokearr, newest first. Version numbers match the image
tags on `ghcr.io/jake-double-one/wokearr`.

## [Unreleased]

### Fixed
- **Review pages without a usable score were re-fetched on every run.** A page
  that yields no score (no `Review` block, no TMDb ID, an HTTP error, or a
  second review page for a TMDb ID already in the cache) never lands in the
  score cache, so the incremental sync kept treating it as a new title -
  indefinitely, once per run. Each attempt is now recorded per URL in
  `url_state.json`, and such a URL is only retried when the sitemap reports a
  change, or after a week at the earliest. Nothing to do on upgrade: the first
  run after it still fetches those URLs once, then leaves them alone.
- **The run protocol counted posters as "rendered" that weren't.** The render
  filter still compared against the bare score while the state file stores
  score *and* badge settings, so every title entered the render list on every
  run. The renderer itself correctly skipped them, but the counter went up
  anyway - a library-sized "rendered" number on runs that changed nothing.
- Two review pages claiming the same TMDb ID now resolve deterministically
  (by URL order) instead of depending on which request happened to finish
  first.
- A branded poster deleted from `/data/branded` is rendered again on the next
  sync, instead of being skipped because the state file still listed it.

## [v0.3.0] – 2026-09-19

### Added
- **Five score bands instead of three**, matching the official classification
  from isitwokeornot.com: 0–19 not woke, 20–39 slightly woke, 40–59 woke,
  60–79 very woke, 80–100 super woke. The filter bar now has five chips, named
  by band rather than by color.
- **Two color palettes** via `BADGE_COLOR_SCHEME`: `standard` (default)
  mirrors the colors isitwokeornot.com uses themselves, `modified` is an
  alternative green → yellow → orange → red → violet ramp.
- **Run protocol**: every run – autopilot or button – records what it changed
  (matched titles incl. the delta, scores updated, posters fetched/rendered/
  pushed, orphans cleaned, duration, trigger). Shown as one quiet footer line,
  expandable into a table. Stored as `run_history.json` in `/data`, so it
  survives restarts. Retention via `RUN_HISTORY_RETENTION` (`3d`/`4w`/`6m`,
  `0` keeps everything).
- **Version in the UI**: the footer shows the running version, plus the build
  date for `latest` images. Also exposed via `/healthz`.

### Changed
- Badge settings are now tracked per poster. Changing `BADGE_COLOR_SCHEME`,
  `BADGE_POSITION`, `BADGE_LABEL_STYLE` or `BADGE_WIDTH_PERCENT` takes effect
  automatically on the next sync instead of requiring a manual forced push.

### Upgrade note
The first run after this update re-renders and re-uploads **every** poster,
seasons included – the band colors changed, and without the new tracking
existing posters would silently keep their old three-band colors. Nothing to
do by hand, but expect a noticeably longer first run and corresponding load on
the Plex server.

## [v0.2.5] – 2026-09-16

### Added
- Polish and Turkish UI translations (now 7 languages).
- Seasons of a show are badged too, each on its own season poster with the
  show's score – previously only the show itself was badged, so browsing into
  a show hit unbadged season tiles.

### Changed
- **Breaking:** `AUTO_SYNC_INTERVAL_MINUTES` replaced by `AUTO_SYNC_CRON`, a
  standard 5-field cron expression (e.g. `0 * * * *`). Empty or `0` disables
  the autopilot, as before. An invalid expression is reported at startup and
  disables the autopilot instead of failing silently.
- The library-wide "Delete Old Posters in Plex" button now also covers season
  posters.

## [v0.2.4] – 2026-09-12

### Added
- UI internationalization via `LANGUAGE`, with `en-US` as default and as the
  per-key fallback for incomplete translations.
- French, Spanish and Italian translations.

### Changed
- README split: `README.md` is now English, `README.de.md` holds the German
  version.
- All remaining German code comments, docstrings and CLI output translated to
  English.
- Docker Compose service and container renamed from `woke-score` to `wokearr`.
  The data volume name was deliberately left unchanged so existing
  installations keep their cache and posters.

## [v0.2.3] – 2026-09-12

### Changed
- Score syncs now use the sitemap's `<lastmod>` to only re-fetch new or changed
  reviews instead of every title on each run – at the request of the
  isitwokeornot.com operator, to keep repeated runs light on their site.
- Links to review pages carry UTM parameters (`utm_source=wokearr`), also at
  their request, so they can see the traffic Wokearr sends them.

## [v0.2.2] – 2026-09-08

### Added
- `BADGE_WIDTH_PERCENT` to set the badge width relative to the poster width,
  with a hard 20% minimum so it stays readable on small posters.

### Changed
- Rendering and uploading split into separate stages, each with its own local
  file (`originals/`, `branded/`), so the burned-in result can be checked
  before anything reaches Plex. Buttons reorganized accordingly.
- Mobile layout reworked.
- Own poster uploads are now identified by Plex's own `upload://posters` key
  scheme instead of image content – this also catches uploads from before the
  badge marker existed.

### Fixed
- Poster display is no longer browser-cached, so updated posters actually show
  up instead of a stale image.
- Cleanup decisions are logged, making it traceable why a poster was kept or
  removed.

## [v0.2.1] – 2026-09-08

### Added
- Autopilot: score sync, poster cache, cleanup and auto-apply in one scheduled
  run.
- Old, self-uploaded poster versions in Plex are cleaned up, automatically
  after each push and manually for the whole library.

### Changed
- Applying badges and cleaning up posters run in parallel instead of
  sequentially (previously roughly 20 seconds per title).
- Original posters are fetched directly from Plex's agent candidates.

### Fixed
- Shape of the "woke" style badge.
- Duplicate badges caused by already-badged posters being used as the base.
- A missing original poster is now surfaced instead of failing quietly.

## [v0.2.0] – 2026-09-08

### Added
- Automatic cache refresh and a cooldown between sitemap fetches.
- `BADGE_LABEL_STYLE` to switch between `37%` and `37% woke`.

### Fixed
- Duplicate badges.

## [v0.1.1] – 2026-09-08

### Added
- Source footer linking to the title's page on isitwokeornot.com.

### Changed
- Cache build parallelized and made incremental, so repeated runs are fast.

### Fixed
- Progress display during the cache build.

## [v0.1.0] – 2026-09-08

Initial release: local web UI in Radarr/Sonarr style that badges Plex posters
with the woke score from isitwokeornot.com, matched via TMDb ID. Ships as a
ready-built image on GHCR for Docker Compose and Portainer.

---

Note: the tag `v0.2` points at the same commit as `v0.2.2` – an accidental
duplicate, not a separate release.

[Unreleased]: https://github.com/Jake-double-one/wokearr/compare/v0.3.0...main
[v0.3.0]: https://github.com/Jake-double-one/wokearr/compare/v0.2.5...v0.3.0
[v0.2.5]: https://github.com/Jake-double-one/wokearr/compare/v0.2.4...v0.2.5
[v0.2.4]: https://github.com/Jake-double-one/wokearr/compare/v0.2.3...v0.2.4
[v0.2.3]: https://github.com/Jake-double-one/wokearr/compare/v0.2.2...v0.2.3
[v0.2.2]: https://github.com/Jake-double-one/wokearr/compare/v0.2.1...v0.2.2
[v0.2.1]: https://github.com/Jake-double-one/wokearr/compare/v0.2.0...v0.2.1
[v0.2.0]: https://github.com/Jake-double-one/wokearr/compare/v0.1.1...v0.2.0
[v0.1.1]: https://github.com/Jake-double-one/wokearr/compare/v0.1.0...v0.1.1
[v0.1.0]: https://github.com/Jake-double-one/wokearr/releases/tag/v0.1.0
