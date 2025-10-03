"""Tests for reminder/escalation audit tracking."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from reminders.reminder_escalation import ReminderEscalation


@pytest.mark.asyncio
async def test_cancel_for_audit_removes_only_matching_tasks():
    """Test that cancel_for_audit only cancels tasks for specific audit_id."""
    email_agent = MagicMock()
    email_agent.send_email_async = AsyncMock(return_value=True)
    
    reminder = ReminderEscalation(email_agent)
    
    # Schedule tasks with different audit_ids
    task1 = reminder.schedule_reminder(
        "test1@example.com",
        "Subject 1",
        "Body 1",
        0.1,
        metadata={"audit_id": "audit-1"}
    )
    
    task2 = reminder.schedule_reminder(
        "test2@example.com",
        "Subject 2",
        "Body 2",
        0.1,
        metadata={"audit_id": "audit-2"}
    )
    
    # Give tasks time to be registered
    await asyncio.sleep(0.01)
    
    # Cancel only audit-1
    reminder.cancel_for_audit("audit-1")
    
    # task1 should be cancelled, task2 should not
    assert task1.cancelled()
    assert not task2.cancelled()
    
    # Cleanup
    reminder.cancel_pending()


@pytest.mark.asyncio
async def test_cancel_for_audit_with_nonexistent_id():
    """Test that cancel_for_audit handles non-existent audit_id gracefully."""
    email_agent = MagicMock()
    reminder = ReminderEscalation(email_agent)
    
    # Should not raise an error
    reminder.cancel_for_audit("nonexistent-audit-id")


@pytest.mark.asyncio
async def test_tasks_tracked_by_audit_id():
    """Test that tasks are properly tracked by audit_id."""
    email_agent = MagicMock()
    email_agent.send_email_async = AsyncMock(return_value=True)
    
    reminder = ReminderEscalation(email_agent)
    
    # Schedule task with audit_id
    task = reminder.schedule_reminder(
        "test@example.com",
        "Subject",
        "Body",
        0.1,
        metadata={"audit_id": "test-audit"}
    )
    
    # Check that task is tracked
    assert "test-audit" in reminder._tasks_by_audit
    assert task in reminder._tasks_by_audit["test-audit"]
    
    # Cleanup
    reminder.cancel_pending()


@pytest.mark.asyncio
async def test_cancel_pending_clears_audit_tracking():
    """Test that cancel_pending clears all audit tracking."""
    email_agent = MagicMock()
    email_agent.send_email_async = AsyncMock(return_value=True)
    
    reminder = ReminderEscalation(email_agent)
    
    # Schedule tasks with audit_ids
    reminder.schedule_reminder(
        "test1@example.com",
        "Subject",
        "Body",
        0.1,
        metadata={"audit_id": "audit-1"}
    )
    
    reminder.schedule_reminder(
        "test2@example.com",
        "Subject",
        "Body",
        0.1,
        metadata={"audit_id": "audit-2"}
    )
    
    # Give tasks time to be registered
    await asyncio.sleep(0.01)
    
    # Cancel all
    reminder.cancel_pending()
    
    # All audit tracking should be cleared
    assert len(reminder._tasks_by_audit) == 0
    assert len(reminder._tasks) == 0
