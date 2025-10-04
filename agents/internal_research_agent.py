"""Internal research agent implementation registered with the agent factory."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
)

from agents.factory import register_agent
from agents.interfaces import BaseResearchAgent
from agents.internal_company.run import run as internal_company_run
from agents.email_agent import EmailAgent
from config.config import settings
from logs.workflow_log_manager import WorkflowLogManager
from reminders.reminder_escalation import ReminderEscalation
from utils.datetime_formatting import format_report_datetime

NormalizedPayload = Dict[str, Any]

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates" / "email"


class _SafeFormatDict(dict):
    """Dictionary returning an empty string for missing format keys."""

    def __missing__(self, key: str) -> str:  # pragma: no cover - defensive safeguard
        return ""


@lru_cache(maxsize=None)
def _load_email_template(template_name: str) -> str:
    template_path = _TEMPLATE_ROOT / template_name
    if not template_path.exists():
        raise FileNotFoundError(
            f"Email template '{template_name}' not found at {template_path}"
        )
    return template_path.read_text(encoding="utf-8")


@register_agent(BaseResearchAgent, "internal_research", "default", is_default=True)
class InternalResearchAgent(BaseResearchAgent):
    """Internal research agent coordinating CRM lookups and reminders."""

    DEFAULT_REQUIRED_FIELDS: Sequence[str] = ("company_name", "company_domain")
    DEFAULT_OPTIONAL_FIELDS: Sequence[str] = (
        "industry_group",
        "industry",
        "description",
    )

    def __init__(
        self,
        *,
        config: Any = settings,
        workflow_log_manager: Optional[WorkflowLogManager] = None,
        email_agent: Optional[EmailAgent] = None,
        internal_search_runner=internal_company_run,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self.workflow_log_manager = workflow_log_manager or WorkflowLogManager(
            config.workflow_log_dir
        )
        self._internal_search_runner = internal_search_runner
        self.logger = logger or logging.getLogger(
            f"{__name__}.{self.__class__.__name__}"
        )

        self.research_artifact_dir = (
            Path(config.research_artifact_dir) / "internal_research"
        )
        self.research_artifact_dir.mkdir(parents=True, exist_ok=True)

        self.agent_log_dir = Path(config.agent_log_dir) / "internal_research"
        self.agent_log_dir.mkdir(parents=True, exist_ok=True)
        self._configure_file_logger()

        self.email_agent = email_agent or self._build_email_agent_from_env()
        self._default_signature_text = "Best regards,\nInternal Research Agent"
        self._default_signature_html = "<p>Best regards,<br>Internal Research Agent</p>"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def run(self, trigger: Mapping[str, Any]) -> NormalizedPayload:  # type: ignore[override]
        payload = self._clone_payload(trigger.get("payload"))
        context = str(trigger.get("source") or "")
        self._normalise_payload(payload)

        run_id = self._resolve_run_id(trigger, payload)
        event_id = self._extract_event_id(trigger, payload)

        self._log_workflow(
            run_id,
            "start",
            "Internal research workflow received trigger.",
            event_id,
        )

        missing_required, missing_optional = self._validate_required_fields(
            payload, context
        )

        if missing_required:
            return await self._handle_missing_fields(
                trigger, payload, run_id, event_id, missing_required, missing_optional
            )

        if missing_optional:
            self._log_workflow(
                run_id,
                "missing_optional_fields",
                f"Optional fields missing: {', '.join(missing_optional)}.",
                event_id,
            )

        self._log_workflow(
            run_id,
            "fields_validated",
            "Required company fields present for research run.",
            event_id,
        )

        research_result = self._run_internal_lookup(trigger, run_id, event_id)
        payload_result = research_result.get("payload") or {}

        samples = self._collect_neighbor_samples(payload_result)
        neighbor_artifact = self._write_artifact(run_id, "level1_samples.json", samples)
        if samples and neighbor_artifact:
            self._log_workflow(
                run_id,
                "neighbor_samples_recorded",
                f"Captured {len(samples)} neighbor samples at {neighbor_artifact}.",
                event_id,
            )

        crm_artifact = self._write_artifact(
            run_id,
            "crm_matching_company.json",
            self._build_crm_matching_payload(payload),
        )
        if crm_artifact:
            self._log_workflow(
                run_id,
                "crm_matching_recorded",
                f"Stored CRM matching details at {crm_artifact}.",
                event_id,
            )

        action, email_status = await self._determine_next_action(
            trigger, payload, payload_result, run_id, event_id
        )

        self._log_workflow(
            run_id,
            "completed",
            f"Internal research completed with status {action}.",
            event_id,
            error=None
            if email_status
            else "email_delivery_failed"
            if email_status is False
            else None,
        )

        normalized = {
            "source": "internal_research",
            "status": action,
            "agent": "internal_research",
            "creator": trigger.get("creator"),
            "recipient": trigger.get("recipient"),
            "payload": {
                "action": action,
                "level1_samples": samples,
                "existing_report": bool(payload_result.get("exists")),
                "last_report_date": payload_result.get("last_report_date"),
                "artifacts": {
                    "neighbor_samples": neighbor_artifact,
                    "crm_match": crm_artifact,
                },
            },
        }

        return normalized

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _configure_file_logger(self) -> None:
        log_file = self.agent_log_dir / "internal_research.log"
        handler_exists = any(
            isinstance(handler, logging.FileHandler)
            and getattr(handler, "_internal_research_handler", False)
            for handler in self.logger.handlers
        )
        if not handler_exists:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            setattr(file_handler, "_internal_research_handler", True)
            self.logger.addHandler(file_handler)

        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

    def _build_email_agent_from_env(self) -> Optional[EmailAgent]:
        host = settings.smtp_host
        port = settings.smtp_port
        username = settings.smtp_username
        password = settings.smtp_password
        sender = settings.smtp_sender or username

        if not (host and port and username and password and sender):
            self.logger.info(
                "EmailAgent configuration missing; reminder emails will be skipped."
            )
            return None

        return EmailAgent(host, int(port), username, password, sender)

    def _clone_payload(
        self, payload: Optional[Mapping[str, Any]]
    ) -> MutableMapping[str, Any]:
        return dict(payload or {})

    def _normalise_payload(self, payload: MutableMapping[str, Any]) -> None:
        if payload.get("company") and not payload.get("company_name"):
            payload["company_name"] = payload["company"]
        if payload.get("domain") and not payload.get("company_domain"):
            payload["company_domain"] = payload["domain"]
        if not payload.get("creator_email"):
            payload["creator_email"] = payload.get("email")

    def _resolve_run_id(
        self, trigger: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> str:
        run_id = (
            trigger.get("run_id")
            or payload.get("event_id")
            or f"internal-research-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        )
        return str(run_id)

    def _extract_event_id(
        self, trigger: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> Optional[str]:
        return (
            payload.get("event_id")
            or trigger.get("event_id")
            or trigger.get("id")
            or payload.get("id")
        )

    def _validate_required_fields(
        self, payload: Mapping[str, Any], context: str
    ) -> tuple[List[str], List[str]]:
        missing_required = [
            field for field in self.DEFAULT_REQUIRED_FIELDS if not payload.get(field)
        ]
        missing_optional = [
            field for field in self.DEFAULT_OPTIONAL_FIELDS if not payload.get(field)
        ]
        self.logger.info(
            "Validated payload context '%s'. Missing required: %s, missing optional: %s",
            context or "default",
            missing_required,
            missing_optional,
        )
        return missing_required, missing_optional

    async def _handle_missing_fields(
        self,
        trigger: Mapping[str, Any],
        payload: Mapping[str, Any],
        run_id: str,
        event_id: Optional[str],
        missing_required: Sequence[str],
        missing_optional: Sequence[str],
    ) -> NormalizedPayload:
        company = (
            payload.get("company_name") or payload.get("company") or "Unknown Company"
        )
        message = (
            f"Missing required fields for {company}: {', '.join(missing_required)}."
        )
        self._log_workflow(run_id, "missing_required_fields", message, event_id)
        self.logger.info(message)

        reminder_sent = await self._dispatch_missing_field_reminder(
            payload, run_id, event_id, missing_required, missing_optional
        )

        if reminder_sent:
            self._log_workflow(
                run_id,
                "reminder_sent",
                f"Reminder issued to {payload.get('creator_email')} for missing fields.",
                event_id,
            )
        else:
            self._log_workflow(
                run_id,
                "reminder_not_sent",
                "Reminder not sent due to unavailable email configuration.",
                event_id,
                error="email_not_configured",
            )

        return {
            "source": "internal_research",
            "status": "AWAIT_REQUESTOR_DETAILS",
            "agent": "internal_research",
            "creator": trigger.get("creator"),
            "recipient": trigger.get("recipient"),
            "payload": {
                "missing_required": list(missing_required),
                "missing_optional": list(missing_optional),
                "company": company,
            },
        }

    async def _dispatch_missing_field_reminder(
        self,
        payload: Mapping[str, Any],
        run_id: str,
        event_id: Optional[str],
        missing_required: Sequence[str],
        missing_optional: Sequence[str],
    ) -> bool:
        if not self.email_agent:
            return False

        recipient = payload.get("creator_email")
        if not recipient:
            return False

        subject = "Additional details required for research request"
        company = (
            payload.get("company_name") or payload.get("company") or "your company"
        )
        missing_all = list(missing_required) + list(missing_optional)
        formatted_missing = ", ".join(missing_all)
        body = (
            f"Hi,\n\nWe need a bit more information to research {company}."
            f" Please provide the following fields: {formatted_missing}.\n\n"
            "Thank you!"
        )

        reminder = ReminderEscalation(
            self.email_agent,
            workflow_log_manager=self.workflow_log_manager,
            run_id=run_id,
        )
        try:
            return bool(await reminder.send_reminder(recipient, subject, body))
        except Exception as exc:  # pragma: no cover - defensive logging
            self._log_workflow(
                run_id,
                "reminder_exception",
                "Exception occurred while sending reminder.",
                event_id,
                error=str(exc),
            )
            return False

    def _run_internal_lookup(
        self, trigger: Mapping[str, Any], run_id: str, event_id: Optional[str]
    ) -> Mapping[str, Any]:
        try:
            result = self._internal_search_runner(trigger)
            self._log_workflow(
                run_id,
                "internal_lookup_completed",
                "Internal company search completed successfully.",
                event_id,
            )
            return result
        except Exception as exc:  # pragma: no cover - defensive logging
            self._log_workflow(
                run_id,
                "internal_lookup_failed",
                "Internal company search failed.",
                event_id,
                error=str(exc),
            )
            raise

    def _collect_neighbor_samples(
        self, payload_result: Mapping[str, Any]
    ) -> List[Dict[str, Any]]:
        samples: List[Dict[str, Any]] = []
        for neighbor in payload_result.get("neighbors", []) or []:
            samples.append(
                {
                    "company_name": neighbor.get("company_name")
                    or neighbor.get("name")
                    or "",
                    "domain": neighbor.get("company_domain")
                    or neighbor.get("domain")
                    or "",
                    "description": neighbor.get("description"),
                    "reason_for_match": "internal industry/description similarity",
                }
            )
        return samples

    def _write_artifact(self, run_id: str, filename: str, data: Any) -> Optional[str]:
        if not data:
            return None

        run_dir = self.research_artifact_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = run_dir / filename
        with artifact_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        return artifact_path.as_posix()

    def _build_crm_matching_payload(
        self, payload: Mapping[str, Any]
    ) -> List[Dict[str, Any]]:
        company_name = payload.get("company_name") or ""
        company_domain = payload.get("company_domain") or ""
        if not company_name and not company_domain:
            return []

        return [
            {
                "company_name": company_name,
                "company_domain": company_domain,
                "industry_group": payload.get("industry_group"),
                "industry": payload.get("industry"),
                "description": payload.get("description"),
            }
        ]

    async def _determine_next_action(
        self,
        trigger: Mapping[str, Any],
        payload: Mapping[str, Any],
        payload_result: Mapping[str, Any],
        run_id: str,
        event_id: Optional[str],
    ) -> tuple[str, Optional[bool]]:
        exists = payload_result.get("exists")
        last_report_date = payload_result.get("last_report_date")

        if exists and last_report_date:
            email_status = await self._send_existing_report_email(
                payload,
                payload_result,
                run_id,
                event_id,
                last_report_date,
            )
            action = "AWAIT_REQUESTOR_DECISION"
            return action, email_status

        action = "REPORT_REQUIRED"
        self._log_workflow(
            run_id,
            "report_required",
            "No existing dossier found; report creation required.",
            event_id,
        )
        return action, None

    async def _send_existing_report_email(
        self,
        payload: Mapping[str, Any],
        payload_result: Mapping[str, Any],
        run_id: str,
        event_id: Optional[str],
        last_report_date: str,
    ) -> Optional[bool]:
        recipient = payload.get("creator_email")
        if not recipient or not self.email_agent:
            self._log_workflow(
                run_id,
                "existing_report_email_skipped",
                "Existing report email skipped due to configuration.",
                event_id,
                error="email_not_configured"
                if not self.email_agent
                else "missing_recipient",
            )
            return None

        company_name = payload.get("company_name") or "the requested company"
        display_date = format_report_datetime(last_report_date)

        subject = f"Existing research available for {company_name}"
        first_name = recipient.split("@", 1)[0]
        context = {
            "recipient_name": first_name,
            "company_name": company_name,
            "last_report_date": display_date,
            "signature": self._default_signature_text,
        }
        body = (
            self._render_email_template(
                "internal_research_existing_dossier.txt", context
            )
            or ""
        )
        html_body = self._render_email_template(
            "internal_research_existing_dossier.html",
            {**context, "signature": self._default_signature_html},
            optional=True,
        )

        portal_link = self._build_crm_portal_link(payload_result, payload)
        attachment_links: Optional[Iterable[str]] = None
        if portal_link:
            attachment_links = [portal_link]

        try:
            send_email = getattr(self.email_agent, "send_email_async", None)
            if not callable(send_email):
                raise AttributeError("email_agent must expose 'send_email_async'")
            sent = await send_email(
                recipient,
                subject,
                body,
                html_body=html_body,
                attachment_links=attachment_links,
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            self._log_workflow(
                run_id,
                "existing_report_email_failed",
                "Failed to send existing report notification.",
                event_id,
                error=str(exc),
            )
            return False

        if sent:
            self._log_workflow(
                run_id,
                "existing_report_email_sent",
                f"Existing report email sent to {recipient}.",
                event_id,
            )
        else:
            self._log_workflow(
                run_id,
                "existing_report_email_failed",
                f"Existing report email failed for {recipient}.",
                event_id,
                error="send_failed",
            )
        return bool(sent)

    def _log_workflow(
        self,
        run_id: str,
        step: str,
        message: str,
        event_id: Optional[str],
        error: Optional[str] = None,
    ) -> None:
        self.workflow_log_manager.append_log(
            run_id,
            step,
            message,
            event_id=event_id,
            error=error,
        )

    def _render_email_template(
        self,
        template_name: str,
        context: Mapping[str, Any],
        *,
        optional: bool = False,
    ) -> Optional[str]:
        try:
            template = _load_email_template(template_name)
        except FileNotFoundError:
            if optional:
                return None
            raise

        safe_context = _SafeFormatDict(context)
        rendered = template.format_map(safe_context)
        return rendered

    def _build_crm_portal_link(self, *sources: Mapping[str, Any]) -> Optional[str]:
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            link = self._extract_portal_link_from_mapping(source)
            if link:
                return link
        return None

    def _extract_portal_link_from_mapping(
        self, mapping: Mapping[str, Any]
    ) -> Optional[str]:
        candidate_keys = (
            "crm_attachment_link",
            "crm_attachment_url",
            "crm_attachment_path",
            "attachment_link",
            "attachment_url",
            "portal_link",
            "portal_url",
            "portal_path",
        )
        for key in candidate_keys:
            value = mapping.get(key)
            normalized = self._normalize_portal_value(value)
            if normalized:
                return normalized

        nested = mapping.get("payload")
        if isinstance(nested, Mapping):
            return self._extract_portal_link_from_mapping(nested)
        return None

    def _normalize_portal_value(self, value: Any) -> Optional[str]:
        if isinstance(value, Mapping):
            for nested_value in value.values():
                normalized = self._normalize_portal_value(nested_value)
                if normalized:
                    return normalized
            return None

        if isinstance(value, (list, tuple, set)):
            for item in value:
                normalized = self._normalize_portal_value(item)
                if normalized:
                    return normalized
            return None

        if not value:
            return None

        value_str = str(value).strip()
        if not value_str:
            return None

        if value_str.startswith("http://") or value_str.startswith("https://"):
            return value_str

        base = (self.config.crm_attachment_base_url or "").rstrip("/")
        if not base:
            return None
        return f"{base}/{value_str.lstrip('/')}"


__all__ = ["InternalResearchAgent"]
