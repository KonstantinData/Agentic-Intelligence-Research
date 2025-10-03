#!/usr/bin/env python
"""Example usage of the production HITL pipeline.

This script demonstrates how to use the enhanced extraction agent,
business-time scheduling, and IMAP inbox processing.
"""

import asyncio
import os
from datetime import datetime
from zoneinfo import ZoneInfo

# Import components
from agents.extraction_agent import ExtractionAgent
from agents.human_in_loop_agent import HumanInLoopAgent
from agents.email_agent import EmailAgent
from agents.inbox_agent import InboxAgent
from utils.business_time import compute_hitl_schedule, compute_delays_from_now


async def example_extraction():
    """Example: Enhanced extraction with sources tracking."""
    print("=== Extraction Example ===\n")
    
    agent = ExtractionAgent()
    
    # Example 1: German company with legal form
    event1 = {
        "summary": "Termin mit Siemens AG",
        "description": "Besprechung über neue Produkte",
    }
    
    result1 = await agent.extract(event1)
    print("Event 1 (German terms):")
    print(f"  Company: {result1['info']['company_name']}")
    print(f"  Status: {result1['status']}")
    print(f"  Missing: {result1['missing']}")
    print(f"  Sources: {result1['sources']}\n")
    
    # Example 2: UK domain
    event2 = {
        "summary": "Meeting",
        "description": "Visit example.co.uk for details.",
    }
    
    result2 = await agent.extract(event2)
    print("Event 2 (.co.uk domain):")
    print(f"  Company: {result2['info']['company_name']}")
    print(f"  Domain: {result2['info']['web_domain']}")
    print(f"  Status: {result2['status']}")
    print(f"  Sources: {result2['sources']}\n")


def example_business_time():
    """Example: Business-time scheduling."""
    print("=== Business-Time Scheduling Example ===\n")
    
    tz = ZoneInfo("Europe/Berlin")
    now = datetime.now(tz)
    
    print(f"Current time: {now}\n")
    
    # Compute HITL schedule
    schedule = compute_hitl_schedule(now, tz)
    
    print("HITL Schedule:")
    print(f"  First deadline: {schedule['first_deadline']}")
    print(f"  First reminder: {schedule['first_reminder']}")
    print(f"  Second deadline: {schedule['second_deadline']}")
    print(f"  Escalation: {schedule['escalation']}")
    print(f"  Admin reminder interval: {schedule['admin_reminder_interval']}\n")
    
    # Compute delays from now
    delays = compute_delays_from_now(now, tz)
    
    print("Delays from now:")
    for delay in delays:
        hours = delay['delay_seconds'] / 3600
        print(f"  {delay['event']}: {hours:.1f} hours ({delay['timestamp']})")
    print()


async def example_inbox_processing():
    """Example: IMAP inbox processing setup."""
    print("=== IMAP Inbox Processing Example ===\n")
    
    # Create inbox agent (will use env vars for config)
    inbox = InboxAgent()
    
    if not inbox.is_configured():
        print("IMAP not configured. Set these environment variables:")
        print("  - IMAP_HOST")
        print("  - IMAP_USERNAME")
        print("  - IMAP_PASSWORD")
        print("  - IMAP_PORT (optional, default: 993)")
        print("  - IMAP_USE_SSL (optional, default: 1)")
        print("  - IMAP_MAILBOX (optional, default: INBOX)\n")
        return
    
    print(f"IMAP configured:")
    print(f"  Host: {inbox.host}")
    print(f"  Port: {inbox.port}")
    print(f"  Username: {inbox.username}")
    print(f"  Mailbox: {inbox.mailbox}\n")
    
    # Example reply handler
    def handle_reply(message):
        print(f"Reply received from: {message.from_addr}")
        print(f"Subject: {message.subject}")
        print(f"Body preview: {message.body[:100]}...\n")
    
    # Register handler for a specific audit_id
    audit_id = "example-audit-123"
    inbox.register_reply_handler(audit_id, handle_reply)
    print(f"Registered reply handler for audit_id: {audit_id}\n")
    
    # Poll inbox once (don't actually connect in this example)
    print("To poll inbox, call: await inbox.poll_inbox()")
    print("To start continuous polling: await inbox.start_polling_loop(60)\n")


async def example_hitl_flow():
    """Example: Complete HITL flow with email sending."""
    print("=== HITL Flow Example ===\n")
    
    # Check if email is configured
    smtp_host = os.getenv("SMTP_HOST")
    if not smtp_host:
        print("Email not configured. Set these environment variables:")
        print("  - SMTP_HOST")
        print("  - SMTP_PORT")
        print("  - SMTP_USER")
        print("  - SMTP_PASS")
        print("  - SMTP_SENDER (optional)\n")
        print("This example will demonstrate the flow without actually sending emails.\n")
    
    # Create email agent (will use env vars)
    email_agent = EmailAgent(
        smtp_server=os.getenv("SMTP_HOST", "smtp.example.com"),
        smtp_port=int(os.getenv("SMTP_PORT", "465")),
        username=os.getenv("SMTP_USER", "user@example.com"),
        password=os.getenv("SMTP_PASS", "password"),
        sender_email=os.getenv("SMTP_SENDER", "noreply@example.com"),
    ) if smtp_host else None
    
    # Create HITL agent
    hitl_agent = HumanInLoopAgent(communication_backend=email_agent)
    
    # Example event with missing info
    event = {
        "id": "event123",
        "summary": "Meeting next week",
        "description": "Discussion about plans",
        "organizer": {
            "email": "organizer@example.com",
            "name": "John Doe",
        },
    }
    
    # Extract info
    extraction_agent = ExtractionAgent()
    extracted = await extraction_agent.extract(event)
    
    print("Extraction result:")
    print(f"  Status: {extracted['status']}")
    print(f"  Missing: {extracted['missing']}")
    print(f"  Complete: {extracted['is_complete']}\n")
    
    if extracted['status'] == 'incomplete':
        print("Info incomplete - would send email request:")
        print(f"  To: {event['organizer']['email']}")
        print(f"  Subject: Missing info for {event['summary']} [LeadMI #xxx]")
        print(f"  Would schedule reminders using business-time schedule\n")
        
        # Show what the schedule would be
        tz = ZoneInfo("Europe/Berlin")
        now = datetime.now(tz)
        schedule = compute_hitl_schedule(now, tz)
        print("Reminder schedule:")
        print(f"  First reminder: {schedule['first_reminder']}")
        print(f"  Escalation: {schedule['escalation']}\n")


async def main():
    """Run all examples."""
    await example_extraction()
    example_business_time()
    await example_inbox_processing()
    await example_hitl_flow()


if __name__ == "__main__":
    asyncio.run(main())
