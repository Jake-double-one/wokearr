"""Optional login (AUTH_METHOD none/basic/forms) - see app/auth.py."""
import base64
import os
import stat

import pytest

from conftest import CSRF


def basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def login(c, **form):
    """Posts the login form the way the browser does: GET it first, send its token back."""
    html = c.get("/login").get_data(as_text=True)
    token = html.split('name="csrf" value="')[1].split('"')[0]
    return c.post("/login", data={"csrf": token, **form})


def test_none_is_open(make_app):
    c = make_app().app.test_client()
    assert c.get("/").status_code == 200
    assert c.get("/healthz").get_json() == {"status": "ok"}


def test_basic(make_app):
    c = make_app(AUTH_METHOD="basic", AUTH_USERNAME="dennis", AUTH_PASSWORD="pässwörd 1").app.test_client()
    r = c.get("/")
    assert r.status_code == 401 and r.headers["WWW-Authenticate"].startswith('Basic realm="Wokearr"')
    assert c.get("/", headers=basic("dennis", "wrong")).status_code == 401
    assert c.get("/", headers=basic("dennis", "pässwörd 1")).status_code == 200
    assert c.get("/healthz").status_code == 200
    assert c.get("/favicon.ico").status_code == 200
    assert c.post("/api/sync-library", headers=basic("dennis", "pässwörd 1")).status_code == 403
    assert c.post("/api/sync-library", headers={**basic("dennis", "pässwörd 1"), **CSRF}).status_code == 400


def test_basic_lockout_per_address(make_app):
    c = make_app(AUTH_METHOD="basic", AUTH_USERNAME="dennis", AUTH_PASSWORD="right-one").app.test_client()
    for _ in range(5):
        c.get("/", headers=basic("x", "guess"), environ_base={"REMOTE_ADDR": "203.0.113.5"})
    r = c.get("/", headers=basic("dennis", "right-one"), environ_base={"REMOTE_ADDR": "203.0.113.5"})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    assert c.get("/", headers=basic("dennis", "right-one"), environ_base={"REMOTE_ADDR": "10.0.0.9"}).status_code == 200


def test_forms_flow(make_app):
    c = make_app(AUTH_METHOD="forms", AUTH_USERNAME="dennis", AUTH_PASSWORD="s3cret!").app.test_client()
    r = c.get("/?x=1")
    assert r.status_code == 302 and r.headers["Location"].startswith("/login?next=")
    assert c.get("/api/library").status_code == 401
    assert login(c, username="dennis", password="nope").status_code == 401
    r = login(c, username="dennis", password="s3cret!", next="/?x=1")
    assert r.status_code == 302 and r.headers["Location"] == "/?x=1"
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Expires" not in cookie
    assert c.get("/").status_code == 200
    assert c.get("/logout").status_code == 302 and c.get("/").status_code == 200, "GET never logs out"
    assert c.post("/logout").status_code == 302
    assert c.get("/").status_code == 302


def test_forms_login_needs_the_forms_own_token(make_app):
    """A foreign page can't post logins from a visitor's browser - neither to
    guess the password nor to lock the visitor's address out."""
    c = make_app(AUTH_METHOD="forms", AUTH_USERNAME="dennis", AUTH_PASSWORD="s3cret!").app.test_client()
    for _ in range(6):
        r = c.post("/login", data={"username": "dennis", "password": "wrong"})
        assert r.status_code == 400
    assert login(c, username="dennis", password="s3cret!").status_code == 302, "not locked out"


