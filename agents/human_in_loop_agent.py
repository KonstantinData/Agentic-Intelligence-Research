import inspect
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Optional, Sequence

from agents.factory import register_agent
from agents.interfaces import BaseHumanAgent
from config.config import settings
from logs.workflow_log_manager import WorkflowLogManager
from reminders.reminder_escalation import ReminderEscalation
from utils.audit_log import AuditLog
from utils.pii import mask_pii

logger = logging.getLogger(__name__)

# Notes:
# HumanInLoopAgent manages human-in-the-loop steps for workflows. It optionally uses a communication backend,
# such as an EmailAgent or chat integration, to interact with event organizers. A communication backend is
# required for production HITL workflows.


@register_agent(BaseHumanAgent, "human_in_loop", "default", is_default=True)
class HumanInLoopAgent(BaseHumanAgent):
    @dataclass
    class DossierReminderPolicy:
        initial_delay: timedelta = timedelta(hours=4)
        follow_up_delays: Sequence[timedelta] = (timedelta(hours=24),)
        escalation_delay: Optional[timedelta] = timedelta(hours=48)
        escalation_recipient: Optional[str] = None

    def __init__(
        self,
        communication_backend: Optional[Any] = None,
        *,
        reminder_policy: Optional["HumanInLoopAgent.DossierReminderPolicy"] = None,
    ) -> None:
        """
        Create the HITL agent.

        Parameters
        ----------
        communication_backend:
            A communication client (e.g. EmailAgent, Slack integration) responsible for
            contacting the event organizer. It should provide either a 'request_confirmation'
            or 'send_confirmation_request' method.
        """
        self.communication_backend = communication_backend
        self.audit_log: Optional[AuditLog] = None
        self.workflow_log_manager: Optional[WorkflowLogManager] = None
        self.run_id: Optional[str] = None
        self.reminder_policy = reminder_policy or self.DossierReminderPolicy()
        self.reminder_escalation: Optional[ReminderEscalation] = None
        self._ensure_reminder_escalation()

    def set_audit_log(self, audit_log: AuditLog) -> None:
        """Attach an audit logger used to persist request/response metadata."""

        self.audit_log = audit_log

    def set_run_context(
        self,
        run_id: str,
        workflow_log_manager: WorkflowLogManager,
    ) -> None:
        """Set the workflow run context used for reminder/escalation logging."""

        self.run_id = run_id
        self.workflow_log_manager = workflow_log_manager
        if self.reminder_escalation:
            self.reminder_escalation.run_id = run_id
            self.reminder_escalation.workflow_log_manager = workflow_log_manager
        else:
            self._ensure_reminder_escalation()

    def request_info(
        self, event: Dict[str, Any], extracted: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Requests missing info from a human. This is a dummy implementation for demonstration.
        In a real scenario, this could send an email, Slack message, or open a web form.
        Here, it simulates a user providing the missing information.

        Parameters
        ----------
        event: dict
            The event dictionary (may include id, summary, etc.)
        extracted: dict
            The dictionary containing already extracted info, e.g. {'info': {...}, 'is_complete': False}

        Returns
        -------
        dict
            The extracted dictionary with all info fields completed.
        """
        contact = self._extract_organizer_contact(event)
        masked_event = self._mask_for_message(event)
        masked_initial_info = self._mask_for_message(extracted.get("info", {}))
        requested_fields = [
            key
            for key, value in extracted.get("info", {}).items()
            if value in (None, "")
        ]
        audit_id: Optional[str] = None
        if self.audit_log:
            audit_id = self.audit_log.record(
                event_id=masked_event.get("id"),
                request_type="missing_info",
                stage="request",
                responder=self._format_contact_label(self._mask_for_message(contact)),
                outcome="pending",
                payload={
                    "requested_fields": requested_fields,
                    "event": masked_event,
                    "info": masked_initial_info,
                },
            )

        logger.info(
            "Requesting missing info for event %s: %s",
            masked_event.get("id", "<unknown>"),
            masked_initial_info,
        )
        # Notes: Simulate human response for demo purposes.
        extracted["info"]["company_name"] = (
            extracted["info"].get("company_name") or "Example Corp"
        )
        extracted["info"]["web_domain"] = (
            extracted["info"].get("web_domain") or "example.com"
        )
        extracted["is_complete"] = True
        extracted["status"] = "completed"

        if self.audit_log:
            masked_completed_info = self._mask_for_message(extracted.get("info", {}))
            response_payload = {
                "info": masked_completed_info,
                "is_complete": extracted.get("is_complete"),
            }
            outcome = "completed" if extracted.get("is_complete") else "incomplete"
            audit_id = self.audit_log.record(
                event_id=masked_event.get("id"),
                request_type="missing_info",
                stage="response",
                responder="simulation",
                outcome=outcome,
                payload=response_payload,
                audit_id=audit_id,
            )
            if audit_id:
                extracted["audit_id"] = audit_id

        return extracted

    def request_dossier_confirmation(
        self, event: Dict[str, Any], info: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Request a dossier decision from the organiser via the configured backend."""
        contact = self._extract_organizer_contact(event)
        masked_event = self._mask_for_message(event)
        masked_info = self._mask_for_message(info)
        subject = self._build_subject(masked_event)
        message = self._build_message(masked_event, masked_info)
        payload = {"event": masked_event, "info": masked_info}

        backend_response: Optional[Any] = None
        handler = self._resolve_backend_handler()
        responder_label = self._backend_label(handler)
        masked_contact = self._mask_for_message(contact)
        contact_label = self._format_contact_label(masked_contact)
        audit_id: Optional[str] = None
        if self.audit_log:
            audit_id = self.audit_log.record(
                event_id=masked_event.get("id"),
                request_type="dossier_confirmation",
                stage="request",
                responder=contact_label,
                outcome="sent",
                payload={
                    "subject": subject,
                    "message": message,
                    "contact": masked_contact,
                },
            )
        if not handler:
            raise RuntimeError(
                "No communication backend configured for dossier_confirmation in production."
            )

        logger.debug("Sending dossier confirmation request via backend %s", handler)
        backend_response = self._call_backend_handler(
            handler,
            contact=contact,
            subject=subject,
            message=message,
            event=event,
            info=info,
            payload=payload,
        )

        normalized = self._normalize_response(backend_response)
        details = normalized.get("details", {})
        if not isinstance(details, dict):
            details = {"raw_response": details}
        details.setdefault("contact", masked_contact)
        details.setdefault("subject", subject)
        details.setdefault("message", message)
        normalized["details"] = details

        if self.audit_log:
            outcome = "approved" if normalized.get("dossier_required") else "declined"
            response_payload = {
                "details": normalized.get("details"),
                "response": backend_response,
            }
            audit_id = self.audit_log.record(
                event_id=masked_event.get("id"),
                request_type="dossier_confirmation",
                stage="response",
                responder=responder_label,
                outcome=outcome,
                payload=response_payload,
                audit_id=audit_id,
            )
            if audit_id:
                normalized["audit_id"] = audit_id
        self._post_process_decision(
            normalized,
            audit_id=audit_id,
            contact=contact,
            subject=subject,
            message=message,
            event=event,
            info=info,
        )
        return normalized

    def _resolve_backend_handler(self) -> Optional[Any]:
        """
        Resolves the backend handler for sending confirmation requests.
        Checks for 'request_confirmation' or 'send_confirmation_request'.
        Returns the handler method or None.
        """
        if not self.communication_backend:
            return None

        for attr in ("request_confirmation", "send_confirmation_request"):
            if hasattr(self.communication_backend, attr):
                return getattr(self.communication_backend, attr)
        return None

    def _backend_label(self, handler: Optional[Any]) -> str:
        if handler is None:
            return "simulation"
        bound = getattr(handler, "__self__", None)
        if bound is not None:
            return bound.__class__.__name__
        return getattr(handler, "__qualname__", getattr(handler, "__name__", "backend"))

    def _format_contact_label(self, contact: Dict[str, Any]) -> str:
        email = contact.get("email") if isinstance(contact, dict) else None
        name = contact.get("name") if isinstance(contact, dict) else None
        if email and name:
            return f"{name} <{email}>"
        if email:
            return email
        if name:
            return name
        return "organizer"

    def _call_backend_handler(self, handler: Any, **kwargs: Any) -> Any:
        """
        Calls the backend handler with arguments. Supports handlers with **kwargs
        or only selected arguments.
        """
        signature = inspect.signature(handler)
        if any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        ):
            return handler(**kwargs)

        supported_kwargs = {
            name: value
            for name, value in kwargs.items()
            if name in signature.parameters
        }
        return handler(**supported_kwargs)

    def _extract_organizer_contact(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extracts organizer's contact info from the event dictionary.
        Returns a dict with email, name, phone, and raw organizer data.
        """
        organizer = event.get("organizer") or {}
        creator = event.get("creator") or {}
        email = (
            organizer.get("email")
            or event.get("organizer_email")
            or creator.get("email")
        )
        name = (
            organizer.get("displayName")
            or organizer.get("name")
            or creator.get("displayName")
            or creator.get("name")
        )
        phone = organizer.get("phone") or organizer.get("phoneNumber")
        return {"email": email, "name": name, "phone": phone, "raw": organizer or None}

    def _build_subject(self, event: Dict[str, Any]) -> str:
        """
        Builds the subject line for dossier confirmation requests.
        """
        summary = event.get("summary") or "event"
        return f"Dossier confirmation required for {summary}"

    def _build_message(self, event: Dict[str, Any], info: Dict[str, Any]) -> str:
        """
        Builds the message body for dossier confirmation requests.
        """
        summary = event.get("summary", "Unknown event")
        event_id = event.get("id", "<unknown>")
        lines = [
            f"Event: {summary} ({event_id})",
            "We extracted the following information:",
        ]
        for key, value in info.items():
            lines.append(f"- {key}: {value}")
        lines.append("")
        lines.append("Should we prepare a dossier for this event? Reply yes or no.")
        return "\n".join(lines)

    def _normalize_response(self, response: Any) -> Dict[str, Any]:
        """
        Normalizes the response from a backend or simulation to a standard structure.
        """
        if isinstance(response, dict):
            status = response.get("status")
            status_decision_map = {
                "approved": True,
                "declined": False,
                "rejected": False,
                "denied": False,
                "pending": None,
            }
            normalized_status = (
                str(status).strip().lower() if isinstance(status, str) else None
            )

            if "dossier_required" in response:
                dossier_required = response.get("dossier_required")
            elif normalized_status in status_decision_map:
                dossier_required = status_decision_map[normalized_status]
            else:
                dossier_required = bool(response)
            details = response.get("details")
            if isinstance(details, dict):
                details = dict(details)
            elif details is None:
                details = {
                    key: value
                    for key, value in response.items()
                    if key != "dossier_required"
                }
            else:
                details = {"raw_response": details}
            status = response.get("status")
            resolved_status = self._status_from_decision(dossier_required)
            return {
                "dossier_required": dossier_required,
                "details": details,
                "status": status or resolved_status,
            }

        if isinstance(response, bool):
            decision = response
        elif response is None:
            decision = None
        else:
            decision = bool(response)
        return {
            "dossier_required": decision,
            "details": {"raw_response": response},
            "status": self._status_from_decision(decision),
        }

    def _mask_for_message(self, payload: Any) -> Any:
        if not getattr(settings, "mask_pii_in_messages", False):
            return payload
        return mask_pii(
            payload,
            whitelist=getattr(settings, "pii_field_whitelist", None),
            mode=getattr(settings, "compliance_mode", "standard"),
        )

    def shutdown(self) -> None:
        """Cancel any scheduled reminders when the agent is torn down."""

        if self.reminder_escalation:
            self.reminder_escalation.cancel_pending()

    # ------------------------------------------------------------------
    # Reminder orchestration helpers
    # ------------------------------------------------------------------
    def _ensure_reminder_escalation(self) -> None:
        if self.reminder_escalation:
            return
        email_agent = self._resolve_email_agent_for_reminders()
        if email_agent is None:
            return
        self.reminder_escalation = ReminderEscalation(
            email_agent,
            workflow_log_manager=self.workflow_log_manager,
            run_id=self.run_id,
        )

    def _resolve_email_agent_for_reminders(self) -> Optional[Any]:
        backend = self.communication_backend
        if backend is None:
            return None
        if hasattr(backend, "send_email_async"):
            return backend
        candidate = getattr(backend, "email_agent", None)
        if candidate is not None and hasattr(candidate, "send_email_async"):
            return candidate
        return None

    def _post_process_decision(
        self,
        normalized: Dict[str, Any],
        *,
        audit_id: Optional[str],
        contact: Dict[str, Any],
        subject: str,
        message: str,
        event: Dict[str, Any],
        info: Dict[str, Any],
    ) -> None:
        status = self._determine_status(normalized)
        if status != "pending":
            return

        pending_audit_id = audit_id or normalized.get("audit_id")
        self._log_workflow(
            "hitl_dossier_pending",
            f"Organizer decision pending; reminders initiated [audit_id={pending_audit_id or 'n/a'}]",
        )
        self._initiate_reminder_sequence(
            audit_id=pending_audit_id,
            contact=contact,
            subject=subject,
            message=message,
            event=event,
            info=info,
            details=normalized.get("details", {}),
        )

    def _determine_status(self, payload: Dict[str, Any]) -> str:
        status = payload.get("status")
        if status:
            return str(status)
        decision = payload.get("dossier_required")
        resolved = self._status_from_decision(decision)
        payload["status"] = resolved
        return resolved

    def _status_from_decision(self, decision: Any) -> str:
        if decision is True:
            return "approved"
        if decision is False:
            return "declined"
        return "pending"

    def _initiate_reminder_sequence(
        self,
        *,
        audit_id: Optional[str],
        contact: Dict[str, Any],
        subject: str,
        message: str,
        event: Dict[str, Any],
        info: Dict[str, Any],
        details: Dict[str, Any],
    ) -> None:
        if not contact:
            return
        contact_email = contact.get("email")
        if not contact_email:
            self._log_workflow(
                "hitl_dossier_reminder_skipped",
                f"No organizer email available for reminders [audit_id={audit_id or 'n/a'}]",
            )
            return

        if not self.reminder_escalation:
            self._log_workflow(
                "hitl_dossier_reminder_skipped",
                f"Reminder escalation not configured for {contact_email} [audit_id={audit_id or 'n/a'}]",
            )
            return

        policy = self.reminder_policy
        if not policy:
            return

        metadata = {
            "audit_id": audit_id or "n/a",
            "contact": contact_email,
            "event_id": event.get("id"),
            "workflow_step": "hitl_dossier",
        }

        cumulative_seconds = 0.0
        initial_seconds = max(policy.initial_delay.total_seconds(), 0)
        cumulative_seconds += initial_seconds
        self.reminder_escalation.schedule_reminder(
            contact_email,
            self._build_reminder_subject(subject),
            self._build_reminder_message(message, attempt=1, details=details),
            cumulative_seconds,
            metadata=metadata,
        )

        for attempt_index, delay in enumerate(policy.follow_up_delays, start=2):
            cumulative_seconds += max(delay.total_seconds(), 0)
            self.reminder_escalation.schedule_reminder(
                contact_email,
                self._build_reminder_subject(subject),
                self._build_reminder_message(
                    message,
                    attempt=attempt_index,
                    details=details,
                ),
                cumulative_seconds,
                metadata=metadata,
            )

        if policy.escalation_delay is not None:
            escalation_seconds = max(policy.escalation_delay.total_seconds(), 0)
            escalation_recipient = policy.escalation_recipient or contact_email
            self.reminder_escalation.schedule_escalation(
                escalation_recipient,
                self._build_escalation_subject(subject),
                self._build_escalation_message(
                    message,
                    contact,
                    event,
                    info,
                    details,
                    audit_id=audit_id,
                ),
                escalation_seconds,
                metadata={
                    **metadata,
                    "escalation_recipient": escalation_recipient,
                },
            )

    def _build_reminder_subject(self, original_subject: str) -> str:
        return f"Reminder: {original_subject}"

    def _build_reminder_message(
        self,
        original_message: str,
        *,
        attempt: int,
        details: Dict[str, Any],
    ) -> str:
        lines = [
            "Hello,",
            "",
            "This is a friendly reminder about the dossier confirmation request below.",
            f"Reminder attempt {attempt}.",
            "",
            original_message,
        ]
        note = details.get("note") or details.get("reason")
        if note:
            lines.extend(["", f"Previous context: {note}"])
        lines.append("")
        lines.append("Please respond at your earliest convenience.")
        return "\n".join(lines)

    def _build_escalation_subject(self, original_subject: str) -> str:
        return f"Escalation: {original_subject}"

    def _build_escalation_message(
        self,
        original_message: str,
        contact: Dict[str, Any],
        event: Dict[str, Any],
        info: Dict[str, Any],
        details: Dict[str, Any],
        *,
        audit_id: Optional[str],
    ) -> str:
        event_id = event.get("id") or "<unknown>"
        company = info.get("company_name") or info.get("web_domain") or "the company"
        lines = [
            "Escalation notice:",
            "",
            "The organizer has not responded to the dossier confirmation request.",
            f"Event ID: {event_id}",
            f"Company: {company}",
            f"Audit trail reference: {audit_id or details.get('audit_id') or 'n/a'}",
            "",
            "Original request message:",
            original_message,
            "",
            "Please review and take the necessary action.",
        ]
        contact_label = self._format_contact_label(contact)
        lines.append(f"Organizer contact: {contact_label}")
        return "\n".join(lines)

    def _log_workflow(self, step: str, message: str) -> None:
        if not (self.workflow_log_manager and self.run_id):
            return
        self.workflow_log_manager.append_log(self.run_id, step, message)
