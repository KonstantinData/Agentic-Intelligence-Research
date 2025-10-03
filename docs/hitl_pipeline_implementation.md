# Production HITL Pipeline - Implementation Guide

This document describes the production-ready Human-in-the-loop (HITL) pipeline implementation.

## Overview

The HITL pipeline now supports:
- **Production email sending** (no more simulations)
- **Business-time scheduling** with Europe/Berlin timezone support
- **IMAP inbox processing** for automatic reply handling
- **Audit tracking per request** for full traceability
- **Enhanced extraction** with German terms and EU legal forms

## Components

### 1. Extraction Agent Enhancements

**File**: `agents/extraction_agent.py`

**New Features**:
- Extended STOP_WORDS with German function/meeting terms (termin, besprechung, treffen, etc.)
- Extended COMPANY_SUFFIXES with DE/EU legal forms (KG, OHG, e.K., KGaA, SE, S.p.A., B.V., NV, AB, SAS, Oy, AS, Sp. z o.o.)
- Extended SECOND_LEVEL_TLDS for common SLD zones (co.uk, com.au, com.br, nz, za, in, jp, kr, cn, mx, ar)
- Hardened DOMAIN_REGEX against trailing punctuation
- Augmented return contract with status, missing fields, and sources

**Example**:
```python
result = await extraction_agent.extract(event)
# Returns:
# {
#   "info": {"company_name": "Example", "web_domain": "example.co.uk"},
#   "is_complete": True,
#   "status": "ok",
#   "missing": [],
#   "sources": {"company_name": "domain", "web_domain": "text"}
# }
```

### 2. Business-Time Scheduling

**File**: `utils/business_time.py`

**Functions**:
- `next_business_day(dt, tz)` - Get next business day (Mon-Fri)
- `at_time(dt, target_time, tz)` - Set time on a date
- `compute_hitl_schedule(now, tz)` - Compute full HITL schedule

**Schedule**:
- First deadline: next working day 10:00
- First reminder: 10:01 same day
- Second deadline: 14:00 same day
- Escalation: 14:01 same day
- Admin recurring reminder period: 24 hours

**Example**:
```python
from datetime import datetime
from zoneinfo import ZoneInfo
from utils.business_time import compute_hitl_schedule

now = datetime.now(ZoneInfo("Europe/Berlin"))
schedule = compute_hitl_schedule(now)
# schedule["first_deadline"], schedule["first_reminder"], etc.
```

### 3. Human-in-Loop Agent Updates

**File**: `agents/human_in_loop_agent.py`

**Changes**:
- `request_info()` now sends real emails via EmailAgent
- All subjects include audit token: `[LeadMI #xxxxx]`
- Returns `status="pending"`, `is_complete=False`, and `audit_id`
- Workflow logging for all HITL events
- Business-time reminders/escalations

**Example**:
```python
# request_info sends email and schedules reminders
result = agent.request_info(event, extracted_info)
# Returns:
# {
#   "info": {...},
#   "status": "pending",
#   "is_complete": False,
#   "audit_id": "abc123"
# }
```

### 4. Reminder/Escalation with Audit Tracking

**File**: `reminders/reminder_escalation.py`

**New Features**:
- Track scheduled tasks per audit_id in `_tasks_by_audit`
- `cancel_for_audit(audit_id)` method for selective cancellation
- Audit_id included in task metadata

**Example**:
```python
# Cancel all reminders for a specific request
reminder_escalation.cancel_for_audit(audit_id)
```

### 5. IMAP Inbox Processing

**Files**: 
- `utils/async_imap.py` - IMAP client utilities
- `agents/inbox_agent.py` - Inbox polling agent

**Configuration** (via environment variables):
```bash
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USERNAME=your-email@example.com
IMAP_PASSWORD=your-password
IMAP_USE_SSL=1
IMAP_MAILBOX=INBOX
```

**Example Usage**:
```python
from agents.inbox_agent import InboxAgent

inbox = InboxAgent()

# Register handler for replies to a specific audit_id
def handle_reply(message):
    print(f"Received reply: {message.body}")

inbox.register_reply_handler(audit_id, handle_reply)

# Poll inbox once
await inbox.poll_inbox()

# Or start continuous polling
await inbox.start_polling_loop(interval_seconds=60)
```

## Integration Flow

### Step 1: Extract Information
```python
extracted = await extraction_agent.extract(event)
```

### Step 2: Check Completeness
```python
if extracted["status"] == "incomplete":
    # Request missing info
    result = agent.request_info(event, extracted)
    audit_id = result["audit_id"]
    
    # Register reply handler
    inbox_agent.register_reply_handler(audit_id, handle_missing_info_reply)
```

### Step 3: Process Reply (Automatic)
When a reply arrives with `[LeadMI #audit_id]` in the subject:
1. Inbox agent extracts audit token
2. Calls registered handler
3. Handler continues workflow with provided information

### Step 4: Cancel Reminders (if reply received)
```python
def handle_missing_info_reply(message):
    # Parse reply
    info = parse_reply(message.body)
    
    # Cancel pending reminders for this request
    reminder_escalation.cancel_for_audit(audit_id)
    
    # Continue workflow
    continue_workflow(event, info)
```

## Testing

Run tests with:
```bash
pytest tests/unit/test_extraction_enhancements.py -v
pytest tests/unit/test_business_time.py -v
pytest tests/unit/test_async_imap.py -v
pytest tests/unit/test_reminder_audit_tracking.py -v
```

## Notes

- All times use Europe/Berlin timezone
- Weekends are automatically skipped (Friday → Monday)
- Email sending is async via `EmailAgent.send_email_async()`
- IMAP processing uses asyncio with thread-safe wrappers
- Audit tokens are case-insensitive when parsing replies
- PII masking respects `settings.mask_pii_in_messages`

## Migration from Simulation

The previous simulation code in `request_info` has been replaced with production email sending. The key differences:

**Before**:
```python
# Simulated response
extracted["info"]["company_name"] = "Example Corp"
extracted["is_complete"] = True
return extracted
```

**After**:
```python
# Send real email with audit token
subject = f"Missing info for {summary} [LeadMI #{audit_id}]"
await email_agent.send_email_async(contact_email, subject, message)

# Return pending status
return {
    "info": {...},
    "status": "pending",
    "is_complete": False,
    "audit_id": audit_id
}
```

Workflows must now handle the pending state and wait for inbox processing to continue.
