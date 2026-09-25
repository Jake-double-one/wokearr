"""
Optional login in front of the whole UI and API, set with AUTH_METHOD:

  none   Default. No login, as before. Also the choice behind a proxy that
         does the authentication itself (e.g. authentik forward auth) - then
         the container port must not be reachable directly.
  basic  The browser's own login popup (HTTP Basic). Meant for authentik's
         "Send HTTP-Basic Authentication" or any other proxy that injects the
         credentials. They travel with every request, only base64 encoded -
         use it behind HTTPS.
  forms  A login page like Radarr's and Sonarr's, with a session cookie and
         "Remember me".

basic and forms share one login from the environment: AUTH_USERNAME and
AUTH_PASSWORD, or AUTH_PASSWORD_FILE for Docker secrets.

Fails closed: if basic/forms is set but the login is incomplete (or
AUTH_METHOD is unknown), every page answers 503 with the reason instead of
silently falling back to no login. /healthz stays reachable without login
(the Docker health check needs it) and reports the misconfiguration.

Also, whenever a login is active:
- Passwords are compared in constant time.
- Failed attempts are rate-limited per client address and logged in a form
  fail2ban/CrowdSec can parse:
      WARNING [auth] Failed login for user 'x' from 203.0.113.5
- Requests that change something (POST etc.) need the X-Requested-With
  header the UI sends. A foreign page can't set it without a CORS preflight,
  so it can't trigger actions with the browser's stored login (CSRF) - which
  matters most for basic, where the browser sends the credentials on its own.
"""
import datetime
import hashlib
import hmac
import math
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from flask import Response, jsonify, redirect, render_template, request, session, url_for
from flask.sessions import SecureCookieSessionInterface
from markupsafe import escape

from logsetup import get_logger

log = get_logger("auth")

METHODS = ("none", "basic", "forms")

# Rate limit: at most MAX_FAILURES failed logins per client address within
# FAILURE_WINDOW seconds; further attempts are refused until the oldest one
# ages out - about five guesses per quarter hour.
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60

REMEMBER_DAYS = 30
SESSION_COOKIE = "wokearr_session"

CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "wokearr"

