import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agents.workflow_orchestrator as orchestrator_module
from agents.inbox_agent import InboxMessage
from agents.workflow_orchestrator import WorkflowOrchestrator
from config.config import settings


class _StubAuditLog:
    def __init__(self) -> None:
        self.records = []

    def record(self, **kwargs) -> None:
        self.records.append(kwargs)


class _StubReminderEscalation:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def cancel_for_audit(self, audit_id: str) -> None:
        self.cancelled.append(audit_id)


class _StubMasterAgent:
    def __init__(self) -> None:
        self.log_filename = "workflow.log"
        self.storage_agent = None
        self.audit_log = _StubAuditLog()
        self.human_agent = SimpleNamespace(
            reminder_escalation=_StubReminderEscalation()
        )
        self.continue_after_missing_info = AsyncMock()
        self.continue_after_dossier_decision = AsyncMock()
        self.on_pending_audit = None

    async def aclose(self) -> None:  # pragma: no cover - not exercised in tests
        return None


@pytest.mark.asyncio
async def test_orchestrator_processes_missing_info_reply(monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator_module, "configure_observability", lambda: None)
    monkeypatch.setattr(settings, "research_artifact_dir", tmp_path.as_posix())

    master = _StubMasterAgent()
    orchestrator = WorkflowOrchestrator(run_id="run-1", master_agent=master)

    assert callable(master.on_pending_audit)

    context = {
        "event": {"id": "evt-1"},
        "info": {"company_name": "Example"},
        "event_id": "evt-1",
        "run_id": "run-1",
    }
    master.on_pending_audit("missing_info", "audit-1", context)

    handler = orchestrator.inbox_agent._reply_handlers["audit-1"][0]
    message = InboxMessage(
        message_id="msg-1",
        subject="Re: AUDIT-audit-1",
        from_addr="human@example.com",
        body="company_name: Example Corp\nweb_domain: example.com",
    )

    await handler(message)

    awaited = master.continue_after_missing_info.await_args
    assert awaited.kwargs["audit_id"] == "audit-1"
    assert awaited.kwargs["fields"] == {
        "company_name": "Example Corp",
        "web_domain": "example.com",
    }
    assert awaited.kwargs["context"] == context

    assert master.human_agent.reminder_escalation.cancelled == ["audit-1"]

    assert len(master.audit_log.records) == 1
    normalized_payload = master.audit_log.records[0]["payload"]["normalized"]
    assert normalized_payload["type"] == "missing_info"
    assert normalized_payload["event_id"] == "evt-1"

    await handler(message)
    assert master.continue_after_missing_info.await_count == 1
    assert len(master.audit_log.records) == 1


@pytest.mark.asyncio
async def test_orchestrator_processes_dossier_reply(monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator_module, "configure_observability", lambda: None)
    monkeypatch.setattr(settings, "research_artifact_dir", tmp_path.as_posix())

    master = _StubMasterAgent()
    orchestrator = WorkflowOrchestrator(run_id="run-2", master_agent=master)

    context = {"event": {}, "info": {}, "event_id": "evt-2", "run_id": "run-2"}
    master.on_pending_audit("dossier", "audit-2", context)

    handler = orchestrator.inbox_agent._reply_handlers["audit-2"][0]
    message = InboxMessage(
        message_id="msg-2",
        subject="Re: AUDIT-audit-2",
        from_addr="human@example.com",
        body="Yes",
    )

    await handler(message)

    awaited = master.continue_after_dossier_decision.await_args
    assert awaited.kwargs["audit_id"] == "audit-2"
    assert awaited.kwargs["decision"] == "approved"
    assert awaited.kwargs["context"] == context

    assert master.human_agent.reminder_escalation.cancelled == ["audit-2"]
    assert len(master.audit_log.records) == 1
    normalized_payload = master.audit_log.records[0]["payload"]["normalized"]
    assert normalized_payload["decision"] == "approved"
    assert normalized_payload["type"] == "dossier"

    await handler(message)
    assert master.continue_after_dossier_decision.await_count == 1
    assert len(master.audit_log.records) == 1


@pytest.mark.asyncio
async def test_orchestrator_clears_resolved_state_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator_module, "configure_observability", lambda: None)
    monkeypatch.setattr(settings, "research_artifact_dir", tmp_path.as_posix())

    master = _StubMasterAgent()
    orchestrator = WorkflowOrchestrator(run_id="run-err", master_agent=master)

    context = {"event": {}, "info": {}, "event_id": "evt-err", "run_id": "run-err"}
    master.on_pending_audit("missing_info", "audit-err", context)

    handler = orchestrator.inbox_agent._reply_handlers["audit-err"][0]
    failing = AsyncMock(side_effect=RuntimeError("boom"))
    orchestrator._continue_after_reply = failing

    message = InboxMessage(
        message_id="msg-err",
        subject="Re: AUDIT-audit-err",
        from_addr="human@example.com",
        body="company_name: Example Corp",
    )

    with pytest.raises(RuntimeError):
        await handler(message)

    assert "audit-err" not in orchestrator._resolved_audits
    assert failing.await_count == 1
    assert len(master.audit_log.records) == 1

    failing.side_effect = None
    await handler(message)

    assert "audit-err" in orchestrator._resolved_audits
    assert failing.await_count == 2
    assert len(master.audit_log.records) == 2


@pytest.mark.asyncio
async def test_orchestrator_starts_polling_when_inbox_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator_module, "configure_observability", lambda: None)
    monkeypatch.setattr(settings, "research_artifact_dir", tmp_path.as_posix())
    monkeypatch.setattr(settings, "hitl_inbox_poll_seconds", 12.5)

    started = {}

    class _ConfiguredInbox:
        parse_dossier_decision = staticmethod(
            orchestrator_module.InboxAgent.parse_dossier_decision
        )
        parse_missing_info_key_values = staticmethod(
            orchestrator_module.InboxAgent.parse_missing_info_key_values
        )

        def __init__(self, **kwargs) -> None:
            started["init_kwargs"] = kwargs
            self._handlers = {}

        def register_reply_handler(self, audit_id, handler) -> None:
            self._handlers.setdefault(audit_id, []).append(handler)

        def is_configured(self) -> bool:
            return True

        async def start_polling_loop(self, interval_seconds: float):
            started["interval"] = interval_seconds
            started["loop_started"] = True

    monkeypatch.setattr(orchestrator_module, "InboxAgent", _ConfiguredInbox)

    master = _StubMasterAgent()
    WorkflowOrchestrator(run_id="run-3", master_agent=master)

    await asyncio.sleep(0)

    assert started["interval"] == 12.5
    assert started["loop_started"] is True
