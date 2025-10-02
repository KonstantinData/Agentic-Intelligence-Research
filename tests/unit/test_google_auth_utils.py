"""Tests for :mod:`utils.google_auth`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from utils import google_auth


class DummyCreds:
    def __init__(self, *, token: str | None, valid: bool, refresh_token: str | None):
        self.token = token
        self.valid = valid
        self.refresh_token = refresh_token
        self.refreshed_with = None

    def refresh(self, request):
        self.refreshed_with = request
        if self.refresh_token:
            self.token = "refreshed-token"


def test_ensure_access_token_returns_existing():
    creds = DummyCreds(token="present", valid=True, refresh_token=None)

    token = google_auth.ensure_access_token(creds)  # type: ignore[arg-type]

    assert token == "present"
    assert creds.refreshed_with is None


def test_ensure_access_token_refreshes(monkeypatch):
    creds = DummyCreds(token=None, valid=False, refresh_token="refresh")
    sentinel = SimpleNamespace()

    monkeypatch.setattr(google_auth, "Request", lambda: sentinel)

    token = google_auth.ensure_access_token(creds)  # type: ignore[arg-type]

    assert token == "refreshed-token"
    assert creds.refreshed_with is sentinel


def test_ensure_access_token_raises_without_token(monkeypatch):
    creds = DummyCreds(token=None, valid=False, refresh_token=None)
    monkeypatch.setattr(google_auth, "Request", lambda: None)

    with pytest.raises(RuntimeError):
        google_auth.ensure_access_token(creds)  # type: ignore[arg-type]


def test_auth_header_formats_token():
    assert google_auth.auth_header("tok") == {"Authorization": "Bearer tok"}
