"""WorkflowOrchestrator: central coordinator for research workflows.

Requires externally provided ``run_id`` (generated in :mod:`main`). The
orchestrator neither generates nor mutates the identifier; callers must provide
the run id before instantiation to guarantee consistent telemetry correlation.
"""

import asyncio
import json
import logging
import signal
import time
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Mapping,
    Optional,
    Sequence,
    Set,
    Union,
)

from agents.alert_agent import AlertAgent, AlertSeverity
from agents.inbox_agent import InboxAgent
from agents.master_workflow_agent import MasterWorkflowAgent
from config.config import settings
from reminders.reminder_escalation import ReminderEscalation
from utils.audit_log import AuditLog
from utils.observability import (
    configure_observability,
    flush_telemetry,
    workflow_run,
)
from utils.reporting import convert_research_artifacts_to_pdfs
from utils.workflow_steps import workflow_step_recorder  # NEU

logger = logging.getLogger("WorkflowOrchestrator")

DEFAULT_SHUTDOWN_TIMEOUT = 5.0


class WorkflowOrchestrator:
    def __init__(
        self,
        communication_backend=None,
        *,
        run_id: str,
        alert_agent: Optional[AlertAgent] = None,
        master_agent: Optional[MasterWorkflowAgent] = None,
        failure_threshold: int = 3,
    ):
        self._init_error: Optional[Exception] = None
        self.alert_agent = alert_agent
        self.failure_threshold = max(1, failure_threshold)
        self._failure_key = "workflow_run"
        self._failure_counts: Dict[str, int] = {}
        if not run_id:
            raise ValueError("WorkflowOrchestrator requires a non-empty run_id")
        self.run_id = run_id
        self._research_summary_root = (
            Path(settings.research_artifact_dir) / "workflow_runs"
        )

        self._background_tasks: Set[asyncio.Task[Any]] = set()
        self._async_cleanups: list[tuple[str, Callable[[], Awaitable[None]]]] = []
        self._sync_cleanups: list[tuple[str, Callable[[], None]]] = []
        self._shutdown_lock: Optional[asyncio.Lock] = None
        self._shutdown_started = False
        self._shutdown_complete = False
        self._shutdown_event: Optional[asyncio.Event] = None
        timeout_setting = getattr(
            settings, "shutdown_timeout_seconds", DEFAULT_SHUTDOWN_TIMEOUT
        )
        try:
            self._shutdown_timeout = max(0.1, float(timeout_setting))
        except (TypeError, ValueError):
            self._shutdown_timeout = DEFAULT_SHUTDOWN_TIMEOUT
        self._last_run_summary: Dict[str, Any] = {}
        self._current_run_started_at: Optional[float] = None

        configure_observability()

        self.inbox_agent = InboxAgent(
            host=settings.imap_host,
            port=settings.imap_port,
            username=settings.imap_username,
            password=settings.imap_password,
            use_ssl=getattr(settings, "imap_use_ssl", True),
            mailbox=getattr(settings, "imap_mailbox", "INBOX"),
        )
        self._resolved_audits: Set[str] = set()

        try:
            self.master_agent = master_agent or MasterWorkflowAgent(
                communication_backend=communication_backend
            )
            self.log_filename = self.master_agent.log_filename
            self.storage_agent = getattr(self.master_agent, "storage_agent", None)
            closer = getattr(self.master_agent, "aclose", None)
            if callable(closer):
                self._register_async_cleanup("master_agent", closer)

            self._install_hitl_callbacks()
            self._start_inbox_polling()
        except EnvironmentError as exc:
            logger.error("Failed to initialise MasterWorkflowAgent: %s", exc)
            self.master_agent = None
            self.log_filename = "polling_trigger.log"
            self._init_error = exc
            self.storage_agent = None
            self._handle_exception(
                exc, handled=True, context={"phase": "initialisation"}
            )

    def _register_async_cleanup(
        self, label: str, closer: Callable[[], Awaitable[None]]
    ) -> None:
        self._async_cleanups.append((label, closer))

    def _register_sync_cleanup(self, label: str, closer: Callable[[], None]) -> None:
        self._sync_cleanups.append((label, closer))

    def _ensure_shutdown_primitives(self) -> tuple[asyncio.Lock, asyncio.Event]:
        if self._shutdown_lock is None or self._shutdown_event is None:
            try:
                asyncio.get_running_loop()
            except RuntimeError as exc:
                raise RuntimeError(
                    "WorkflowOrchestrator shutdown requires an active event loop"
                ) from exc

            if self._shutdown_lock is None:
                self._shutdown_lock = asyncio.Lock()
            if self._shutdown_event is None:
                self._shutdown_event = asyncio.Event()

        return self._shutdown_lock, self._shutdown_event

    def track_background_task(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        if task.done():
            return task
        self._background_tasks.add(task)

        def _discard(completed: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(completed)

        task.add_done_callback(_discard)
        return task

    # ------------------------------------------------------------------
    # HITL orchestration helpers
    # ------------------------------------------------------------------
    def _install_hitl_callbacks(self) -> None:
        if not self.master_agent:
            return

        def on_pending(kind: str, audit_id: str, context: Dict[str, Any]) -> None:
            async def _handle_reply(message) -> None:
                if self._is_audit_resolved(audit_id):
                    return

                self._mark_audit_resolved(audit_id)
                normalized = self._normalize_incoming_reply(kind, message)
                normalized.setdefault("event_id", context.get("event_id"))
                self._record_audit_response(audit_id, kind, message, normalized, context)
                self._cancel_reminders_for(audit_id)

                try:
                    await self._continue_after_reply(kind, audit_id, normalized, context)
                except Exception:
                    # Allow retries when continuation fails by clearing resolved state.
                    self._resolved_audits.discard(audit_id)
                    raise

            try:
                self.inbox_agent.register_reply_handler(audit_id, _handle_reply)
            except Exception:  # pragma: no cover - defensive logging
                logger.exception(
                    "Failed to register inbox reply handler for audit_id=%s", audit_id
                )

        self.master_agent.on_pending_audit = on_pending

    def _start_inbox_polling(self) -> None:
        if not self.inbox_agent.is_configured():
            logger.info("InboxAgent not configured; skipping polling startup")
            return

        interval = getattr(settings, "hitl_inbox_poll_seconds", 60.0)
        try:
            task = asyncio.create_task(
                self.inbox_agent.start_polling_loop(interval_seconds=interval)
            )
        except RuntimeError:
            logger.exception("Unable to start inbox polling loop")
            return

        self.track_background_task(task)

    def _is_audit_resolved(self, audit_id: str) -> bool:
        return audit_id in self._resolved_audits

    def _mark_audit_resolved(self, audit_id: str) -> None:
        self._resolved_audits.add(audit_id)

    def _cancel_reminders_for(self, audit_id: str) -> None:
        if not self.master_agent:
            return
        human_agent = getattr(self.master_agent, "human_agent", None)
        reminder: Optional[ReminderEscalation] = getattr(
            human_agent, "reminder_escalation", None
        )
        if reminder and hasattr(reminder, "cancel_for_audit"):
            try:
                reminder.cancel_for_audit(audit_id)
            except Exception:  # pragma: no cover - defensive logging
                logger.exception(
                    "Failed to cancel reminders for audit_id=%s", audit_id
                )

    def _record_audit_response(
        self,
        audit_id: str,
        kind: str,
        message,
        normalized: Dict[str, Any],
        context: Mapping[str, Any],
    ) -> None:
        if not self.master_agent:
            return
        audit_log: Optional[AuditLog] = getattr(self.master_agent, "audit_log", None)
        if not audit_log:
            return

        body_text = (message.body or "") if getattr(message, "body", None) else ""
        payload = {
            "kind": kind,
            "from": getattr(message, "from_addr", None),
            "subject": getattr(message, "subject", None),
            "body_preview": body_text[:500],
            "normalized": normalized,
        }
        event_id = context.get("event_id")

        try:
            audit_log.record(
                event_id=str(event_id) if event_id is not None else None,
                request_type="hitl_response",
                stage="response",
                responder=getattr(message, "from_addr", None) or "unknown",
                outcome=normalized.get("outcome") or "received",
                payload=payload,
                audit_id=audit_id,
            )
        except Exception:  # pragma: no cover - defensive logging
            logger.exception("Failed to record audit response for audit_id=%s", audit_id)

    def _normalize_incoming_reply(self, kind: str, message) -> Dict[str, Any]:
        body = getattr(message, "body", None) or ""
        if kind == "dossier":
            decision = self.inbox_agent.parse_dossier_decision(body)
            return {
                "type": "dossier",
                "decision": decision,
                "outcome": decision or "pending",
            }
        if kind == "missing_info":
            fields = self.inbox_agent.parse_missing_info_key_values(body)
            return {
                "type": "missing_info",
                "fields": fields,
                "outcome": "fields_received" if fields else "pending",
            }
        return {"type": kind, "outcome": "unknown"}

    async def _continue_after_reply(
        self,
        kind: str,
        audit_id: str,
        normalized: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> None:
        if not self.master_agent:
            logger.warning(
                "No master agent available to continue workflow for audit_id=%s", audit_id
            )
            return

        try:
            if kind == "dossier":
                decision = normalized.get("decision")
                await self.master_agent.continue_after_dossier_decision(
                    audit_id=audit_id,
                    decision=decision,
                    context=dict(context),
                )
            elif kind == "missing_info":
                fields = normalized.get("fields") or {}
                await self.master_agent.continue_after_missing_info(
                    audit_id=audit_id,
                    fields=dict(fields),
                    context=dict(context),
                )
            else:
                logger.info(
                    "Unhandled reply type '%s' for audit_id=%s", kind, audit_id
                )
        except Exception:  # pragma: no cover - defensive logging
            logger.exception(
                "Continuation after reply failed for audit_id=%s", audit_id
            )

    def install_signal_handlers(
        self, loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        try:
            loop = loop or asyncio.get_running_loop()
        except RuntimeError:
            return

        if not hasattr(loop, "add_signal_handler"):
            return

        for signal_name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, signal_name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: loop.create_task(
                        self.shutdown(reason=f"signal:{getattr(s, 'name', str(s))}")
                    ),
                )
            except (NotImplementedError, RuntimeError):
                continue

    def _update_run_summary(
        self,
        run_context,
        events_processed: int,
        duration_seconds: float,
    ) -> None:
        if run_context is None:
            return

        self._last_run_summary = {
            "run_id": run_context.run_id,
            "status": run_context.status,
            "events_processed": events_processed,
            "duration_seconds": max(0.0, duration_seconds),
        }

    def _log_run_manifest(self) -> None:
        if not self._last_run_summary:
            return
        summary = self._last_run_summary
        run_id = summary.get("run_id")
        if not isinstance(run_id, str):
            return

        # Guard gegen doppelte Manifest-Ausgabe
        if not workflow_step_recorder.should_write_manifest(run_id):
            return

        logger.info(
            "Run manifest: run_id=%s status=%s events=%s duration=%.3fs",
            summary.get("run_id"),
            summary.get("status"),
            summary.get("events_processed"),
            summary.get("duration_seconds", 0.0),
        )

    async def shutdown(
        self, *, reason: str = "manual", timeout: Optional[float] = None
    ) -> None:
        try:
            resolved_timeout = (
                self._shutdown_timeout if timeout is None else max(0.1, float(timeout))
            )
        except (TypeError, ValueError):
            resolved_timeout = self._shutdown_timeout

        lock, event = self._ensure_shutdown_primitives()
        wait_for_completion: Optional[asyncio.Event] = None
        async with lock:
            if self._shutdown_complete:
                return
            if self._shutdown_started:
                wait_for_completion = event
            else:
                self._shutdown_started = True

        if wait_for_completion is not None:
            await wait_for_completion.wait()
            return

        logger.info("Initiating orchestrator shutdown (reason=%s)", reason)

        try:
            pending_tasks = [task for task in self._background_tasks if not task.done()]
            if pending_tasks:
                logger.debug("Cancelling %d background task(s)", len(pending_tasks))
                for task in pending_tasks:
                    task.cancel()
                try:
                    results = await asyncio.wait_for(
                        asyncio.gather(*pending_tasks, return_exceptions=True),
                        timeout=resolved_timeout,
                    )
                    for result in results:
                        if isinstance(result, BaseException) and not isinstance(
                            result, asyncio.CancelledError
                        ):
                            logger.warning(
                                "Background task exited with exception during shutdown: %s",
                                result,
                            )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Timed out waiting for %d background task(s) to cancel.",
                        len(pending_tasks),
                    )

            for label, closer in list(self._async_cleanups):
                try:
                    await asyncio.wait_for(closer(), timeout=resolved_timeout)
                except asyncio.TimeoutError:
                    logger.warning("Timed out closing resource %s", label)
                except Exception:
                    logger.exception("Error closing resource %s", label)

            for label, closer in list(self._sync_cleanups):
                try:
                    closer()
                except Exception:
                    logger.exception("Error closing synchronous resource %s", label)

            try:
                await flush_telemetry(timeout=resolved_timeout)
            except Exception:
                logger.exception(
                    "Failed to flush observability telemetry during shutdown"
                )

            # Manifest (nur falls noch nicht geschrieben)
            self._log_run_manifest()
            logger.info("Orchestrator shutdown complete.")
        finally:
            self._background_tasks.clear()
            self._async_cleanups.clear()
            self._sync_cleanups.clear()
            self._shutdown_complete = True
            event.set()

    async def run(self) -> None:
        run_id = self.run_id
        events_processed = 0
        run_context = None
        start_time = time.perf_counter()
        self._current_run_started_at = start_time

        try:
            with workflow_run(run_id=run_id) as context:
                run_context = context
                logger.info("Workflow orchestrator started.")

                if self._init_error is not None or not self.master_agent:
                    logger.warning(
                        "Workflow orchestrator initialisation skipped due to configuration error."
                    )
                    context.mark_status("skipped")
                else:
                    try:
                        if hasattr(self.master_agent, "attach_run"):
                            self.master_agent.attach_run(
                                context.run_id, self.master_agent.workflow_log_manager
                            )

                        results = await self.master_agent.process_all_events() or []
                        events_processed = len(results)
                        self._report_research_errors(context.run_id, results)
                        try:
                            self._store_research_outputs(context.run_id, results)
                        except Exception as exc:
                            logger.error(
                                "Failed to persist research outputs", exc_info=True
                            )
                            self._handle_exception(
                                exc,
                                handled=True,
                                context={
                                    "phase": "store_research",
                                    "run_id": context.run_id,
                                },
                            )
                    except Exception as exc:
                        context.mark_failure(exc)
                        logger.exception("Workflow failed with exception:")
                        self._handle_exception(
                            exc,
                            handled=False,
                            context={"phase": "run"},
                            track_failure=True,
                        )
                    else:
                        context.mark_success()
                        logger.info("Workflow completed successfully.")
                        self._reset_failure_count(self._failure_key)
        finally:
            self._finalize()
            duration = time.perf_counter() - start_time
            self._current_run_started_at = None
            self._update_run_summary(run_context, events_processed, duration)
            # Manifest hier (einmalig) – Guard in _log_run_manifest verhindert Doppelausgabe
            self._log_run_manifest()

    def _store_research_outputs(
        self, run_id: str, results: Sequence[Dict[str, object]]
    ) -> None:
        if not results:
            return

        summary_dir = self._research_summary_root / run_id
        summary_dir.mkdir(parents=True, exist_ok=True)
        summary_path = summary_dir / "summary.json"

        sanitized: list[Dict[str, object]] = []
        for entry in results:
            sanitized_entry = {
                "event_id": entry.get("event_id"),
                "status": entry.get("status"),
                "crm_dispatched": entry.get("crm_dispatched", False),
                "trigger": entry.get("trigger"),
                "extraction": entry.get("extraction"),
                "research": entry.get("research"),
                "research_errors": entry.get("research_errors", []),
            }
            pdf_artifacts = self._generate_pdf_artifacts(run_id, entry)
            if pdf_artifacts:
                sanitized_entry["pdf_artifacts"] = pdf_artifacts
            sanitized.append(sanitized_entry)

        summary_path.write_text(
            json.dumps(sanitized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(
            "Stored research summary for run %s at %s",
            run_id,
            summary_path.as_posix(),
        )

    def _generate_pdf_artifacts(
        self, run_id: str, result_entry: Mapping[str, object]
    ) -> Optional[Dict[str, str]]:
        research_section = result_entry.get("research")
        if not isinstance(research_section, Mapping):
            return None

        dossier = research_section.get("dossier_research")
        similar = research_section.get("similar_companies_level1")
        if not isinstance(dossier, Mapping) or not isinstance(similar, Mapping):
            return None

        dossier_source = self._resolve_pdf_source(dossier)
        similar_source = self._resolve_pdf_source(similar)
        if dossier_source is None or similar_source is None:
            return None

        output_dir = Path(settings.research_pdf_dir) / run_id
        event_id = result_entry.get("event_id")

        try:
            return convert_research_artifacts_to_pdfs(
                dossier_source, similar_source, output_dir=output_dir
            )
        except ImportError as exc:
            logger.warning(
                "Skipping PDF generation for event %s due to missing dependency: %s",
                event_id,
                exc,
            )
        except Exception:
            logger.exception("Failed to generate PDF artefacts for event %s", event_id)
        return None

    @staticmethod
    def _resolve_pdf_source(
        research_result: Mapping[str, object],
    ) -> Optional[Union[str, Path, Mapping[str, Any]]]:
        payload = research_result.get("payload")
        if isinstance(payload, Mapping):
            return payload

        artifact_path = research_result.get("artifact_path")
        if isinstance(artifact_path, str) and artifact_path:
            return artifact_path

        if isinstance(payload, Mapping):  # defensive double check
            nested_path = payload.get("artifact_path")
            if isinstance(nested_path, str) and nested_path:
                return nested_path

        return None

    def _report_research_errors(
        self, run_id: str, results: Sequence[Dict[str, object]]
    ) -> None:
        if not results:
            return
        for entry in results:
            for error in entry.get("research_errors", []) or []:
                message = (
                    f"Research agent '{error.get('agent')}' failed during run {run_id}."
                )
                context = {
                    "run_id": run_id,
                    "event_id": entry.get("event_id"),
                    "agent": error.get("agent"),
                    "error": error.get("error"),
                }
                self._emit_alert(message, AlertSeverity.ERROR, context)

    def _finalize(self):
        if not self.master_agent:
            return
        try:
            self.master_agent.finalize_run_logs()
            logger.info("Run log stored locally at %s", self.master_agent.log_file_path)
        except Exception as exc:
            logger.error("Failed to finalise local log storage", exc_info=True)
            self._handle_exception(
                exc,
                handled=True,
                context={"phase": "finalize"},
            )
        logger.info("Orchestration finalized.")

    def _handle_exception(
        self,
        exc: Exception,
        *,
        handled: bool,
        context: Optional[Dict[str, object]] = None,
        track_failure: bool = False,
    ) -> None:
        severity = self._map_exception_to_severity(exc)
        ctx: Dict[str, object] = {
            "exception_type": type(exc).__name__,
            "handled": handled,
        }
        if context:
            ctx.update(context)

        if track_failure:
            failure_count = self._increment_failure_count(self._failure_key)
            ctx["failure_count"] = failure_count
            if failure_count >= self.failure_threshold:
                severity = AlertSeverity.CRITICAL
                ctx["escalated"] = True

        message = (
            "Handled" if handled else "Unhandled"
        ) + f" exception in WorkflowOrchestrator: {exc}"
        self._emit_alert(message, severity, ctx)

    def _emit_alert(
        self, message: str, severity: AlertSeverity, context: Dict[str, object]
    ) -> None:
        if not self.alert_agent:
            return
        self.alert_agent.send_alert(message, severity, context=context)

    def _map_exception_to_severity(self, exc: Exception) -> AlertSeverity:
        if isinstance(exc, (EnvironmentError, OSError)):
            return AlertSeverity.CRITICAL
        if isinstance(exc, (RuntimeError, ConnectionError, TimeoutError)):
            return AlertSeverity.ERROR
        if isinstance(exc, (ValueError, KeyError)):
            return AlertSeverity.WARNING
        return AlertSeverity.ERROR

    def _increment_failure_count(self, key: str) -> int:
        if self.storage_agent and hasattr(
            self.storage_agent, "increment_failure_count"
        ):
            return self.storage_agent.increment_failure_count(key)
        self._failure_counts[key] = self._failure_counts.get(key, 0) + 1
        return self._failure_counts[key]

    def _reset_failure_count(self, key: str) -> None:
        if self.storage_agent and hasattr(self.storage_agent, "reset_failure_count"):
            self.storage_agent.reset_failure_count(key)
        elif key in self._failure_counts:
            del self._failure_counts[key]