def test_trusted_proxies_rate_limit_the_client_behind_the_proxy(make_app):
    """Behind a reverse proxy, the lockout counts the client in X-Forwarded-For
    - but only when the request really comes from a listed proxy."""
    wa = make_app(AUTH_METHOD="basic", AUTH_USERNAME="dennis", AUTH_PASSWORD="right-one",
                  TRUSTED_PROXIES="172.18.0.0/16, 10.0.0.5")
    c = wa.app.test_client()
    via_proxy = {"REMOTE_ADDR": "172.18.0.2"}
    for _ in range(5):
        c.get("/", headers={**basic("x", "guess"), "X-Forwarded-For": "203.0.113.5"}, environ_base=via_proxy)
    locked = c.get("/", headers={**basic("dennis", "right-one"), "X-Forwarded-For": "203.0.113.5"}, environ_base=via_proxy)
    assert locked.status_code == 429
    other = c.get("/", headers={**basic("dennis", "right-one"), "X-Forwarded-For": "203.0.113.6"}, environ_base=via_proxy)
    assert other.status_code == 200, "another client behind the same proxy is not locked out"
    # A chain: the client's own (fake) entry on the left, the real one appended by the proxy
    chain = c.get("/", headers={**basic("dennis", "right-one"), "X-Forwarded-For": "1.2.3.4, 203.0.113.5, 10.0.0.5"},
                  environ_base=via_proxy)
    assert chain.status_code == 429, "the rightmost non-proxy address is the client"
    # Not from a trusted proxy: the header is ignored, the connecting address counts
    direct = {"REMOTE_ADDR": "198.51.100.9"}
    assert c.get("/", headers={**basic("dennis", "right-one"), "X-Forwarded-For": "203.0.113.5"},
                 environ_base=direct).status_code == 200


def test_without_trusted_proxies_x_forwarded_for_is_ignored(make_app):
    c = make_app(AUTH_METHOD="basic", AUTH_USERNAME="dennis", AUTH_PASSWORD="right-one").app.test_client()
    for i in range(5):
        c.get("/", headers={**basic("x", "guess"), "X-Forwarded-For": f"203.0.113.{i}"},
              environ_base={"REMOTE_ADDR": "172.18.0.2"})
    r = c.get("/", headers={**basic("dennis", "right-one"), "X-Forwarded-For": "203.0.113.99"},
              environ_base={"REMOTE_ADDR": "172.18.0.2"})
    assert r.status_code == 429, "all of them count as the connecting address"


def test_invalid_trusted_proxy_entries_are_skipped(make_app):
    wa = make_app(TRUSTED_PROXIES="10.0.0.5, not-an-ip, 300.1.1.1")
    assert [str(n) for n in wa.auth.trusted_proxies] == ["10.0.0.5/32"]
    assert wa.auth._rejected_proxies == ["not-an-ip", "300.1.1.1"]


@pytest.mark.parametrize("target", ["//evil.example/", "https://evil.example/", "/\\evil.example", "javascript:alert(1)", "/login"])
def test_forms_no_open_redirect(make_app, target):
    c = make_app(AUTH_METHOD="forms", AUTH_USERNAME="dennis", AUTH_PASSWORD="s3cret!").app.test_client()
    r = login(c, username="dennis", password="s3cret!", next=target)
    assert r.headers["Location"] == "/"


def test_forms_password_change_ends_sessions(make_app, tmp_path):
    wa = make_app(AUTH_METHOD="forms", AUTH_USERNAME="dennis", AUTH_PASSWORD="first-password")
    c = wa.app.test_client()
    login(c, username="dennis", password="first-password")
    cookie = c.get_cookie("wokearr_session").value
    secret = tmp_path / "data" / "session_secret"
    assert stat.S_IMODE(os.stat(secret).st_mode) == 0o600
    wa = make_app(AUTH_METHOD="forms", AUTH_USERNAME="dennis", AUTH_PASSWORD="first-password")
    c2 = wa.app.test_client(); c2.set_cookie("wokearr_session", cookie)
    assert c2.get("/").status_code == 200, "a restart keeps the session"
    wa = make_app(AUTH_METHOD="forms", AUTH_USERNAME="dennis", AUTH_PASSWORD="second-password")
    c3 = wa.app.test_client(); c3.set_cookie("wokearr_session", cookie)
    assert c3.get("/").status_code == 302, "a new password ends it"


@pytest.mark.parametrize("env", [
    {"AUTH_METHOD": "forms", "AUTH_USERNAME": "dennis"},
    {"AUTH_METHOD": "external", "AUTH_USERNAME": "a", "AUTH_PASSWORD": "b"},
    {"AUTH_METHOD": "basic", "AUTH_USERNAME": "a:b", "AUTH_PASSWORD": "c"},
    {"AUTH_METHOD": "basic", "AUTH_USERNAME": "a", "AUTH_PASSWORD_FILE": "/nonexistent/pw"},
])
def test_misconfiguration_fails_closed(make_app, env):
    c = make_app(**env).app.test_client()
    assert c.get("/").status_code == 503
    assert c.get("/api/library").status_code == 503
    h = c.get("/healthz")
    assert h.status_code == 503 and h.get_json()["auth"] == "misconfigured"
