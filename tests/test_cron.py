"""AUTO_SYNC_CRON: a schedule on a day that never exists is read as the last
day of the month; everything that does fire stays as written."""
import datetime
import logging

import pytest
from croniter import croniter


@pytest.mark.parametrize("configured, effective", [
    ("0 0 30 2 *", "0 0 L 2 *"),
    ("0 0 31 2 *", "0 0 L 2 *"),
    ("0 0 30,31 2 *", "0 0 L 2 *"),
    ("30 4 31 4,6,9,11 *", "30 4 L 4,6,9,11 *"),
])
def test_impossible_day_becomes_last_day_of_month(make_app, configured, effective):
    app = make_app()
    assert app._resolve_cron(configured) == effective


@pytest.mark.parametrize("expression", [
    "0 * * * *", "*/15 * * * *", "0 7-23 * * *", "@daily", "0 3 L * *",
    "0 0 31 * *",   # only the months with 31 days - cron's own meaning
    "0 0 29 2 *",   # leap years only
    "0 0 15,30 2 *",  # the 15th exists
])
def test_schedules_that_fire_stay_as_written(make_app, expression):
    app = make_app()
    assert app._resolve_cron(expression) == expression


def test_last_day_of_february_follows_leap_years(make_app):
    app = make_app()
    it = croniter(app._resolve_cron("0 0 30 2 *"), datetime.datetime(2026, 11, 15))
    runs = [it.get_next(datetime.datetime).date().isoformat() for _ in range(3)]
    assert runs == ["2027-02-28", "2028-02-29", "2029-02-28"]


def test_startup_uses_the_corrected_schedule_and_says_so(make_app, caplog):
    with caplog.at_level(logging.WARNING, logger="wokearr"):
        app = make_app(AUTO_SYNC_CRON="0 0 30 2 *")
    assert app.AUTO_SYNC_CRON == "0 0 L 2 *"
    assert any("last day of the month" in r.getMessage() and "0 0 30 2 *" in r.getMessage()
               for r in caplog.records)


def test_ordinary_schedule_starts_without_a_warning(make_app, caplog):
    with caplog.at_level(logging.WARNING, logger="wokearr"):
        app = make_app(AUTO_SYNC_CRON="0 * * * *")
    assert app.AUTO_SYNC_CRON == "0 * * * *"
    assert not [r for r in caplog.records if "AUTO_SYNC_CRON" in r.getMessage()]
