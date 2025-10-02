"""Tests for :mod:`utils.duplicate_checker`."""

import pytest

from utils.duplicate_checker import DuplicateChecker


def test_duplicate_checker_detects_duplicate():
    checker = DuplicateChecker()
    assert checker.is_duplicate("evt-1", {"evt-1", "evt-2"}) is True


def test_duplicate_checker_handles_missing():
    checker = DuplicateChecker()
    assert checker.is_duplicate("evt-3", {"evt-1"}) is False


def test_duplicate_checker_logs_errors(monkeypatch):
    checker = DuplicateChecker()

    class ExplodingSet:
        def __contains__(self, item):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        checker.is_duplicate("evt-1", ExplodingSet())
