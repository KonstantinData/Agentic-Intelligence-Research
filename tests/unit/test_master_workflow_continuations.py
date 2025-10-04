from unittest.mock import AsyncMock

import pytest

from agents.master_workflow_agent import MasterWorkflowAgent


class _StubHumanAgent:
    def __init__(self):
        self.calls = []

    def request_info(self, event, extracted):
        self.calls.append((event, extracted))
        return {"status": "pending", "audit_id": "follow-up"}


@pytest.mark.asyncio
async def test_continue_after_missing_info_dispatches_when_complete():
    agent = MasterWorkflowAgent.__new__(MasterWorkflowAgent)
    agent.human_agent = _StubHumanAgent()
    agent.on_pending_audit = None
    agent._normalise_info_for_research = lambda info: info
    agent._has_research_inputs = lambda info: bool(info.get("company_name")) and bool(
        info.get("web_domain")
    )
    agent._process_crm_dispatch = AsyncMock()

    context = {"event": {"id": "evt"}, "info": {"company_name": "Acme"}, "event_id": "evt"}
    await agent.continue_after_missing_info(
        audit_id="audit",
        fields={"web_domain": "acme.com"},
        context=context,
    )

    agent._process_crm_dispatch.assert_awaited_once()
    assert not agent.human_agent.calls


@pytest.mark.asyncio
async def test_continue_after_missing_info_requests_follow_up_when_incomplete():
    agent = MasterWorkflowAgent.__new__(MasterWorkflowAgent)
    human = _StubHumanAgent()
    agent.human_agent = human
    captured = {}

    def _on_pending(kind, audit_id, ctx):
        captured.update({"kind": kind, "audit_id": audit_id, "context": ctx})

    agent.on_pending_audit = _on_pending
    agent._normalise_info_for_research = lambda info: info
    agent._has_research_inputs = lambda info: False
    agent._process_crm_dispatch = AsyncMock()

    context = {"event": {"id": "evt"}, "info": {"company_name": "Acme"}, "event_id": "evt"}
    await agent.continue_after_missing_info(
        audit_id="audit",
        fields={},
        context=context,
    )

    assert human.calls, "expected follow-up request to be triggered"
    assert captured["kind"] == "missing_info"
    assert captured["audit_id"] == "follow-up"
    agent._process_crm_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_continue_after_dossier_decision_dispatches_on_approval():
    agent = MasterWorkflowAgent.__new__(MasterWorkflowAgent)
    agent.human_agent = _StubHumanAgent()
    agent.on_pending_audit = None
    agent._normalise_info_for_research = lambda info: info
    agent._has_research_inputs = lambda info: True
    agent._process_crm_dispatch = AsyncMock()

    context = {"event": {"id": "evt"}, "info": {"company_name": "Acme", "web_domain": "acme.com"}, "event_id": "evt"}
    await agent.continue_after_dossier_decision(
        audit_id="audit",
        decision="approved",
        context=context,
    )

    agent._process_crm_dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_continue_after_dossier_decision_requests_missing_info_when_incomplete():
    agent = MasterWorkflowAgent.__new__(MasterWorkflowAgent)
    human = _StubHumanAgent()
    agent.human_agent = human
    captured = {}

    def _on_pending(kind, audit_id, ctx):
        captured.update({"kind": kind, "audit_id": audit_id, "context": ctx})

    agent.on_pending_audit = _on_pending
    agent._normalise_info_for_research = lambda info: info
    agent._has_research_inputs = lambda info: False
    agent._process_crm_dispatch = AsyncMock()

    context = {"event": {"id": "evt"}, "info": {"company_name": "Acme"}, "event_id": "evt"}
    await agent.continue_after_dossier_decision(
        audit_id="audit",
        decision="approved",
        context=context,
    )

    assert human.calls, "expected missing info request"
    assert captured["kind"] == "missing_info"
    assert captured["audit_id"] == "follow-up"
    agent._process_crm_dispatch.assert_not_called()
