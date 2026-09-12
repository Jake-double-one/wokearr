# Wokearr

[Deutsche Version](README.de.md)

Small, self-hosted web UI in a Radarr/Sonarr look that badges movies and shows
in your Plex library with a traffic-light indicator (red/yellow/green), based
on the score from [isitwokeornot.com](https://isitwokeornot.com/).

- **Red** = high score (warning), **Yellow** = medium, **Green** = low
- Matching runs on the TMDb ID that both Plex and isitwokeornot.com keep per
  title
- The original poster stays the base; the badge is only rendered on top and
  uploaded to Plex as a new poster - rendering and uploading are two separate
  steps, each with its own local file (`originals/`, `branded/`), so you can
  check the burned-in result before it's pushed to Plex
- **Autopilot:** once `AUTO_SYNC_INTERVAL_MINUTES` is set, everything runs on
  its own - new titles automatically get their score, their original poster,
  their rendered badge, and get uploaded to Plex; removed titles get cleaned
  up (see [Autopilot](#autopilot---automatic-operation))

## Screenshot

Poster grid with colored score badges, a filter bar (All/Red/Yellow/Green),
and buttons for score sync, Plex comparison, and pushing the badges.

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
| `LANGUAGE`          | no    | `en-US`         | UI language: `en-US` \| `de-DE`. `en-US` is also the fallback for individual missing translations in other languages |
| `BADGE_POSITION`    | no    | `top-right`     | `top-right` \| `top-left` \| `bottom-right` \| `bottom-left` |
| `BADGE_LABEL_STYLE` | no    | `percent`       | `percent` (`37%`) \| `woke` (`37% woke`) |
| `BADGE_WIDTH_PERCENT` | no  | `20`            | Badge width relative to poster width, in percent. Hard-floored at `20` (lower values are automatically raised) |
| `AUTO_SYNC_INTERVAL_MINUTES` | no | `0` (off) | Interval in minutes for the full autopilot run (score sync, poster cache, cleanup, auto-push). `60` for hourly. |
| `CACHE_REBUILD_COOLDOWN_MINUTES` | no | `5` | Minimum gap between two sitemap fetches (manual or automatic) |
| `CLEANUP_OLD_POSTERS` | no | `true` | After every push, automatically delete older, self-uploaded poster versions in Plex (see below) |

\* Without these two variables, the app runs in demo mode.

Changes to environment variables only take effect after a **container
redeploy** (Portainer: **Update the stack**, not just reloading the page).
`BADGE_LABEL_STYLE` and `BADGE_WIDTH_PERCENT` also only affect posters that
are pushed *from now on* - the text and size are burned into the image and
don't change retroactively for posters already pushed.

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
`rendered_state.json`/`pushed_state.json` (track, per title, which score was
last rendered and last uploaded to Plex).

## Autopilot - automatic operation

Set `AUTO_SYNC_INTERVAL_MINUTES` to an interval > 0 (e.g. `60` for hourly) and
Wokearr runs entirely on its own, without you needing to touch the UI. Every
run performs the same three stages in sequence, which can also be triggered
individually via the buttons below:

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
early simply skips this stage and continues with the rest.

## Manual operation

Not needed for normal operation with the autopilot active, but meant for
anyone who deliberately turns off the autopilot (`AUTO_SYNC_INTERVAL_MINUTES=0`,
the default) and wants to trigger each stage themselves:

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
  keep repeated runs lean. Links to review pages in the UI carry UTM
  parameters (`utm_source=wokearr`), so isitwokeornot.com can see how much
  traffic Wokearr sends them.
- This project is a private hobby tool with no affiliation to
  isitwokeornot.com, Plex Inc., or TMDb.
- Uploaded posters generally stay "selected" in Plex, even after a metadata
  refresh. If Plex does revert to the original, you can manually lock the
  poster in the Plex web UI (right-click -> Poster -> Lock).

## License

MIT, see [LICENSE](LICENSE).
