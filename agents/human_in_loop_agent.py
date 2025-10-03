import inspect
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List, Optional, Sequence

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
# such as an EmailAgent or chat integration, to interact with event organizers. If no backend is provided,
# the agent falls back to a deterministic simulation for demo/testing.


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
            or 'send_confirmation_request' method. If not supplied, the agent uses a
            deterministic simulation (useful for demos and tests).
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
        Requests missing info from a human via email. Returns status="pending"
        and is_complete=False on sending, with an audit_id for correlation.

        Parameters
        ----------
        event: dict
            The event dictionary (may include id, summary, etc.)
        extracted: dict
            The dictionary containing already extracted info, e.g. {'info': {...}, 'is_complete': False}

        Returns
        -------
        dict
            The extracted dictionary with status="pending", is_complete=False, and audit_id.
        """
        contact = self._extract_organizer_contact(event)
        masked_event = self._mask_for_message(event)
        masked_initial_info = self._mask_for_message(extracted.get("info", {}))
        
        # Determine which fields are missing
        requested_fields = extracted.get("missing", [])
        if not requested_fields:
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
        
        # Build subject with audit token
        summary = event.get("summary", "event")
        subject = f"Missing info for {summary}"
        if audit_id:
            subject = f"{subject} [LeadMI #{audit_id}]"
        
        # Build message body
        message = self._build_missing_info_message(event, extracted, requested_fields)
        
        # Send email via communication backend (EmailAgent)
        email_agent = self._resolve_email_agent_for_reminders()
        contact_email = contact.get("email")
        
        if email_agent and contact_email:
            # Use asyncio to send email
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                # Schedule email sending
                asyncio.create_task(
                    email_agent.send_email_async(contact_email, subject, message)
                )
            except RuntimeError:
                # No event loop running - try sync approach
                logger.warning("No event loop available for email sending; request may be delayed")
            
            self._log_workflow(
                "hitl_missing_info_pending",
                f"Missing info request sent to {contact_email} [audit_id={audit_id or 'n/a'}]",
            )
            
            # Initiate reminder sequence
            if self.reminder_escalation and audit_id:
                self._initiate_missing_info_reminder_sequence(
                    audit_id=audit_id,
                    contact=contact,
                    subject=subject,
                    message=message,
                    event=event,
                    info=extracted.get("info", {}),
                    requested_fields=requested_fields,
                )
        else:
            logger.warning(
                "Email agent not available or no contact email; cannot send missing info request"
            )
            self._log_workflow(
                "hitl_missing_info_skipped",
                f"Missing info request skipped (no email agent or contact) [audit_id={audit_id or 'n/a'}]",
            )
        
        # Return pending status
        result = dict(extracted)
        result["status"] = "pending"
        result["is_complete"] = False
        result["audit_id"] = audit_id
        
        return result

    def request_dossier_confirmation(
        self, event: Dict[str, Any], info: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Ask the organizer whether a dossier should be created for the event.
        Orchestrates interaction with the configured communication backend and always
        returns a normalized dictionary with a boolean flag 'dossier_required' and a
        'details' payload with contextual information.

        Parameters
        ----------
        event: dict
            The event dictionary.
        info: dict
            The extracted info dictionary.

        Returns
        -------
        dict
            {
                "dossier_required": bool,
                "details": {
                    ...context info...
                }
            }
        """
        contact = self._extract_organizer_contact(event)
        masked_event = self._mask_for_message(event)
        masked_info = self._mask_for_message(info)
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
                    "contact": masked_contact,
                },
            )
        
        # Build subject and message with audit token
        subject = self._build_subject(masked_event, audit_id=audit_id)
        message = self._build_message(masked_event, masked_info)
        
        # Update audit log with subject and message
        if self.audit_log and audit_id:
            self.audit_log.record(
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
                audit_id=audit_id,
            )
        
        if handler:
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
        else:
            logger.debug(
                "No communication backend configured; using simulated response."
            )
            backend_response = self._simulate_confirmation(contact, payload)

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

    def _build_subject(self, event: Dict[str, Any], audit_id: Optional[str] = None) -> str:
        """
        Builds the subject line for dossier confirmation requests.
        """
        summary = event.get("summary") or "event"
        subject = f"Dossier confirmation required for {summary}"
        if audit_id:
            subject = f"{subject} [LeadMI #{audit_id}]"
        return subject

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

    def _simulate_confirmation(
        self, contact: Dict[str, Any], payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Simulates dossier confirmation (used for tests or when no backend is available).
        """
        logger.info(
            "Simulating dossier confirmation for organiser %s",
            self._mask_for_message(contact).get("email"),
        )
        return {
            "dossier_required": True,
            "details": {
                "simulation": True,
                "reason": "Default simulated approval",
                "contact": contact,
            },
        }

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
            escalation_recipient = (
                policy.escalation_recipient or contact_email
            )
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

    def _build_missing_info_message(
        self, event: Dict[str, Any], extracted: Dict[str, Any], requested_fields: List[str]
    ) -> str:
        """Build message body for missing info requests."""
        summary = event.get("summary", "Unknown event")
        event_id = event.get("id", "<unknown>")
        lines = [
            "Hello,",
            "",
            f"We need additional information for the event: {summary} ({event_id})",
            "",
            "Currently extracted information:",
        ]
        
        info = extracted.get("info", {})
        for key, value in info.items():
            display_value = value if value else "<missing>"
            lines.append(f"- {key}: {display_value}")
        
        if requested_fields:
            lines.append("")
            lines.append("Missing required fields:")
            for field in requested_fields:
                lines.append(f"- {field}")
        
        lines.append("")
        lines.append("Please reply with the missing information.")
        return "\n".join(lines)

    def _initiate_missing_info_reminder_sequence(
        self,
        *,
        audit_id: str,
        contact: Dict[str, Any],
        subject: str,
        message: str,
        event: Dict[str, Any],
        info: Dict[str, Any],
        requested_fields: List[str],
    ) -> None:
        """Initiate reminder sequence for missing info requests."""
        if not contact:
            return
        contact_email = contact.get("email")
        if not contact_email:
            return

        if not self.reminder_escalation:
            return

        # Use business-time schedule
        from utils.business_time import compute_delays_from_now
        from datetime import datetime
        from zoneinfo import ZoneInfo
        
        try:
            now = datetime.now(ZoneInfo("Europe/Berlin"))
            delays = compute_delays_from_now(now)
        except Exception as exc:
            logger.warning(f"Failed to compute business-time schedule: {exc}")
            # Fallback to simple delays
            delays = [
                {"event": "first_reminder", "delay_seconds": 3600 * 4},  # 4 hours
                {"event": "escalation", "delay_seconds": 3600 * 24},  # 24 hours
            ]

        metadata = {
            "audit_id": audit_id,
            "contact": contact_email,
            "event_id": event.get("id"),
            "workflow_step": "hitl_missing_info",
        }

        for delay_info in delays:
            event_name = delay_info["event"]
            delay_seconds = delay_info["delay_seconds"]
            
            if event_name in ("first_reminder", "second_deadline"):
                self.reminder_escalation.schedule_reminder(
                    contact_email,
                    self._build_reminder_subject(subject),
                    self._build_reminder_message(message, attempt=1, details={}),
                    delay_seconds,
                    metadata={**metadata, "reminder_type": event_name},
                )
                self._log_workflow(
                    f"hitl_missing_info_reminder_scheduled",
                    f"Reminder scheduled for {contact_email} at +{delay_seconds}s [audit_id={audit_id}]",
                )
            elif event_name == "escalation":
                escalation_recipient = getattr(self.reminder_policy, "escalation_recipient", None) or contact_email
                self.reminder_escalation.schedule_escalation(
                    escalation_recipient,
                    self._build_escalation_subject(subject),
                    self._build_missing_info_escalation_message(message, contact, event, info, audit_id),
                    delay_seconds,
                    metadata={**metadata, "escalation_recipient": escalation_recipient},
                )
                self._log_workflow(
                    f"hitl_missing_info_escalation_scheduled",
                    f"Escalation scheduled to {escalation_recipient} at +{delay_seconds}s [audit_id={audit_id}]",
                )

    def _build_missing_info_escalation_message(
        self,
        original_message: str,
        contact: Dict[str, Any],
        event: Dict[str, Any],
        info: Dict[str, Any],
        audit_id: str,
    ) -> str:
        """Build escalation message for missing info requests."""
        event_id = event.get("id") or "<unknown>"
        company = info.get("company_name") or info.get("web_domain") or "unknown company"
        lines = [
            "Escalation notice:",
            "",
            "The organizer has not responded to the missing information request.",
            f"Event ID: {event_id}",
            f"Company: {company}",
            f"Audit trail reference: {audit_id}",
            "",
            "Original request message:",
            original_message,
            "",
            "Please review and take the necessary action.",
        ]
        contact_label = self._format_contact_label(contact)
        lines.append(f"Organizer contact: {contact_label}")
        return "\n".join(lines)
