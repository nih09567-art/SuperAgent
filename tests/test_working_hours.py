from datetime import datetime

from src.security.working_hours import is_within_working_hours


def test_configured_working_hours_are_start_inclusive_end_exclusive(monkeypatch):
    monkeypatch.setenv("S_ABAC_WORKING_HOURS_START", "08:00")
    monkeypatch.setenv("S_ABAC_WORKING_HOURS_END", "22:00")

    assert is_within_working_hours(datetime(2026, 8, 11, 8, 0)) is True
    assert is_within_working_hours(datetime(2026, 8, 11, 21, 59)) is True
    assert is_within_working_hours(datetime(2026, 8, 11, 22, 0)) is False


def test_overnight_working_hours_are_supported(monkeypatch):
    monkeypatch.setenv("S_ABAC_WORKING_HOURS_START", "22:00")
    monkeypatch.setenv("S_ABAC_WORKING_HOURS_END", "06:00")

    assert is_within_working_hours(datetime(2026, 8, 11, 23, 0)) is True
    assert is_within_working_hours(datetime(2026, 8, 12, 5, 59)) is True
    assert is_within_working_hours(datetime(2026, 8, 12, 12, 0)) is False
