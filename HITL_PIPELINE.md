# Production HITL Pipeline - Quick Reference

## What Changed?

This implementation removes all simulations and dummy data from the HITL pipeline, replacing them with production-ready email sending, IMAP inbox processing, and business-time scheduling.

## Key Files Modified

### Core Changes
1. **`agents/extraction_agent.py`** - Enhanced extraction with German terms, EU legal forms, and .co.uk domain support
2. **`agents/human_in_loop_agent.py`** - Replaced simulations with real email sending and business-time reminders
3. **`reminders/reminder_escalation.py`** - Added audit-ID tracking for selective reminder cancellation

### New Files
1. **`utils/business_time.py`** - Business-time scheduling for Europe/Berlin timezone
2. **`agents/inbox_agent.py`** - IMAP inbox processing agent
3. **`utils/async_imap.py`** - Async IMAP utilities

### Documentation
1. **`docs/hitl_pipeline_implementation.md`** - Complete implementation guide
2. **`examples/hitl_pipeline_example.py`** - Usage examples

### Tests
1. **`tests/unit/test_extraction_enhancements.py`** - Extraction tests
2. **`tests/unit/test_business_time.py`** - Business-time tests
3. **`tests/unit/test_async_imap.py`** - IMAP tests
4. **`tests/unit/test_reminder_audit_tracking.py`** - Reminder tests

## Quick Start

### 1. Configure Email (SMTP)
```bash
export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=465
export SMTP_USER=your-email@example.com
export SMTP_PASS=your-password
export SMTP_SENDER=noreply@example.com
```

### 2. Configure Inbox (IMAP)
```bash
export IMAP_HOST=imap.gmail.com
export IMAP_PORT=993
export IMAP_USERNAME=your-email@example.com
export IMAP_PASSWORD=your-password
export IMAP_MAILBOX=INBOX
```

### 3. Use Enhanced Extraction
```python
from agents.extraction_agent import ExtractionAgent

agent = ExtractionAgent()
result = await agent.extract(event)

# Returns:
# {
#   "info": {"company_name": "...", "web_domain": "..."},
#   "status": "ok" | "incomplete",
#   "missing": [...],
#   "sources": {"company_name": "event|domain|text|none", ...}
# }
```

### 4. Request Missing Info
```python
from agents.human_in_loop_agent import HumanInLoopAgent

hitl = HumanInLoopAgent(communication_backend=email_agent)
result = hitl.request_info(event, extracted)

# Sends email with subject: "Missing info for Event [LeadMI #abc123]"
# Returns: {"status": "pending", "is_complete": False, "audit_id": "abc123"}
# Schedules business-time reminders automatically
```

### 5. Process Replies
```python
from agents.inbox_agent import InboxAgent

inbox = InboxAgent()

# Register handler
def handle_reply(message):
    # Parse reply and continue workflow
    info = parse_reply(message.body)
    reminder_escalation.cancel_for_audit(audit_id)
    continue_workflow(event, info)

inbox.register_reply_handler(audit_id, handle_reply)

# Poll once
await inbox.poll_inbox()

# Or poll continuously
await inbox.start_polling_loop(interval_seconds=60)
```

## What Was Removed?

### Before (Simulation)
```python
# Old request_info in human_in_loop_agent.py
print("Please provide missing info...")
extracted["info"]["company_name"] = "Example Corp"  # Dummy data!
extracted["info"]["web_domain"] = "example.com"     # Dummy data!
extracted["is_complete"] = True
return extracted
```

### After (Production)
```python
# New request_info
subject = f"Missing info for {summary} [LeadMI #{audit_id}]"
await email_agent.send_email_async(contact_email, subject, message)
schedule_business_time_reminders(audit_id, contact, ...)
return {"status": "pending", "is_complete": False, "audit_id": audit_id}
```

## Business-Time Schedule

All reminders follow Europe/Berlin business hours (Mon-Fri):

- **First deadline**: Next working day 10:00
- **First reminder**: Same day 10:01
- **Second deadline**: Same day 14:00
- **Escalation**: Same day 14:01
- **Admin reminders**: Every 24 hours after escalation

Weekends are automatically skipped (Friday requests → Monday deadline).

## Extraction Enhancements

### German Terms Support
- **Stop words**: termin, besprechung, treffen, gespräch, austausch, telko
- **Legal forms**: AG, GmbH, KG, OHG, e.K., KGaA

### EU Legal Forms
- **Germany**: KG, OHG, e.K., KGaA, AG, GmbH
- **Netherlands**: B.V., NV
- **Sweden**: AB
- **France**: SAS, S.A.
- **Finland**: Oy
- **Norway**: AS
- **Poland**: Sp. z o.o.
- **Italy**: S.p.A.

### Domain Handling
- **Second-level TLDs**: .co.uk, .com.au, .com.br, etc.
- **Punycode/IDN**: Automatic normalization
- **Trailing punctuation**: Automatically removed

### Example
```python
# Input: "Visit example.co.uk for details"
# Output: company_name="Example", web_domain="example.co.uk"
```

## Testing

Run all tests:
```bash
pytest tests/unit/test_extraction_enhancements.py -v
pytest tests/unit/test_business_time.py -v
pytest tests/unit/test_async_imap.py -v
pytest tests/unit/test_reminder_audit_tracking.py -v
```

Manual verification:
```bash
python examples/hitl_pipeline_example.py
```

## Migration Checklist

- [ ] Configure SMTP environment variables
- [ ] Configure IMAP environment variables
- [ ] Update workflow code to handle `status="pending"` responses
- [ ] Implement reply handlers for inbox processing
- [ ] Test with real email accounts (use test accounts first!)
- [ ] Monitor audit logs for tracking pending requests
- [ ] Set up escalation recipients (optional)

## Support

For detailed information, see:
- **Implementation Guide**: `docs/hitl_pipeline_implementation.md`
- **Example Script**: `examples/hitl_pipeline_example.py`
- **Test Suite**: `tests/unit/test_*.py`

All simulations have been removed. The pipeline now requires proper email configuration to function.
