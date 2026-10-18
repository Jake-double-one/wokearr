"""Shared fixtures. app.py reads its configuration from the environment at
import time, so every test that needs a particular configuration gets a
freshly reloaded module with its own data directory."""
import importlib
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

_CONFIG_VARS = ("PLEX_URL", "PLEX_TOKEN", "AUTH_METHOD", "AUTH_USERNAME", "AUTH_PASSWORD",
                "AUTH_PASSWORD_FILE", "AUTO_SYNC_CRON", "NOTIFY_URL", "NOTIFY_ON", "LANGUAGE",
                "WOKEARR_RUN_AS_NOTICE", "PUID", "PGID")


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    """make_app(**env) -> the app module, reloaded with exactly this config."""
    def _make(**env):
        for var in _CONFIG_VARS:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        import logsetup
        logsetup._secret_values.clear()
        import build_score_cache
        importlib.reload(build_score_cache)
        import app as module
        return importlib.reload(module)
    return _make


CSRF = {"X-Requested-With": "wokearr"}


@pytest.fixture(autouse=True)
def no_internet(monkeypatch):
    """Tests never talk to the outside world - least of all isitwokeornot.com.
    Loopback stays allowed (the Plex-error tests connect to 127.0.0.1:1 on
    purpose to get a real connection error)."""
    import socket
    real_connect = socket.socket.connect

    def guarded_connect(sock, address, *args):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"test tried to reach the network: {address}")
        return real_connect(sock, address, *args)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
