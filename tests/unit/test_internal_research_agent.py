"""Unit tests for :mod:`agents.internal_research_agent`."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from agents.internal_research_agent import InternalResearchAgent


class DummyWorkflowLogManager:
    def __init__(self) -> None:
        self.records = []

    def append_log(self, run_id, step, message, **kwargs):  # type: ignore[override]
        self.records.append({
            "run_id": run_id,
            "step": step,
            "message": message,
            **kwargs,
        })


class DummyEmailAgent:
    def __init__(self) -> None:
        self.calls = []

    async def send_email_async(self, recipient, subject, body, html_body=None, *, attachments=None, attachment_links=None):  # noqa: D401,E501
        self.calls.append({
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "html": html_body,
            "attachments": attachments,
            "links": attachment_links,
        })
        return True


def _make_agent(tmp_path, *, email_agent=None, runner=None):
    config = SimpleNamespace(
        workflow_log_dir=tmp_path / "workflow",
        research_artifact_dir=tmp_path / "research",
        agent_log_dir=tmp_path / "agent_logs",
        crm_attachment_base_url="https://crm.example",
    )
    for attr in ("workflow_log_dir", "research_artifact_dir", "agent_log_dir"):
        getattr(config, attr).mkdir(parents=True, exist_ok=True)

    return InternalResearchAgent(
        config=config,
        workflow_log_manager=DummyWorkflowLogManager(),
        email_agent=email_agent,
        internal_search_runner=runner or (lambda trigger: {"payload": {}}),
        logger=logging.getLogger("InternalResearchAgentTest"),
    )


def test_normalise_payload(tmp_path):
    agent = _make_agent(tmp_path)
    payload = {"company": "Acme", "domain": "acme.test", "email": "a@acme.test"}

    agent._normalise_payload(payload)

    assert payload["company_name"] == "Acme"
    assert payload["company_domain"] == "acme.test"
    assert payload["creator_email"] == "a@acme.test"


def test_resolve_and_extract_ids(tmp_path):
    agent = _make_agent(tmp_path)
    trigger = {"run_id": "run-1", "event_id": "evt-123"}
    payload = {"event_id": "evt-456"}

    assert agent._resolve_run_id(trigger, payload) == "run-1"
    assert agent._extract_event_id(trigger, payload) == "evt-456"


def test_validate_required_fields(tmp_path, caplog):
    agent = _make_agent(tmp_path)
    payload = {"company_name": "", "industry": "Tech"}

    missing_required, missing_optional = agent._validate_required_fields(payload, "ctx")

    assert "company_name" in missing_required
    assert "company_domain" in missing_required
    assert "industry_group" in missing_optional


@pytest.mark.asyncio
async def test_handle_missing_fields_logs_and_returns_payload(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path)

    async def dispatch_true(*args, **kwargs):
        return True

    monkeypatch.setattr(agent, "_dispatch_missing_field_reminder", dispatch_true)

    trigger = {"creator": "agent", "recipient": "user"}
    payload = {"company_name": "Acme", "creator_email": "user@example.com"}

    result = await agent._handle_missing_fields(
        trigger,
        payload,
        run_id="run-123",
        event_id="evt",
        missing_required=["company_domain"],
        missing_optional=["industry"],
    )

    assert result["payload"]["missing_required"] == ["company_domain"]
    assert any(record["step"] == "reminder_sent" for record in agent.workflow_log_manager.records)


@pytest.mark.asyncio
async def test_handle_missing_fields_without_reminder(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path)

    async def dispatch_false(*args, **kwargs):
        return False

    monkeypatch.setattr(agent, "_dispatch_missing_field_reminder", dispatch_false)

    trigger = {"creator": "agent", "recipient": "user"}
    payload = {"company_name": "Acme"}

    result = await agent._handle_missing_fields(
        trigger,
        payload,
        run_id="run-123",
        event_id="evt",
        missing_required=["company_domain"],
        missing_optional=[],
    )

    assert result["status"] == "AWAIT_REQUESTOR_DETAILS"
    assert any(record["step"] == "reminder_not_sent" for record in agent.workflow_log_manager.records)


@pytest.mark.asyncio
async def test_dispatch_missing_field_reminder_without_email(tmp_path):
    agent = _make_agent(tmp_path, email_agent=None)

    sent = await agent._dispatch_missing_field_reminder({}, "run", None, ["a"], [])

    assert sent is False


@pytest.mark.asyncio
async def test_dispatch_missing_field_reminder_with_email(tmp_path, monkeypatch):
    email_agent = DummyEmailAgent()
    agent = _make_agent(tmp_path, email_agent=email_agent)

    class StubReminder:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def send_reminder(self, recipient, subject, body):
            return True

    monkeypatch.setattr("agents.internal_research_agent.ReminderEscalation", StubReminder)

    payload = {"creator_email": "user@example.com", "company_name": "Acme"}
    sent = await agent._dispatch_missing_field_reminder(payload, "run", None, ["field"], [])

    assert sent is True


def test_collect_neighbor_samples(tmp_path):
    agent = _make_agent(tmp_path)
    payload_result = {"neighbors": [{"company_name": "Acme", "domain": "acme.test"}]}

    samples = agent._collect_neighbor_samples(payload_result)

    assert samples[0]["company_name"] == "Acme"


def test_write_artifact_creates_file(tmp_path):
    agent = _make_agent(tmp_path)
    path = agent._write_artifact("run", "artifact.json", {"foo": "bar"})

    assert path is not None
    expected = tmp_path / "research" / "internal_research" / "run" / "artifact.json"
    assert expected.exists()


def test_build_crm_matching_payload(tmp_path):
    agent = _make_agent(tmp_path)
    payload = {"company_name": "Acme", "company_domain": "acme.test", "industry": "Tech"}

    data = agent._build_crm_matching_payload(payload)

    assert data[0]["company_name"] == "Acme"


@pytest.mark.asyncio
async def test_determine_next_action_existing_report(tmp_path, monkeypatch):
    email_agent = DummyEmailAgent()
    agent = _make_agent(tmp_path, email_agent=email_agent)

    async def fake_send_existing_report_email(*args, **kwargs):
        return True

    monkeypatch.setattr(agent, "_send_existing_report_email", fake_send_existing_report_email)

    action, status = await agent._determine_next_action(
        {"creator": "c"},
        {"creator_email": "user@example.com"},
        {"exists": True, "last_report_date": "2024-01-01"},
        "run",
        "evt",
    )

    assert action == "AWAIT_REQUESTOR_DECISION"
    assert status is True


@pytest.mark.asyncio
async def test_determine_next_action_new_report(tmp_path):
    agent = _make_agent(tmp_path)

    action, status = await agent._determine_next_action(
        {},
        {},
        {"exists": False},
        "run",
        None,
    )

    assert action == "REPORT_REQUIRED"
    assert status is None


@pytest.mark.asyncio
async def test_send_existing_report_email_without_configuration(tmp_path):
    agent = _make_agent(tmp_path, email_agent=None)
    payload = {"company_name": "Acme"}

    result = await agent._send_existing_report_email(payload, {}, "run", None, "2024-01-01")

    assert result is None


@pytest.mark.asyncio
async def test_send_existing_report_email_success(tmp_path, monkeypatch):
    email_agent = DummyEmailAgent()
    agent = _make_agent(tmp_path, email_agent=email_agent)

    monkeypatch.setattr(agent, "_render_email_template", lambda name, ctx, optional=False: f"body-{name}")
    monkeypatch.setattr(agent, "_build_crm_portal_link", lambda *sources: "https://crm")

    result = await agent._send_existing_report_email(
        {"creator_email": "user@example.com", "company_name": "Acme"},
        {},
        "run",
        None,
        "2024-01-01",
    )

    assert result is True
    assert email_agent.calls[0]["recipient"] == "user@example.com"


def test_render_email_template_optional_missing(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path)

    def fake_loader(name):
        raise FileNotFoundError

    monkeypatch.setattr("agents.internal_research_agent._load_email_template", fake_loader)

    assert agent._render_email_template("optional.html", {}, optional=True) is None


def test_build_crm_portal_link_from_nested(tmp_path):
    agent = _make_agent(tmp_path)
    mapping = {"payload": {"portal_link": "https://example.com"}}

    assert agent._build_crm_portal_link(mapping) == "https://example.com"


def test_normalize_portal_value_relative(tmp_path):
    agent = _make_agent(tmp_path)
    assert agent._normalize_portal_value("path/to/file") == "https://crm.example/path/to/file"
