"""NOTIFY_URL parsing - formats as in Apprise, secrets never in the display text."""
import pytest

import notify


def test_gotify_with_port_and_path():
    t = notify.parse_url("gotifys://push.example.com:8443/gotify/TOKEN123")
    assert t.endpoint == "https://push.example.com:8443/gotify/message"
    assert t.headers["X-Gotify-Key"] == "TOKEN123"
    assert "TOKEN123" not in t.display


def test_ntfy_public_topic_keeps_case():
    t = notify.parse_url("ntfy://MyTopic")
    assert t.endpoint == "https://ntfy.sh/" and t.topic == "MyTopic"
    assert "MyTopic" not in t.display


def test_ntfy_own_server_with_auth():
    t = notify.parse_url("ntfys://user:p%40ss@ntfy.example.com/alerts?token=tk_abc")
    assert t.endpoint == "https://ntfy.example.com/" and t.auth == ("user", "p@ss")
    assert t.headers["Authorization"] == "Bearer tk_abc"
    assert "p@ss" not in t.display and "tk_abc" not in t.display


@pytest.mark.parametrize("url", ["gotify://host", "ntfy://", "pushover://x/y", "https://"])
def test_invalid_urls_rejected(url):
    with pytest.raises(ValueError):
        notify.parse_url(url)