# Reachable without a login: the Docker health check, the login page and the
# static files it needs.
PUBLIC_ENDPOINTS = {"healthz", "static", "login"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def load_config(environ=os.environ) -> tuple[str, str, str, str | None]:
    """Returns (method, username, password, error). error is set whenever a
    login was asked for but can't work - the caller then locks everything."""
    method = (environ.get("AUTH_METHOD", "") or "none").strip().lower()
    username = environ.get("AUTH_USERNAME", "").strip()
    password = environ.get("AUTH_PASSWORD", "")
    password_file = environ.get("AUTH_PASSWORD_FILE", "").strip()

    if method not in METHODS:
        return method, username, password, f"Unknown AUTH_METHOD={method!r}. Available: none, basic, forms."

    if password_file:
        try:
            # Only the trailing newline editors and `echo` add - spaces may be intended
            password = Path(password_file).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as e:
            if method != "none":
                return method, username, "", f"AUTH_PASSWORD_FILE={password_file!r} can't be read: {e.strerror or e}"

    if method == "none":
        return method, username, password, None
    if not username or not password:
        return method, username, password, (
            f"AUTH_METHOD={method} needs AUTH_USERNAME and AUTH_PASSWORD (or AUTH_PASSWORD_FILE)."
        )
    if ":" in username:
        # HTTP Basic separates user and password with the first colon
        return method, username, password, "AUTH_USERNAME must not contain ':'."
    return method, username, password, None


def _digest(value: str) -> bytes:
    """Fixed-length digest, so compare_digest doesn't leak the length either."""
    return hashlib.sha256(value.encode("utf-8")).digest()


def _safe_next(target: str | None) -> str:
    """Where to go after the login - only paths on this site, never another
    host (open redirect) and never back to the login itself."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or parts.path in ("/login", "/logout"):
        return "/"
    return target


class _SessionInterface(SecureCookieSessionInterface):
    """Marks the cookie Secure whenever the page is served over HTTPS -
    directly or behind a proxy that says so - and only then, so a plain-http
    LAN setup still works."""

    def get_cookie_secure(self, app):
        forwarded = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
        return request.is_secure or forwarded == "https"


class Auth:
    def __init__(self, app, data_dir: Path, translate, environ=os.environ):
        self.method, self.username, self.password, self.error = load_config(environ)
        self._login_requested_without_method = self.method == "none" and bool(
            environ.get("AUTH_USERNAME") or environ.get("AUTH_PASSWORD") or environ.get("AUTH_PASSWORD_FILE")
        )
        self.t = translate
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

        if self.method == "forms" and not self.error:
            app.secret_key = self._load_secret(data_dir / "session_secret")
            app.session_interface = _SessionInterface()
            app.config.update(
                SESSION_COOKIE_NAME=SESSION_COOKIE,
                SESSION_COOKIE_HTTPONLY=True,
                SESSION_COOKIE_SAMESITE="Lax",
                PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=REMEMBER_DAYS),
            )

        app.before_request(self._gate)
        app.add_url_rule("/login", "login", self._login, methods=["GET", "POST"])
        app.add_url_rule("/logout", "logout", self._logout)

    @property
    def active(self) -> bool:
        return self.method in ("basic", "forms") and not self.error

    def log_startup(self) -> None:
        if self.error:
            log.error("Authentication misconfigured - Wokearr stays locked until this is fixed: %s", self.error)
        elif self.method == "none":
            log.info("Authentication: none - anyone who can reach this port can use Wokearr.")
            if self._login_requested_without_method:
                log.warning("AUTH_USERNAME/AUTH_PASSWORD are set, but AUTH_METHOD is none - no login is active. "
                            "Set AUTH_METHOD=basic or AUTH_METHOD=forms.")
        else:
            log.info("Authentication: %s (user %r)", self.method, self.username)

    # -- secret for signing the session cookie --------------------------------

    @staticmethod
    def _load_secret(path: Path) -> bytes:
        """Kept in /data, so a restart doesn't log everyone out. Created once
        with owner-only permissions."""
        try:
            secret = bytes.fromhex(path.read_text(encoding="ascii").strip())
            if len(secret) >= 32:
                return secret
        except (OSError, ValueError):
            pass
        secret = secrets.token_bytes(32)
        try:
            path.unlink(missing_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(secret.hex())
        except OSError as e:
            log.warning("Could not store the session secret in %s (%s) - logins won't survive a restart.", path, e)
        return secret

    # -- credentials ----------------------------------------------------------

    def _credentials_ok(self, username: str, password: str) -> bool:
        # Both compared every time, so the answer takes as long for a wrong
        # username as for a wrong password
        user_ok = hmac.compare_digest(_digest(username), _digest(self.username))
        password_ok = hmac.compare_digest(_digest(password), _digest(self.password))
        return user_ok and password_ok

    def _session_token(self) -> str:
        """Stored in the session. Derived from the current login, so changing
        AUTH_USERNAME or AUTH_PASSWORD ends every existing session."""
        return hmac.new(_current_secret(), f"{self.username}\0{self.password}".encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def _session_valid(self) -> bool:
        # Bytes, not str: compare_digest raises on non-ASCII str values
        return hmac.compare_digest(str(session.get("auth", "")).encode("utf-8"),
                                   self._session_token().encode("utf-8"))

    # -- rate limiting --------------------------------------------------------

    @staticmethod
    def _client() -> tuple[str, str]:
        """(address to rate-limit on, readable description for the log). The
        limit uses the connecting address, which can't be faked; a proxy's
        X-Forwarded-For is only added to the log line."""
        ip = request.remote_addr or "?"
        forwarded = request.headers.get("X-Forwarded-For", "").strip()
        return ip, f"{ip} (X-Forwarded-For: {forwarded[:100]})" if forwarded else ip

    def _seconds_locked(self, ip: str) -> int:
        now = time.time()
        with self._lock:
            recent = [ts for ts in self._failures.get(ip, []) if now - ts < FAILURE_WINDOW]
            if recent:
                self._failures[ip] = recent
            else:
                self._failures.pop(ip, None)
            if len(recent) >= MAX_FAILURES:
                return int(FAILURE_WINDOW - (now - recent[0])) + 1
        return 0

    def _record_failure(self, ip: str, who: str, username: str) -> None:
        now = time.time()
        with self._lock:
            if len(self._failures) > 10000:
                # Many different addresses - drop the stale ones instead of growing forever
                self._failures = {k: v for k, v in self._failures.items() if now - v[-1] < FAILURE_WINDOW}
            attempts = self._failures.setdefault(ip, [])
            attempts.append(now)
            count = len(attempts)
        log.warning("Failed login for user %r from %s", username[:64], who)
        if count == MAX_FAILURES:
            log.warning("Too many failed logins from %s - further attempts refused for up to %d minutes.",
                        who, FAILURE_WINDOW // 60)

    def _clear_failures(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)

    def _locked_message(self, seconds: int) -> str:
        return self.t("login.error_locked", minutes=max(1, math.ceil(seconds / 60)))

    # -- the gate in front of every request -----------------------------------

    def _gate(self):
        endpoint = request.endpoint
        if endpoint == "healthz":
            return None
        if self.error:
            return None if endpoint == "static" else self._config_error_response()
        if self.method == "none" or endpoint in PUBLIC_ENDPOINTS:
            return None

        if self.method == "basic":
            denied = self._check_basic()
            if denied is not None:
                return denied
        elif not self._session_valid():
            if request.path.startswith("/api/"):
                return jsonify({"error": self.t("auth.required")}), 401
            target = request.full_path if request.query_string else request.path
            return redirect(url_for("login", next=target))

        if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            _, who = self._client()
            log.warning("Rejected %s %s from %s: %s header missing (possible cross-site request).",
                        request.method, request.path, who, CSRF_HEADER)
            return jsonify({"error": self.t("auth.csrf")}), 403
        return None

    def _check_basic(self):
        ip, who = self._client()
        credentials = request.authorization
        if credentials is None or credentials.type != "basic":
            return self._basic_challenge()
        locked = self._seconds_locked(ip)
        if locked:
            return Response(self._locked_message(locked), 429,
                            {"Retry-After": str(locked), "Content-Type": "text/plain; charset=utf-8"})
        if self._credentials_ok(credentials.username or "", credentials.password or ""):
            if ip in self._failures:
                self._clear_failures(ip)
            return None
        self._record_failure(ip, who, credentials.username or "")
        return self._basic_challenge()

    def _basic_challenge(self):
        return Response(self.t("auth.required"), 401, {
            "WWW-Authenticate": 'Basic realm="Wokearr", charset="UTF-8"',
            "Content-Type": "text/plain; charset=utf-8",
        })

    def _config_error_response(self):
        if request.path.startswith("/api/"):
            return jsonify({"error": f"{self.t('auth.config_error_title')}: {self.error}"}), 503
        page = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Wokearr</title></head>
<body style="background:#1c1e22;color:#e8e9eb;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
padding:40px 16px;text-align:center">
<h1 style="font-size:20px;color:#d72d20">{escape(self.t('auth.config_error_title'))}</h1>
<p><code>{escape(self.error)}</code></p>
<p style="color:#9aa0a6">{escape(self.t('auth.config_error_hint'))}</p>
</body></html>"""
        return Response(page, 503, {"Content-Type": "text/html; charset=utf-8"})

    # -- login page (forms) ---------------------------------------------------

    def _login(self):
        if self.method != "forms" or self.error:
            return redirect("/")
        next_url = _safe_next(request.values.get("next"))
        if request.method == "GET":
            if self._session_valid():
                return redirect(next_url)
            return render_template("login.html", error=None, next=next_url, username="")

        ip, who = self._client()
        username = request.form.get("username", "")
        locked = self._seconds_locked(ip)
        if locked:
            return render_template("login.html", error=self._locked_message(locked), next=next_url,
                                   username=username), 429
        if not self._credentials_ok(username, request.form.get("password", "")):
            self._record_failure(ip, who, username)
            locked = self._seconds_locked(ip)
            error = self._locked_message(locked) if locked else self.t("login.error_invalid")
            return render_template("login.html", error=error, next=next_url, username=username), 401

        self._clear_failures(ip)
        session.clear()
        session["auth"] = self._session_token()
        session.permanent = bool(request.form.get("remember"))
        log.info("Login: user %r from %s%s", username, who, " (remembered)" if session.permanent else "")
        return redirect(next_url)

    def _logout(self):
        if self.method != "forms" or self.error:
            return redirect("/")
        if self._session_valid():
            _, who = self._client()
            log.info("Logout from %s", who)
        session.clear()
        return redirect(url_for("login"))


def _current_secret() -> bytes:
    from flask import current_app
    key = current_app.secret_key
    return key if isinstance(key, bytes) else str(key).encode("utf-8")
