"""Tests for business-time scheduling utilities."""

import pytest
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from utils.business_time import (
    next_business_day,
    at_time,
    compute_hitl_schedule,
    compute_delays_from_now,
)


def test_next_business_day_on_weekday():
    """Test next_business_day returns same day for weekdays."""
    tz = ZoneInfo("Europe/Berlin")
    # Monday
    dt = datetime(2024, 1, 8, 10, 0, tzinfo=tz)
    result = next_business_day(dt, tz)
    assert result.date() == dt.date()


def test_next_business_day_on_saturday():
    """Test next_business_day skips to Monday from Saturday."""
    tz = ZoneInfo("Europe/Berlin")
    # Saturday
    dt = datetime(2024, 1, 6, 10, 0, tzinfo=tz)
    result = next_business_day(dt, tz)
    # Should be Monday
    assert result.weekday() == 0
    assert result.date() == datetime(2024, 1, 8).date()


def test_next_business_day_on_sunday():
    """Test next_business_day skips to Monday from Sunday."""
    tz = ZoneInfo("Europe/Berlin")
    # Sunday
    dt = datetime(2024, 1, 7, 10, 0, tzinfo=tz)
    result = next_business_day(dt, tz)
    # Should be Monday
    assert result.weekday() == 0
    assert result.date() == datetime(2024, 1, 8).date()


def test_at_time_changes_time():
    """Test at_time changes hour/minute on same day."""
    tz = ZoneInfo("Europe/Berlin")
    dt = datetime(2024, 1, 8, 9, 30, tzinfo=tz)
    result = at_time(dt, time(14, 0), tz)
    
    assert result.date() == dt.date()
    assert result.hour == 14
    assert result.minute == 0


def test_compute_hitl_schedule_structure():
    """Test that compute_hitl_schedule returns expected structure."""
    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2024, 1, 8, 9, 0, tzinfo=tz)  # Monday morning
    
    schedule = compute_hitl_schedule(now, tz)
    
    assert "first_deadline" in schedule
    assert "first_reminder" in schedule
    assert "second_deadline" in schedule
    assert "escalation" in schedule
    assert "admin_reminder_interval" in schedule
    
    # Check times
    assert schedule["first_deadline"].hour == 10
    assert schedule["first_reminder"].hour == 10
    assert schedule["first_reminder"].minute == 1
    assert schedule["second_deadline"].hour == 14
    assert schedule["escalation"].hour == 14
    assert schedule["escalation"].minute == 1


def test_compute_hitl_schedule_same_day():
    """Test that all events are on the same business day."""
    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2024, 1, 8, 9, 0, tzinfo=tz)  # Monday morning
    
    schedule = compute_hitl_schedule(now, tz)
    
    # All should be on the same day
    first_day = schedule["first_deadline"].date()
    assert schedule["first_reminder"].date() == first_day
    assert schedule["second_deadline"].date() == first_day
    assert schedule["escalation"].date() == first_day


def test_compute_hitl_schedule_skips_weekend():
    """Test that schedule skips weekends."""
    tz = ZoneInfo("Europe/Berlin")
    # Friday afternoon after business hours
    now = datetime(2024, 1, 5, 16, 0, tzinfo=tz)
    
    schedule = compute_hitl_schedule(now, tz)
    
    # Should be on Monday
    assert schedule["first_deadline"].weekday() == 0  # Monday


def test_compute_delays_from_now():
    """Test compute_delays_from_now returns positive delays."""
    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2024, 1, 8, 9, 0, tzinfo=tz)  # Monday morning
    
    delays = compute_delays_from_now(now, tz)
    
    assert len(delays) > 0
    for delay in delays:
        assert "event" in delay
        assert "timestamp" in delay
        assert "delay_seconds" in delay
        assert delay["delay_seconds"] >= 0


def test_admin_reminder_interval():
    """Test that admin reminder interval is 24 hours."""
    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2024, 1, 8, 9, 0, tzinfo=tz)
    
    schedule = compute_hitl_schedule(now, tz)
    
    assert schedule["admin_reminder_interval"] == timedelta(hours=24)
