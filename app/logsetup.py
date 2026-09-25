"""
Logging for the container log: one line per event with local timestamp,
level and area, full tracebacks on errors.

    2026-09-22 21:43:54 ERROR   [score-sync] Sitemap fetch failed: ...

Timestamps follow the container's time zone (TZ, see docker-compose.yaml) -
the same zone the autopilot's cron schedule is evaluated in, so log times and
schedule always agree. LOG_LEVEL (DEBUG/INFO/WARNING/ERROR, default INFO)
controls how chatty Wokearr itself is; third-party libraries (urllib3,
plexapi) stay at WARNING either way, so DEBUG shows Wokearr's per-title
details without drowning them in HTTP connection noise.
"""
import logging
import os
import sys

ROOT = "wokearr"
_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
           "WARN": logging.WARNING, "ERROR": logging.ERROR}
_configured = False


class _Formatter(logging.Formatter):
    """Shows 'score-sync' instead of the full logger name 'wokearr.score-sync'."""

    def format(self, record):
        name = record.name
        record.area = name[len(ROOT) + 1:] if name.startswith(ROOT + ".") else name
        return super().format(record)


def setup_logging() -> None:
    """Idempotent - both app.py and build_score_cache.py call it, and the
    latter also runs standalone."""
    global _configured
    if _configured:
        return
    _configured = True

    raw_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    level = _LEVELS.get(raw_level, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter("%(asctime)s %(levelname)-7s [%(area)s] %(message)s",
                                    datefmt="%Y-%m-%d %H:%M:%S"))

    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger(ROOT).setLevel(level)

    if raw_level not in _LEVELS:
        get_logger("config").warning("Unknown LOG_LEVEL=%r, using INFO. Available: DEBUG, INFO, WARNING, ERROR.",
                                     raw_level)


def get_logger(area: str) -> logging.Logger:
    return logging.getLogger(f"{ROOT}.{area}")
