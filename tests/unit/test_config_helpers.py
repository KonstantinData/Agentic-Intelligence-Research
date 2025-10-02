"""Tests for helper functions in :mod:`config.config`."""

from __future__ import annotations

import pytest

from config import config as cfg


def test_normalise_returns_none_for_empty():
    assert cfg._normalise(None) is None
    assert cfg._normalise("") is None
    assert cfg._normalise("value") == "value"


def test_get_env_var_with_alias_precedence(monkeypatch):
    monkeypatch.setattr(cfg, "_ORIGINAL_ENV", {})
    monkeypatch.delenv("PRIMARY", raising=False)
    monkeypatch.setenv("ALIAS", "alias-value")

    result = cfg._get_env_var("PRIMARY", aliases=("ALIAS",))

    assert result == "alias-value"


def test_get_env_var_prefers_primary(monkeypatch):
    monkeypatch.setattr(cfg, "_ORIGINAL_ENV", {"PRIMARY": "from-original"})
    monkeypatch.setenv("PRIMARY", "primary-value")
    monkeypatch.setenv("ALIAS", "alias-value")

    assert cfg._get_env_var("PRIMARY", aliases=("ALIAS",)) == "primary-value"


def test_get_int_float_bool_env(monkeypatch):
    monkeypatch.setenv("INT_ENV", "5")
    monkeypatch.setenv("FLOAT_ENV", "2.5")
    monkeypatch.setenv("BOOL_TRUE", "yes")
    monkeypatch.setenv("BOOL_FALSE", "0")
    monkeypatch.setenv("BOOL_INVALID", "maybe")

    assert cfg._get_int_env("INT_ENV", 1) == 5
    assert cfg._get_float_env("FLOAT_ENV", 1.0) == pytest.approx(2.5)
    assert cfg._get_bool_env("BOOL_TRUE", False) is True
    assert cfg._get_bool_env("BOOL_FALSE", True) is False

    with pytest.raises(ValueError):
        cfg._get_bool_env("BOOL_INVALID", True)


def test_get_path_env(monkeypatch, tmp_path):
    path = tmp_path / "data"
    path.mkdir()
    monkeypatch.setenv("PATH_ENV", str(path))

    resolved = cfg._get_path_env("PATH_ENV", tmp_path)

    assert resolved == path.resolve()


def test_read_agent_config_file_json(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{\n  \"agents\": {\"polling\": \"custom\"}\n}", encoding="utf-8")

    data = cfg._read_agent_config_file(config_path)

    assert data["agents"]["polling"] == "custom"


def test_extract_agent_overrides_from_nested():
    config_data = {"agents": {"polling_agent": "poll", "human": "human-impl"}}

    overrides = cfg._extract_agent_overrides(config_data)

    assert overrides["polling"] == "poll"
    assert overrides["human"] == "human-impl"


def test_prefixed_env_mapping(monkeypatch):
    monkeypatch.setenv("SERVICE_RATE_LIMIT_EMAIL", "3")
    monkeypatch.setenv("SERVICE_RATE_LIMIT_SMS", "5")

    mapping = cfg._prefixed_env_mapping("SERVICE_RATE_LIMIT_", int)

    assert mapping == {"email": 3, "sms": 5}


def test_cast_non_empty_str():
    assert cfg._cast_non_empty_str(" value ") == "value"
    with pytest.raises(ValueError):
        cfg._cast_non_empty_str("   ")


def test_coerce_mapping(monkeypatch):
    mapping = {"Trigger": "0.8", "Extraction": "0.6"}
    coerced = cfg._coerce_mapping(mapping, float)

    assert coerced == {"trigger": 0.8, "extraction": 0.6}

    with pytest.raises(ValueError):
        cfg._coerce_mapping({"bad": ""}, cfg._cast_non_empty_str)
