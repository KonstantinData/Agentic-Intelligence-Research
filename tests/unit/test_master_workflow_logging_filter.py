"""Tests for the master workflow logging filter."""

from __future__ import annotations

import gc
import logging

from agents.master_workflow_agent import _RunIdLoggingFilter


class DummyAgent:
    def __init__(self, run_id: str | None):
        self.run_id = run_id


def _make_record() -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="message",
        args=(),
        exc_info=None,
    )


def test_filter_populates_run_id_when_missing():
    agent = DummyAgent("run-123")
    filt = _RunIdLoggingFilter(agent)
    record = _make_record()

    assert "run_id" not in record.__dict__
    filt.filter(record)
    assert record.run_id == "run-123"


def test_filter_preserves_existing_run_id():
    agent = DummyAgent("run-456")
    filt = _RunIdLoggingFilter(agent)
    record = _make_record()
    record.run_id = "existing"

    filt.filter(record)

    assert record.run_id == "existing"


def test_filter_handles_missing_agent(monkeypatch):
    agent = DummyAgent("run-789")
    filt = _RunIdLoggingFilter(agent)
    record = _make_record()

    del agent
    gc.collect()

    filt.filter(record)

    assert record.run_id == "n/a"
