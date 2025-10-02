"""Tests for workflow step recorder utility."""

from __future__ import annotations

import logging

from utils.workflow_steps import WorkflowStepRecorder


def test_record_step_logs_extra_payload(caplog):
    recorder = WorkflowStepRecorder()

    with caplog.at_level(logging.INFO):
        recorded = recorder.record_step(
            "run-123",
            "event-abc",
            "normalize-data",
            extra={"details": "payload"},
        )

    assert recorded is True
    assert "Workflow log appended for run run-123 (step: normalize-data)" in caplog.text
    assert "payload" in caplog.text


def test_record_step_duplicate_is_ignored(caplog):
    recorder = WorkflowStepRecorder()

    with caplog.at_level(logging.INFO):
        assert recorder.record_step("run-dup", None, "initial") is True

    caplog.clear()

    with caplog.at_level(logging.INFO):
        assert recorder.record_step("run-dup", None, "initial") is False

    assert caplog.text == ""


def test_should_write_manifest_only_once():
    recorder = WorkflowStepRecorder()

    assert recorder.should_write_manifest("run-manifest") is True
    assert recorder.should_write_manifest("run-manifest") is False


def test_clear_run_resets_recorded_steps_and_manifest():
    recorder = WorkflowStepRecorder()

    assert recorder.record_step("run-clear", "event", "step-one") is True
    assert recorder.should_write_manifest("run-clear") is True

    recorder.clear_run("run-clear")

    assert recorder.record_step("run-clear", "event", "step-one") is True
    assert recorder.should_write_manifest("run-clear") is True
