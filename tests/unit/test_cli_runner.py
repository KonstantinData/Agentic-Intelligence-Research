"""Tests for :mod:`utils.cli_runner`."""

from __future__ import annotations

import asyncio
import types
import sys

import pytest

from utils import cli_runner


async def _async_marker() -> str:
    return "ok"


def test_run_cli_executes_async_function():
    executed = cli_runner.run_cli(lambda: _async_marker())
    assert executed == "ok"


def test_resolve_entrypoint_roundtrip(monkeypatch):
    module = types.ModuleType("dummy_async_module")

    async def entrypoint() -> str:  # pragma: no cover - executed via resolution
        return "from module"

    module.entrypoint = entrypoint
    monkeypatch.setitem(sys.modules, module.__name__, module)

    resolved = cli_runner._resolve_entrypoint(f"{module.__name__}:entrypoint")
    assert resolved is entrypoint


def test_resolve_entrypoint_requires_separator():
    with pytest.raises(ValueError):
        cli_runner._resolve_entrypoint("missing.separator")


def test_resolve_entrypoint_requires_coroutine(monkeypatch):
    module = types.ModuleType("dummy_sync_module")

    def not_async():
        return None

    module.entrypoint = not_async
    monkeypatch.setitem(sys.modules, module.__name__, module)

    with pytest.raises(TypeError):
        cli_runner._resolve_entrypoint(f"{module.__name__}:entrypoint")


def test_parse_args_extracts_entrypoint(monkeypatch):
    argv = ["package.module:call"]
    assert cli_runner._parse_args(argv) == "package.module:call"


def test_main_invokes_run_cli(monkeypatch):
    captured = {}

    async def entrypoint():
        captured["ran"] = True

    module = types.ModuleType("main_target")
    module.entrypoint = entrypoint
    monkeypatch.setitem(sys.modules, module.__name__, module)

    def fake_run_cli(factory):
        captured["factory"] = factory
        return asyncio.run(factory())

    monkeypatch.setattr(cli_runner, "run_cli", fake_run_cli)

    cli_runner.main([f"{module.__name__}:entrypoint"])

    assert captured["ran"] is True
    assert captured["factory"].__name__ == "entrypoint"


def test_main_propagates_resolution_error(monkeypatch):
    monkeypatch.setattr(cli_runner, "_parse_args", lambda argv: "module:call")
    monkeypatch.setattr(
        cli_runner,
        "_resolve_entrypoint",
        lambda spec: (_ for _ in ()).throw(ValueError("boom")),
    )

    with pytest.raises(ValueError):
        cli_runner.main([])
