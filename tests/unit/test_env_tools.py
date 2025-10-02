"""Tests for environment helper utilities."""

from __future__ import annotations

import os

from utils import env_compat, env_validation


def test_apply_env_compat_promotes_legacy_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPEN_AI_KEY", "legacy-token")

    env_compat.apply_env_compat(promote_legacy=True, backfill_legacy=False)

    assert os.getenv("OPENAI_API_KEY") == "legacy-token"


def test_apply_env_compat_backfills_legacy(monkeypatch):
    monkeypatch.delenv("OPEN_AI_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "modern-token")

    env_compat.apply_env_compat(promote_legacy=False, backfill_legacy=True)

    assert os.getenv("OPEN_AI_KEY") == "modern-token"


def test_validate_environment_missing(monkeypatch, caplog):
    for key in env_validation.REQUIRED:
        monkeypatch.delenv(key, raising=False)

    result = env_validation.validate_environment(strict=False)

    assert result is True
    assert "Missing required environment variables" in caplog.text


def test_validate_environment_success(monkeypatch, caplog):
    monkeypatch.setenv("OPENAI_API_KEY", "abc")
    for key in env_validation.REQUIRED:
        if key == "__OPENAI_KEY__":
            continue
        monkeypatch.setenv(key, "value")

    assert env_validation.validate_environment(strict=True) is True
    assert "Environment validation passed" in caplog.text


def test_validate_environment_fails_strict(monkeypatch):
    for key in env_validation.REQUIRED:
        monkeypatch.delenv(key, raising=False)

    assert env_validation.validate_environment(strict=True) is False
