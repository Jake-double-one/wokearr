"""Every locale has every key, with the same placeholders as en-US."""
import json
import re
from pathlib import Path

import pytest

LOCALES = Path(__file__).resolve().parent.parent / "app" / "locales"
BASE = json.loads((LOCALES / "en-US.json").read_text(encoding="utf-8"))
placeholders = lambda text: sorted(re.findall(r"\{(\w+)\}", text))


@pytest.mark.parametrize("path", sorted(LOCALES.glob("*.json")), ids=lambda p: p.stem)
def test_locale_complete_and_consistent(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == set(BASE)
    for key, text in BASE.items():
        assert placeholders(data[key]) == placeholders(text), key
