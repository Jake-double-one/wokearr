#!/usr/bin/env python3
"""
Notifications for autopilot runs - configured via NOTIFY_URL, one URL whose
scheme picks the service. The formats follow Apprise's, so they look familiar
from other self-hosted tools:

    gotify://host/TOKEN             Gotify over http
    gotifys://host/TOKEN            Gotify over https
    gotifys://host:8443/path/TOKEN  with port and/or a sub-path
    ntfy://TOPIC                    ntfy.sh (https)
    ntfy://host/TOPIC               own ntfy server over http
    ntfys://host/TOPIC              own ntfy server over https
    ntfys://user:pass@host/TOPIC    with login
    ntfys://host/TOPIC?token=tk_... with an access token
    https://example.com/hook        anything else: generic JSON webhook

Tokens, passwords and ntfy topics are never logged (see Target.display).

Test from the command line - sends one test message and says what happened:

    docker exec wokearr python notify.py
"""
import os
import sys
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlsplit

import requests

from logsetup import get_logger, setup_logging

log = get_logger("notify")

TIMEOUT = 10  # seconds - a notification must never hold up a run for long

# Per service: (normal, high) priority
_GOTIFY_PRIORITY = (5, 8)   # Gotify: 8+ plays a sound on Android
_NTFY_PRIORITY = (3, 4)     # ntfy: 3 default, 4 high


@dataclass
class Target:
    kind: str                 # "gotify" | "ntfy" | "webhook"
    endpoint: str             # URL the request goes to
    display: str              # safe to log: no token, password or topic
    headers: dict = field(default_factory=dict)
    auth: tuple | None = None
    topic: str | None = None  # ntfy only


def _mask(secret: str) -> str:
    return f"{secret[:2]}…" if len(secret) > 4 else "…"


def _host_port(parts) -> str:
    return f"{parts.hostname}:{parts.port}" if parts.port else (parts.hostname or "")


def parse_url(url: str) -> Target:
    """Raises ValueError with a readable reason for anything unusable."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    segments = [unquote(s) for s in parts.path.split("/") if s]
    query = dict(parse_qsl(parts.query))

    if scheme in ("gotify", "gotifys"):
        if not parts.hostname or not segments:
            raise ValueError("expected gotify://host/TOKEN or gotifys://host/TOKEN")
        token, base_path = segments[-1], "/".join(segments[:-1])
        http = "https" if scheme == "gotifys" else "http"
        base = f"{http}://{_host_port(parts)}" + (f"/{base_path}" if base_path else "")
        return Target(
            kind="gotify",
            endpoint=f"{base}/message",
            display=f"Gotify at {_host_port(parts)} (token {_mask(token)})",
            headers={"X-Gotify-Key": token},
        )

    if scheme in ("ntfy", "ntfys"):
        if not parts.hostname:
            raise ValueError("expected ntfy://TOPIC, ntfy://host/TOPIC or ntfys://host/TOPIC")
        if not segments:
            # ntfy://TOPIC - the public ntfy.sh server, always over https.
            # Taken from netloc, not hostname: hostname is lowercased, but
            # ntfy topics are case-sensitive
            host, topic, http = "ntfy.sh", unquote(parts.netloc.rsplit("@", 1)[-1]), "https"
        else:
            host, topic = _host_port(parts), segments[-1]
            http = "https" if scheme == "ntfys" else "http"
        target = Target(
            kind="ntfy",
            # JSON publishing goes to the server root with the topic in the
            # body - avoids HTTP headers, which can't carry non-Latin-1 text
            endpoint=f"{http}://{host}/",
            display=f"ntfy at {host} (topic {_mask(topic)})",
            topic=topic,
        )
        if parts.username:
            target.auth = (unquote(parts.username), unquote(parts.password or ""))
        if query.get("token"):
            target.headers["Authorization"] = f"Bearer {query['token']}"
        return target

    if scheme in ("http", "https"):
        if not parts.hostname:
            raise ValueError("webhook URL has no host")
        return Target(kind="webhook", endpoint=url.strip(), display=f"webhook at {_host_port(parts)}")

    raise ValueError(f"unsupported scheme {parts.scheme!r} - use gotify(s)://, ntfy(s):// or http(s)://")


def _payload(target: Target, title: str, message: str, high_priority: bool) -> dict:
    if target.kind == "gotify":
        return {
            "title": title,
            "message": message,
            "priority": _GOTIFY_PRIORITY[high_priority],
            "extras": {"client::display": {"contentType": "text/plain"}},
        }
    if target.kind == "ntfy":
        return {
            "topic": target.topic,
            "title": title,
            "message": message,
            "priority": _NTFY_PRIORITY[high_priority],
            "tags": ["warning"] if high_priority else ["clapper"],
        }
    return {
        "source": "wokearr",
        "title": title,
        "message": message,
        "priority": "high" if high_priority else "normal",
    }


def send(target: Target, title: str, message: str, high_priority: bool = False) -> bool:
    """Best-effort: logs and returns False on any failure, never raises."""
    try:
        resp = requests.post(
            target.endpoint,
            json=_payload(target, title, message, high_priority),
            headers=target.headers,
            auth=target.auth,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
    except requests.HTTPError as e:
        body = (e.response.text or "").strip().replace("\n", " ")[:200] if e.response is not None else ""
        log.warning("Notification to %s rejected: HTTP %s%s", target.display,
                    getattr(e.response, "status_code", "?"), f" - {body}" if body else "")
        return False
    except requests.RequestException as e:
        log.warning("Notification to %s failed: %s: %s", target.display, type(e).__name__, e)
        return False
    log.info("Notification sent to %s: %s", target.display, title)
    return True


def main() -> int:
    setup_logging()
    url = os.environ.get("NOTIFY_URL", "").strip()
    if not url:
        log.error("NOTIFY_URL is not set.")
        return 1
    try:
        target = parse_url(url)
    except ValueError as e:
        log.error("Invalid NOTIFY_URL: %s", e)
        return 1
    message = " ".join(sys.argv[1:]) or "Test notification - if you can read this, NOTIFY_URL works."
    return 0 if send(target, "Wokearr", message) else 1


if __name__ == "__main__":
    sys.exit(main())
