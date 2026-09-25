# Wokearr

[Deutsche Version](README.de.md)

Small, self-hosted web UI in a Radarr/Sonarr look that badges movies and shows
in your Plex library with a traffic-light indicator (red/yellow/green), based
on the score from [isitwokeornot.com](https://isitwokeornot.com/).

- Five bands, matching the official ones from isitwokeornot.com: **0–19**
  not woke, **20–39** slightly woke, **40–59** woke, **60–79** very woke,
  **80–100** super woke. Two color palettes to choose from (`BADGE_COLOR_SCHEME`)
- Matching runs on the TMDb ID that both Plex and isitwokeornot.com keep per
  title
- For shows, every season gets badged too (with the show's score, on that
  season's own poster) - not just the show itself, so browsing into a show
  doesn't hit unbadged season tiles. Individual episode thumbnails are left
  alone
- The original poster stays the base; the badge is only rendered on top and
  uploaded to Plex as a new poster - rendering and uploading are two separate
  steps, each with its own local file (`originals/`, `branded/`), so you can
  check the burned-in result before it's pushed to Plex
- **Autopilot:** once `AUTO_SYNC_CRON` is set to a cron schedule, everything
  runs on its own - new titles automatically get their score, their original
  poster, their rendered badge, and get uploaded to Plex; removed titles get
  cleaned up (see [Autopilot](#autopilot---automatic-operation))

## Screenshot

Poster grid with colored score badges, a filter bar with the five score bands
and sorting (title, score, release date - ascending or descending, remembered
per browser), and buttons for score sync, Plex comparison, and pushing the
badges.

## Quickstart (Docker Compose)

```bash
git clone https://github.com/Jake-double-one/wokearr.git
cd wokearr
cp .env.example .env
# fill in .env with PLEX_URL / PLEX_TOKEN / LIBRARY_SECTIONS
docker compose up -d
```

The compose file pulls the ready-built image directly from
`ghcr.io/jake-double-one/wokearr` (see [Releases](https://github.com/Jake-double-one/wokearr/releases)) -
no local build needed.

Then open `http://<server-ip>:5005`.

Without a valid `PLEX_URL`/`PLEX_TOKEN`, the app automatically starts in
**demo mode** with three example titles, so you can try the UI risk-free.

## Deploying as a stack in Portainer

1. **Stacks -> Add stack**
2. Choose **Web editor** as the source and paste the contents of
   `docker-compose.yaml` as-is - the file pulls the ready-built image from
   `ghcr.io/jake-double-one/wokearr` directly, no build needed.
   (Alternatively, use **Repository** as the source with compose path
   `docker-compose.yaml` - Portainer then builds from the repo code itself
   instead; for that, replace `image:` with `build:` as described in the
   compose file.)
3. Under **Environment variables**, set `PLEX_URL`, `PLEX_TOKEN`,
   `LIBRARY_SECTIONS` (Portainer doesn't automatically read the `.env` file).
4. **Deploy the stack**.

To update to a new [release](https://github.com/Jake-double-one/wokearr/releases),
just use **Stacks -> wokearr -> Pull and redeploy** in Portainer (pulls the
`:latest` image again). To pin a specific version, change the tag in `image:`,
e.g. to `:v0.1.0`.

## Environment variables

| Variable           | Required | Default        | Description                                      |
|---------------------|---------|-----------------|----------------------------------------------------|
| `PLEX_URL`          | yes*    | –               | e.g. `http://192.168.1.10:32400`                  |
| `PLEX_TOKEN`        | yes*    | –               | [Find your token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/) |
| `LIBRARY_SECTIONS`  | no    | `Filme,Serien`  | Exact names of your Plex libraries, comma-separated |
| `LANGUAGE`          | no    | `en-US`         | UI language: `en-US` \| `de-DE` \| `fr-FR` \| `es-ES` \| `it-IT` \| `pl-PL` \| `tr-TR`. `en-US` is also the fallback for individual missing translations in other languages |
| `BADGE_POSITION`    | no    | `top-right`     | `top-right` \| `top-left` \| `bottom-right` \| `bottom-left` |
| `BADGE_LABEL_STYLE` | no    | `percent`       | `percent` (`37%`) \| `woke` (`37% woke`) |
| `BADGE_WIDTH_PERCENT` | no  | `20`            | Badge width relative to poster width, in percent. Hard-floored at `20` (lower values are automatically raised) |
| `BADGE_COLOR_SCHEME` | no  | `standard`      | Palette for the five score bands: `standard` (as used by isitwokeornot.com) \| `modified` (green/yellow/orange/red/violet) |
| `RUN_HISTORY_RETENTION` | no | `4w`          | How long run-protocol entries are kept: `<number><unit>` with `d`/`w`/`m`, e.g. `3d`, `4w`, `6m`. `0` keeps everything |
| `AUTO_SYNC_CRON` | no | empty (off) | 5-field cron expression for the full autopilot run (score sync, poster cache, cleanup, auto-push), evaluated in `TZ`. Empty or `0` disables it. E.g. `0 * * * *` for hourly. |
| `TZ` | no | `Etc/UTC` | Time zone for log times **and** the `AUTO_SYNC_CRON` schedule, e.g. `Europe/Berlin`. Without it, `0 7-23 * * *` means 7-23 o'clock UTC, not your local time |
| `CACHE_REBUILD_COOLDOWN_MINUTES` | no | `5` | Minimum gap between two sitemap fetches (manual or automatic) |
| `CLEANUP_OLD_POSTERS` | no | `true` | After every push, automatically delete older, self-uploaded poster versions in Plex (see below) |
| `NOTIFY_URL` | no | empty (off) | Where to send notifications about autopilot runs: Gotify, ntfy or a webhook (see [Notifications](#notifications)) |
| `NOTIFY_ON` | no | `error,changes` | What to notify about: `error` (a stage failed) and/or `changes` (new titles badged, scores of your titles changed) |
| `AUTH_METHOD` | no | `none` | Login in front of the UI: `none` \| `basic` (browser popup, e.g. for authentik) \| `forms` (login page). See [Authentication](#authentication) |
| `AUTH_USERNAME` / `AUTH_PASSWORD` | with `basic`/`forms` | – | The login, shared by `basic` and `forms`. Instead of `AUTH_PASSWORD`, `AUTH_PASSWORD_FILE` can point to a file (Docker secrets) |
| `LOG_LEVEL` | no | `INFO` | Detail of the container log: `DEBUG` \| `INFO` \| `WARNING` \| `ERROR`. `DEBUG` adds a line per title (see [Container log](#container-log)) |

\* Without these two variables, the app runs in demo mode.

Changes to environment variables only take effect after a **container
redeploy** (Portainer: **Update the stack**, not just reloading the page).
Changing a badge setting (`BADGE_COLOR_SCHEME`, `BADGE_POSITION`,
`BADGE_LABEL_STYLE`, `BADGE_WIDTH_PERCENT`) re-renders and re-uploads the
affected posters on the next sync automatically - the settings are burned
into the image, so Wokearr tracks which settings a poster was rendered with.
Note that the first run after such a change therefore covers the **whole**
library, seasons included.

### Setting PLEX_URL correctly

The most common source of errors. `PLEX_URL` needs **scheme + host + port**,
otherwise you'll see SSL/connection errors in the logs (e.g.
`TLSV1_UNRECOGNIZED_NAME` or `Max retries exceeded`):

- **Scheme**: `http://`, not `https://` - Plex speaks unencrypted HTTP by
  default on the local network. `https://` without your own certificate lands
  on port 443, where no matching server responds.
- **Port**: always include `:32400` (Plex's default port). Without a port,
  `https://` defaults to 443 and `http://` to 80 - both wrong.
- **Host**: the local IP or hostname of your Plex server, reachable from the
  Docker host/container's point of view (e.g. `192.168.1.10`, not
  `localhost`, unless the app runs in the same network namespace as Plex).
- No trailing slash needed.

Correct: `PLEX_URL=http://192.168.1.10:32400`
Wrong: `https://192.168.1.10`, `192.168.1.10:32400` (no scheme), `http://192.168.1.10` (no port)

The `/data` volume holds and survives container restarts/updates:
`score_cache.json` (score database), `originals/` (unbadged posters),
`branded/` (fully rendered posters, not necessarily uploaded yet),
`rendered_state.json`/`pushed_state.json` (track, per title, which score and
badge settings were last rendered and last uploaded to Plex),
`run_history.json` (the run protocol shown in the footer),
`library_index.json` (which titles are in your Plex library, so score changes
can be reported for your titles only), `session_secret` (signs the login
cookie with `AUTH_METHOD=forms`),
`url_state.json` (per review URL, the last fetch attempt - so pages that
yield no usable score aren't re-fetched on every run).

### Run protocol

The footer shows one quiet line with the last run and what it changed
(matched titles incl. the delta, scores updated and changed, posters
rendered/uploaded, orphans cleaned up) and, with the autopilot on, when the
next run is due. A click expands the recent runs as a compact table -
collapsed by default. Every run is recorded, whether it came from the
autopilot or from one of the buttons. How long entries are kept is set via
`RUN_HISTORY_RETENTION`. The footer also shows the running version and, for
`latest` images, the build date. The version links to this repository - for a
tagged version straight to its release notes.

**"Scores updated" vs. "scores changed":** *updated* counts the review pages
fetched again because isitwokeornot.com marked them as modified - often just
an edited text or a new review. *Changed* counts only titles in your library
whose score value actually changed; only those trigger a notification.

A failed run names the stage and the reason in plain words, e.g. "Score
database failed: isitwokeornot.com not responding (timeout)", and is shown in
red. Rows with more to tell can be expanded (tap or click): which of your
titles got a new score (`Barbie: 72 → 81`), which were badged for the first
time, and the raw error message. The full technical details, including the
traceback, are in the [container log](#container-log).

A per-version list of changes is in [CHANGELOG.md](CHANGELOG.md).

## Autopilot - automatic operation

Set `AUTO_SYNC_CRON` to a cron schedule (e.g. `0 * * * *` for hourly, see
[crontab.guru](https://crontab.guru/) to build one) and Wokearr runs entirely
on its own, without you needing to touch the UI. Every run performs the same
three stages in sequence, which can also be triggered individually via the
buttons below:

1. **Update Score Database** - pulls new/missing titles from
   isitwokeornot.com (incremental).
2. **Sync Now** (Plex comparison) - for every title in your Plex library with
   a known score: fetches the missing original poster from Plex
   (`originals/`) and renders the branded version from it (`branded/`), if
   not already done with the current score. Also cleans up local files for
   titles no longer in your Plex library ("orphans") - reuses the library
   listing it already fetched, so it costs no extra Plex request. Doesn't
   upload anything to Plex yet.
3. **Push to Plex** - new titles or titles with a changed score get their
   already-rendered `branded/` poster uploaded to Plex; old self-uploaded
   poster versions are cleaned up as usual (see below). Titles already
   uploaded with their current score are skipped - so every run in normal
   operation is a quick no-op check, not a full pass through the library.

The score-sync step shares a cooldown (`CACHE_REBUILD_COOLDOWN_MINUTES`,
default 5 minutes) with the manual button below, counted since the last
sitemap fetch, so isitwokeornot.com isn't hit too often - a run that's too
early simply skips this stage and continues with the rest. A sitemap fetch
that fails (after one retry for timeouts and server errors) doesn't use up the
cooldown, since no review page was requested.

The stages fail independently: if isitwokeornot.com is unreachable, the run
continues with the scores already in the local database, so new Plex titles
still get their badge. Only a failed Plex comparison skips the push, as it
needs the same Plex connection.

The schedule is evaluated in the container's time zone - set `TZ` (e.g.
`Europe/Berlin`), otherwise it runs on UTC.

## Authentication

Optional, off by default - like Radarr and Sonarr, set via `AUTH_METHOD`:

| `AUTH_METHOD` | What you get |
|---|---|
| `none` (default) | No login. Also the right choice behind a proxy that authenticates on its own (e.g. authentik forward auth) - then port 5005 must not be reachable directly, or the proxy can simply be bypassed |
| `basic` | The browser's own login popup (HTTP Basic). Works with authentik's "Send HTTP-Basic Authentication" (see below) |
| `forms` | A login page like Radarr's, with "Remember me" (30 days) and a logout link in the footer |

Both use the same login from `AUTH_USERNAME` and `AUTH_PASSWORD` (or
`AUTH_PASSWORD_FILE`), so you can switch between them without changing
anything else:

```yaml
    environment:
      AUTH_METHOD: "forms"
      AUTH_USERNAME: "admin"
      AUTH_PASSWORD: "a long password"
```

In a compose file, write a `$` in the password as `$$` - otherwise compose
treats it as a variable and a different password arrives.

**`basic` only behind HTTPS.** The browser sends the credentials with every
request, merely base64-encoded, and there's no logout - it keeps them until
it's closed. Radarr removed Basic in v6 for these reasons. Behind authentik
with HTTPS that's fine: authentik injects the credentials itself.

**authentik with `basic`:** create a group with the attributes
`wokearr_user` and `wokearr_password` (same values as `AUTH_USERNAME`/
`AUTH_PASSWORD`), then in the Wokearr proxy provider enable *Send HTTP-Basic
Authentication* with those two attribute names. authentik logs you in once
and passes the login on - no second prompt.

Also, whenever a login is active:

- **Fails closed:** if `basic`/`forms` is set but the login is incomplete, or
  `AUTH_METHOD` is unknown, Wokearr stays locked and shows the reason - it
  never silently falls back to "no login". `/healthz` then reports
  `unhealthy`, so it shows up in Portainer.
- **Brute-force protection:** after 5 failed logins from one address within
  15 minutes, further attempts from it are refused until the oldest failure
  has aged out. Every failure is logged in a form fail2ban/CrowdSec can
  parse: `WARNING [auth] Failed login for user 'x' from 203.0.113.5`
- **CSRF protection:** requests that change something need a header the UI
  sends and foreign pages can't, so another website can't trigger actions
  with your browser's stored login.
- With `forms`, the session cookie is HttpOnly and SameSite=Lax, and Secure
  whenever the page is served over HTTPS (also behind a proxy that sends
  `X-Forwarded-Proto`). Changing `AUTH_USERNAME` or `AUTH_PASSWORD` ends all
  existing sessions. The signing key lives in `/data/session_secret`, so a
  restart doesn't log you out.
- `/healthz` stays reachable without a login, for Docker's health check.

## Notifications

With `NOTIFY_URL` set, the autopilot sends a message when a stage fails and/or
when something changed (`NOTIFY_ON`): new titles badged, or isitwokeornot.com
changed the score of a title in your library. Quiet runs send nothing - that
includes runs that only show "scores updated" (see [Run protocol](#run-protocol)). Manual
runs don't notify - you're looking at the UI then anyway. The URL formats
follow [Apprise](https://github.com/caronc/apprise/wiki)'s:

| Service | `NOTIFY_URL` |
|---|---|
| Gotify | `gotify://host/APP_TOKEN` (http), `gotifys://host/APP_TOKEN` (https), also with port and sub-path: `gotifys://host:8443/gotify/APP_TOKEN` |
| ntfy | `ntfy://TOPIC` (ntfy.sh), `ntfy://host/TOPIC` (http), `ntfys://host/TOPIC` (https), with login `ntfys://user:pass@host/TOPIC` or token `ntfys://host/TOPIC?token=tk_...` |
| Webhook | any `http(s)://` URL - receives a JSON `POST` with `title`, `message`, `priority` (`normal`/`high`) |

Failures get high priority (Gotify 8, ntfy 4), changes normal. To check the
setup, send a test message from inside the container:

```bash
docker exec wokearr python notify.py
```

## Container log

Every line has a timestamp (in `TZ`), level and area, e.g.:

```
2026-09-22 19:00:00 INFO    [run] Started: Autopilot
2026-09-22 19:00:02 WARNING [score-sync] Sitemap fetch failed after 2.0s (attempt 1/2): HTTPError: 503 ... - retrying in 15s.
2026-09-22 19:00:17 ERROR   [autopilot] Score database failed: isitwokeornot.com returned HTTP 503
Traceback (most recent call last): ...
2026-09-22 19:00:17 WARNING [autopilot] Continuing with the scores already in the local database.
2026-09-22 19:00:19 WARNING [run] Finished with errors: Autopilot in 19.4s - 27 matched; Score database failed: ...
```

Log messages are always in English, whatever `LANGUAGE` is set to - so they
can be searched and shared in an issue as they are. At startup, Wokearr logs
its effective configuration (version, time zone, libraries, schedule and next
run, notification target) - never tokens or passwords. `LOG_LEVEL=DEBUG` adds
a line per title (fetched, rendered, removed). View it with
`docker logs wokearr`, or in Portainer under the container's **Logs**.

## Manual operation

Not needed for normal operation with the autopilot active, but meant for
anyone who deliberately turns off the autopilot (`AUTO_SYNC_CRON` empty, the
default) and wants to trigger each stage themselves:

- **Update Score Database** - stage 1 only, incremental.
- **Sync Now** - stage 2 only (Plex comparison, maintain original and branded
  posters, remove orphans). No push to Plex.
- **Push to Plex** (the "all" button or per title) - stage 3 only, forced:
  uploads the rendered poster regardless of whether the score has changed
  since the last push (useful, e.g., after changing a badge setting, to force
  everything to re-upload). Renders on demand if not already synced.
- **Full Rebuild** - like "Update Score Database", but really re-queries
  every title at isitwokeornot.com (e.g. to pick up scores that changed in
  the meantime). Takes correspondingly longer.
- **Delete Old Posters in Plex** - see next section.

All jobs keep running as a background process in the container, even if you
reload the page, filter, or close the browser tab.

### Plex accumulates old poster versions

Plex automatically keeps the previous version of every uploaded poster as
"poster history" (visible in Plex's poster picker) and never deletes it on
its own - this is normal Plex behavior, not specific to this tool, but it
noticeably fills up the Plex server's disk space over time (especially after
pushing the same title multiple times, e.g. while testing).

- **Automatic:** with `CLEANUP_OLD_POSTERS=true` (default), the app removes
  all uploaded versions of a title in Plex after every push ("Push to
  Plex"), except the currently active one - detected via Plex's own key
  scheme for uploads, not by image content. This also catches uploads from
  before this feature existed. TMDb/agent posters are never touched (and
  can't technically be deleted via the Plex API either). Rule of thumb:
  anything ever uploaded via Wokearr (or manually in Plex) that's no longer
  active gets removed.
- **One-off, for the whole library:** the **"Delete Old Posters in Plex"**
  button goes through every title and cleans up already-accumulated old
  versions.
- This only affects Plex's own storage, not this app's `/data` Docker volume.

## Notes

- There's no official API from isitwokeornot.com; scores are read from the
  structured `schema.org/Review` data block on each title page. The site's
  `robots.txt` only blocks `/api/` and `/admin` - normal page requests are
  allowed, but please stay fair anyway (don't set the cache script's default
  delay to 0). Normal (non-full) score syncs only re-fetch new/changed
  reviews thanks to the sitemap's `<lastmod>` - at the operator's request, to
  keep repeated runs lean. A review page that yields no usable score (no
  Review block, no TMDb ID, an HTTP error) is remembered as such and only
  retried once the sitemap reports a change, or after a week at the earliest -
  otherwise it would count as "new" and be re-fetched on every single run.
  Links to review pages in the UI carry UTM
  parameters (`utm_source=wokearr`), so isitwokeornot.com can see how much
  traffic Wokearr sends them.
- This project is a private hobby tool with no affiliation to
  isitwokeornot.com, Plex Inc., or TMDb.
- Uploaded posters generally stay "selected" in Plex, even after a metadata
  refresh. If Plex does revert to the original, you can manually lock the
  poster in the Plex web UI (right-click -> Poster -> Lock).

## License

MIT, see [LICENSE](LICENSE).
