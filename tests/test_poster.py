"""Posters in the grid: small versions the browser may keep, under an
address that changes whenever the poster does."""
import io
import os
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

PLEX = {"PLEX_URL": "http://plex.local:32400", "PLEX_TOKEN": "t" * 20}


def _jpeg(width=1000, height=1500, color=(200, 60, 40)) -> bytes:
    # Noise-free but not flat, so the full-size file is clearly larger than the small one
    im = Image.new("RGB", (width, height), color)
    for x in range(0, width, 20):
        im.paste((x % 255, 80, 160), (x, 0, x + 10, height))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=95)
    return out.getvalue()


@pytest.fixture
def wa(make_app):
    return make_app(**PLEX)


def _store_original(wa, rating_key="7", **kw) -> bytes:
    data = _jpeg(**kw)
    (wa.ORIGINALS_DIR / f"{rating_key}.jpg").write_bytes(data)
    return data


def _size(body: bytes):
    with Image.open(io.BytesIO(body)) as im:
        return im.format, im.size


def test_grid_gets_a_small_version_it_may_keep_for_good(wa):
    original = _store_original(wa)
    version = wa._poster_version("7")
    resp = wa.app.test_client().get(f"/api/poster/7?v={version}")
    assert resp.status_code == 200 and resp.mimetype == "image/jpeg"
    assert _size(resp.data) == ("JPEG", (wa.POSTER_THUMB_WIDTH, 600))
    assert len(resp.data) < len(original) / 3
    assert resp.headers["Cache-Control"] == "private, max-age=31536000, immutable"
    assert (wa.THUMBS_DIR / "7.jpg").read_bytes() == resp.data


def test_without_a_version_the_browser_asks_and_gets_an_empty_answer(wa):
    _store_original(wa)
    client = wa.app.test_client()
    first = client.get("/api/poster/7")
    assert first.status_code == 200 and first.headers["Cache-Control"] == "private, no-cache"
    again = client.get("/api/poster/7", headers={"If-None-Match": first.headers["ETag"]})
    assert again.status_code == 304 and again.data == b""


def test_an_outdated_version_is_not_kept_for_good(wa):
    _store_original(wa)
    resp = wa.app.test_client().get("/api/poster/7?v=something-old")
    assert resp.status_code == 200 and resp.headers["Cache-Control"] == "private, no-cache"


def test_a_changed_original_gets_a_new_version_and_a_new_thumbnail(wa):
    _store_original(wa)
    client = wa.app.test_client()
    old_version = wa._poster_version("7")
    old = client.get(f"/api/poster/7?v={old_version}")

    path = wa.ORIGINALS_DIR / "7.jpg"
    before = path.stat()
    path.write_bytes(_jpeg(color=(20, 200, 90)))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))

    new_version = wa._poster_version("7")
    assert new_version != old_version
    new = client.get(f"/api/poster/7?v={new_version}")
    assert new.data != old.data and "immutable" in new.headers["Cache-Control"]
    assert new.headers["ETag"] != old.headers["ETag"]
    # the browser still holding the old address is told it has changed
    assert "immutable" not in client.get(f"/api/poster/7?v={old_version}").headers["Cache-Control"]


def test_stored_thumbnail_is_reused(wa):
    _store_original(wa)
    client = wa.app.test_client()
    client.get("/api/poster/7")
    with patch.object(wa, "_shrink_poster", side_effect=AssertionError("made again")):
        assert client.get("/api/poster/7").status_code == 200


def test_small_originals_are_not_enlarged(wa):
    _store_original(wa, width=200, height=300)
    assert _size(wa.app.test_client().get("/api/poster/7").data)[1] == (200, 300)


def test_png_original_is_sent_as_jpeg(wa):
    out = io.BytesIO()
    Image.new("RGBA", (800, 1200), (10, 20, 30, 128)).save(out, "PNG")
    (wa.ORIGINALS_DIR / "7.jpg").write_bytes(out.getvalue())
    assert _size(wa.app.test_client().get("/api/poster/7").data) == ("JPEG", (400, 600))


def test_unreadable_original_is_passed_on_and_nothing_is_stored(wa):
    (wa.ORIGINALS_DIR / "7.jpg").write_bytes(b"not an image")
    resp = wa.app.test_client().get("/api/poster/7")
    assert resp.status_code == 200 and resp.data == b"not an image"
    assert not (wa.THUMBS_DIR / "7.jpg").exists()


def test_thumbnail_folder_that_cannot_be_written_still_serves(wa):
    _store_original(wa)
    with patch.object(wa.os, "replace", side_effect=PermissionError("read-only")):
        resp = wa.app.test_client().get("/api/poster/7")
    assert resp.status_code == 200 and _size(resp.data)[1][0] == wa.POSTER_THUMB_WIDTH
    assert list(wa.THUMBS_DIR.iterdir()) == []   # no half-written file left behind


def test_poster_straight_from_plex_is_small_and_revalidated(wa):
    plex = MagicMock()
    plex.url = lambda key, includeToken=None: f"http://plex.local:32400{key}"
    plex.fetchItem.return_value = MagicMock(thumb="/library/metadata/9/thumb/1")
    with patch.object(wa, "get_plex", return_value=plex), patch.object(wa, "_plex_get", return_value=_jpeg()):
        client = wa.app.test_client()
        first = client.get("/api/poster/9")
        assert _size(first.data)[1][0] == wa.POSTER_THUMB_WIDTH
        assert first.headers["Cache-Control"] == "private, no-cache"
        assert client.get("/api/poster/9", headers={"If-None-Match": first.headers["ETag"]}).status_code == 304
    assert not (wa.THUMBS_DIR / "9.jpg").exists()   # nothing cached without an original


def test_library_listing_carries_the_poster_version(wa):
    _store_original(wa, "7")
    wa.CACHE_FILE.write_text('{"movie:1": {"title": "Example Movie", "score": 50, "url": "https://isitwokeornot.com/movie/x"},'
                             ' "movie:2": {"title": "Example Movie 2", "score": 60, "url": "https://isitwokeornot.com/movie/y"}}')

    def item(rating_key, tmdb):
        entry = MagicMock(ratingKey=rating_key, year=2024, titleSort="Example", guids=[MagicMock(id=f"tmdb://{tmdb}")])
        entry.title = "Example Movie"
        return entry

    library = {"7": (item(7, 1), "movie"), "8": (item(8, 2), "movie")}
    with patch.object(wa, "get_plex"), patch.object(wa, "_current_library_items", return_value=library), \
            patch.object(wa, "_release_date", return_value=None):
        items = {i["ratingKey"]: i for i in wa.app.test_client().get("/api/library").get_json()["items"]}
    assert items[7]["posterVersion"] == wa._poster_version("7")
    assert items[8]["posterVersion"] is None   # no original cached yet


def test_page_script_asks_for_posters_under_their_version(wa):
    script = (wa.app.static_folder + "/app.js")
    text = open(script, encoding="utf-8").read()
    assert "item.posterVersion" in text and "/api/poster/${key}${version}" in text


def test_posters_stay_behind_the_login(make_app):
    wa = make_app(AUTH_METHOD="forms", AUTH_USERNAME="admin", AUTH_PASSWORD="a-long-password", **PLEX)
    _store_original(wa)
    resp = wa.app.test_client().get(f"/api/poster/7?v={wa._poster_version('7')}")
    assert resp.status_code in (401, 302) and b"JFIF" not in resp.data
    assert "immutable" not in resp.headers.get("Cache-Control", "")
