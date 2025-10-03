"""Business-time scheduling helpers for Europe/Berlin timezone.

Provides utilities to compute next-business-day schedules honoring weekends
and business hours for HITL reminder/escalation workflows.
"""

from datetime import datetime, time, timedelta
from typing import Dict, List
from zoneinfo import ZoneInfo


def next_business_day(dt: datetime, tz: ZoneInfo) -> datetime:
    """Return the next business day (Mon-Fri) from the given datetime.
    
    If dt is already a weekday, returns dt itself. If it's a weekend,
    returns the following Monday at the same time.
    
    Args:
        dt: Starting datetime
        tz: Timezone to use for weekday calculation
        
    Returns:
        Next business day datetime
    """
    # Ensure we're working in the target timezone
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)
    
    # 0 = Monday, 4 = Friday, 5 = Saturday, 6 = Sunday
    weekday = dt.weekday()
    
    if weekday < 5:  # Already a weekday (Mon-Fri)
        return dt
    elif weekday == 5:  # Saturday
        return dt + timedelta(days=2)
    else:  # Sunday
        return dt + timedelta(days=1)


def at_time(dt: datetime, target_time: time, tz: ZoneInfo) -> datetime:
    """Return a datetime at the specified time on the same day.
    
    Args:
        dt: Base datetime
        target_time: Target time (e.g., time(10, 0) for 10:00)
        tz: Timezone to use
        
    Returns:
        Datetime at the target time
    """
    # Ensure we're working in the target timezone
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)
    
    return dt.replace(
        hour=target_time.hour,
        minute=target_time.minute,
        second=target_time.second,
        microsecond=target_time.microsecond
    )


def compute_hitl_schedule(now: datetime, tz: ZoneInfo = None) -> Dict[str, datetime]:
    """Compute the full HITL schedule based on business-time rules.
    
    Schedule:
    - First deadline: next working day 10:00
    - Reminder: 10:01 same day
    - Second deadline: 14:00 same day
    - Escalation: 14:01 same day
    - Admin recurring reminder period: 24 hours
    
    Args:
        now: Current datetime
        tz: Timezone (defaults to Europe/Berlin)
        
    Returns:
        Dictionary with schedule timestamps:
        - first_deadline: Next working day at 10:00
        - first_reminder: 10:01 on first deadline day
        - second_deadline: 14:00 on first deadline day
        - escalation: 14:01 on first deadline day
        - admin_reminder_interval: 24 hours (as timedelta)
    """
    if tz is None:
        tz = ZoneInfo("Europe/Berlin")
    
    # Ensure now is timezone-aware
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    
    # Find the next business day
    next_bd = next_business_day(now + timedelta(days=1), tz)
    
    # If we're past business hours today, move to next business day
    current_time = now.time()
    if now.weekday() < 5 and current_time < time(10, 0):
        # Before 10:00 on a weekday - use today
        next_bd = next_business_day(now, tz)
    
    # Set schedule times
    first_deadline = at_time(next_bd, time(10, 0), tz)
    first_reminder = at_time(next_bd, time(10, 1), tz)
    second_deadline = at_time(next_bd, time(14, 0), tz)
    escalation = at_time(next_bd, time(14, 1), tz)
    
    return {
        "first_deadline": first_deadline,
        "first_reminder": first_reminder,
        "second_deadline": second_deadline,
        "escalation": escalation,
        "admin_reminder_interval": timedelta(hours=24),
    }


def compute_delays_from_now(now: datetime, tz: ZoneInfo = None) -> List[Dict[str, any]]:
    """Compute delays in seconds from now for each HITL event.
    
    Args:
        now: Current datetime
        tz: Timezone (defaults to Europe/Berlin)
        
    Returns:
        List of dicts with 'event', 'timestamp', and 'delay_seconds' keys
    """
    schedule = compute_hitl_schedule(now, tz)
    
    events = [
        {"event": "first_reminder", "timestamp": schedule["first_reminder"]},
        {"event": "second_deadline", "timestamp": schedule["second_deadline"]},
        {"event": "escalation", "timestamp": schedule["escalation"]},
    ]
    
    result = []
    for event in events:
        delay = (event["timestamp"] - now).total_seconds()
        result.append({
            "event": event["event"],
            "timestamp": event["timestamp"],
            "delay_seconds": max(0, delay),
        })
    
    return result
