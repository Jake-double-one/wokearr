"""Installed as a web app (home screen): manifest, icons and the meta tags
that keep the status bar dark - reachable with and without a login."""
import json


def test_manifest_and_icons_are_served_without_login(make_app):
    c = make_app(AUTH_METHOD="forms", AUTH_USERNAME="dennis", AUTH_PASSWORD="s3cret!").app.test_client()
    r = c.get("/static/manifest.json")
    assert r.status_code == 200
    manifest = json.loads(r.get_data(as_text=True))
    assert manifest["display"] == "standalone" and manifest["theme_color"] == "#1c1e22"
    for icon in manifest["icons"]:
        assert c.get(icon["src"]).status_code == 200, icon["src"]


def test_pages_declare_the_dark_theme_for_installed_web_apps(make_app):
    c = make_app(AUTH_METHOD="forms", AUTH_USERNAME="dennis", AUTH_PASSWORD="s3cret!").app.test_client()
    for path in ("/login", "/"):
        html = c.get(path, follow_redirects=True).get_data(as_text=True)
        assert '<meta name="theme-color" content="#1c1e22">' in html, path
        assert 'apple-mobile-web-app-status-bar-style" content="black-translucent"' in html, path
        assert 'rel="manifest" href="/static/manifest.json?v=' in html, path
        assert "viewport-fit=cover" in html, path


def test_login_fields_are_not_autofocused(make_app):
    """Password managers on iOS only offer saved logins on a tap, not on a
    field focused by the page itself."""
    c = make_app(AUTH_METHOD="forms", AUTH_USERNAME="dennis", AUTH_PASSWORD="s3cret!").app.test_client()
    assert "autofocus" not in c.get("/login").get_data(as_text=True)


def test_static_files_are_versioned_by_content(make_app):
    """A new image must never run a browser's cached app.js against new HTML."""
    import hashlib
    # make_app reloads one module in place, so the two configurations are checked one after the other
    for path, env in (("/", {}), ("/login", {"AUTH_METHOD": "forms", "AUTH_USERNAME": "dennis", "AUTH_PASSWORD": "s3cret!"})):
        wa = make_app(**env)
        html = wa.app.test_client().get(path).get_data(as_text=True)
        for name in ("style.css", "favicon.svg") + (("app.js",) if path == "/" else ()):
            digest = hashlib.sha256((wa.Path(wa.app.static_folder) / name).read_bytes()).hexdigest()[:10]
            assert f'/static/{name}?v={digest}' in html, (path, name)
        assert wa.app.test_client().get(f"/static/{name}?v=anything").status_code == 200
