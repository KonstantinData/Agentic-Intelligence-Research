import asyncio

import pytest

from reminders.reminder_escalation import ReminderEscalation


class _FakeEmailAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def send_email_async(self, recipient, subject, body):
        self.calls.append((recipient, subject, body))
        return True


@pytest.mark.asyncio
async def test_admin_recurring_reminders_cancelled_after_first_iteration():
    email_agent = _FakeEmailAgent()
    reminder = ReminderEscalation(email_agent)
    task = reminder.schedule_admin_recurring_reminders(
        "admin@example.com",
        "Subject",
        "Body",
        interval_hours=0,
        metadata={"audit_id": "audit-123"},
    )

    await asyncio.sleep(0.25)
    reminder.cancel_for_audit("audit-123")
    await asyncio.sleep(0)

    assert email_agent.calls, "expected at least one reminder to be sent"
    assert task.cancelled() or task.done()
