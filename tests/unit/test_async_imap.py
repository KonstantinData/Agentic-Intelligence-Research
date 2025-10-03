"""Tests for IMAP inbox processing."""

import pytest
from unittest.mock import MagicMock

from utils.async_imap import extract_audit_token_from_subject, decode_email_header


def test_extract_audit_token_from_subject_standard():
    """Test extracting audit token from standard format."""
    subject = "Missing info for Meeting [LeadMI #12345]"
    token = extract_audit_token_from_subject(subject)
    assert token == "12345"


def test_extract_audit_token_from_subject_with_spaces():
    """Test extracting audit token with extra spaces."""
    subject = "Dossier confirmation [LeadMI # abc-123 ]"
    token = extract_audit_token_from_subject(subject)
    assert token == "abc-123"


def test_extract_audit_token_case_insensitive():
    """Test that extraction is case-insensitive."""
    subject = "Re: Request [leadmi #xyz789]"
    token = extract_audit_token_from_subject(subject)
    assert token == "xyz789"


def test_extract_audit_token_no_match():
    """Test that None is returned when no token is found."""
    subject = "Regular email without token"
    token = extract_audit_token_from_subject(subject)
    assert token is None


def test_extract_audit_token_partial_match():
    """Test that partial matches are rejected."""
    subject = "Email with [LeadMI but no closing bracket"
    token = extract_audit_token_from_subject(subject)
    assert token is None


def test_decode_email_header_plain():
    """Test decoding plain ASCII header."""
    header = "Test Subject"
    result = decode_email_header(header)
    assert result == "Test Subject"


def test_decode_email_header_empty():
    """Test decoding empty header."""
    result = decode_email_header("")
    assert result == ""


def test_decode_email_header_none():
    """Test decoding None header."""
    result = decode_email_header(None)
    assert result == ""
