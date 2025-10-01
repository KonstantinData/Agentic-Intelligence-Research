"""WorkflowOrchestrator: Central orchestrator for the Agentic Intelligence Research workflow."""

import asyncio
import json
import logging
import signal
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Sequence, Set, Union

from agents.alert_agent import AlertAgent, AlertSeverity
from agents.master_workflow_agent import MasterWorkflowAgent
from config.config import settings
from utils.observability import (
    configure_observability,
    flush_telemetry,
    generate_run_id,
    workflow_run,
)
from utils.reporting import convert_research_artifacts_to_pdfs

logger = logging.getLogger("WorkflowOrchestrator")


DEFAULT_SHUTDOWN_TIMEOUT = 5.0


class WorkflowOrchestrator:
    def __init__(
        self,
        communication_backend=None,
        *,
        alert_agent: Optional[AlertAgent] = None,
        master_agent: Optional[MasterWorkflowAgent] = None,
        failure_threshold: int = 3,
    ):
        # Track init errors so run() can short-circuit gracefully.
        self._init_error: Optional[Exception] = None
        self.alert_agent = alert_agent
        self.failure_threshold = max(1, failure_threshold)
        self._failure_key = "workflow_run"
        self._failure_counts: Dict[str, int] = {}
        self._last_run_id: Optional[str] = None
        self._research_summary_root = Path(settings.research_artifact_dir) / "workflow_runs"

        self._background_tasks: Set[asyncio.Task[Any]] = set()
        self._async_cleanups: list[tuple[str, Callable[[], Awaitable[None]]]] = []
        self._sync_cleanups: list[tuple[str, Callable[[], None]]] = []
        self._shutdown_lock: Optional[asyncio.Lock] = None
        self._shutdown_started = False
        self._shutdown_complete = False
        self._shutdown_event: Optional[asyncio.Event] = None
        timeout_setting = getattr(settings, "shutdown_timeout_seconds", DEFAULT_SHUTDOWN_TIMEOUT)
        try:
            self._shutdown_timeout = max(0.1, float(timeout_setting))
        except (TypeError, ValueError):
            self._shutdown_timeout = DEFAULT_SHUTDOWN_TIMEOUT
        self._last_run_summary: Dict[str, Any] = {}
        self._current_run_started_at: Optional[float] = None

        configure_observability()

        try:
            # Support passing through the communication backend.
            self.master_agent = master_agent or MasterWorkflowAgent(
                communication_backend=communication_backend
            )
            self.log_filename = self.master_agent.log_filename
            self.storage_agent = getattr(self.master_agent, "storage_agent", None)
            closer = getattr(self.master_agent, "aclose", None)
            if callable(closer):
                self._register_async_cleanup("master_agent", closer)
        except EnvironmentError as exc:
            # Missing env/config is expected in some (e.g., test) environments.
            logger.error("Failed to initialise MasterWorkflowAgent: %s", exc)
            self.master_agent = None
            self.log_filename = "polling_trigger.log"
            self._init_error = exc
            self.storage_agent = None
            self._handle_exception(exc, handled=True, context={"phase": "initialisation"})

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
            except RuntimeError as exc:  # pragma: no cover - defensive guard
                raise RuntimeError(
                    "WorkflowOrchestrator shutdown requires an active event loop"
                ) from exc

            if self._shutdown_lock is None:
                self._shutdown_lock = asyncio.Lock()
            if self._shutdown_event is None:
                self._shutdown_event = asyncio.Event()

        return self._shutdown_lock, self._shutdown_event

    def track_background_task(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        """Track *task* for cooperative cancellation during shutdown."""

        if task.done():
            return task

        self._background_tasks.add(task)

        def _discard(completed: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(completed)

        task.add_done_callback(_discard)
        return task

    def install_signal_handlers(
        self, loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        """Install POSIX signal handlers that trigger a graceful shutdown."""

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
                # Windows / non-main threads may not support signal handlers.
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
        logger.info(
            "Run manifest: run_id=%s status=%s events=%s duration=%.3fs",
            summary.get("run_id"),
            summary.get("status"),
            summary.get("events_processed"),
            summary.get("duration_seconds", 0.0),
        )

    async def shutdown(self, *, reason: str = "manual", timeout: Optional[float] = None) -> None:
        """Gracefully release resources and cancel background activity."""

        try:
            resolved_timeout = (
                self._shutdown_timeout
                if timeout is None
                else max(0.1, float(timeout))
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
                logger.exception("Failed to flush observability telemetry during shutdown")

            self._log_run_manifest()
            logger.info("Orchestrator shutdown complete.")
        finally:
            self._background_tasks.clear()
            self._async_cleanups.clear()
            self._sync_cleanups.clear()
            self._shutdown_complete = True
            event.set()

    async def run(self) -> None:
        run_id = generate_run_id()
        events_processed = 0
        run_context = None
        start_time = time.perf_counter()
        self._current_run_started_at = start_time

        try:
            with workflow_run(run_id=run_id) as context:
                run_context = context
                self._last_run_id = context.run_id
                logger.info("Workflow orchestrator started.")

                if self._init_error is not None or not self.master_agent:
                    logger.warning(
                        "Workflow orchestrator initialisation skipped due to configuration error."
                    )
                    context.mark_status("skipped")
                else:
                    try:
                        if hasattr(self.master_agent, "initialize_run"):
                            self.master_agent.initialize_run(context.run_id)

                        results = await self.master_agent.process_all_events() or []
                        events_processed = len(results)
                        self._report_research_errors(context.run_id, results)
                        try:
                            self._store_research_outputs(context.run_id, results)
                        except Exception as exc:  # pragma: no cover - defensive guard
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
            logger.exception(
                "Failed to generate PDF artefacts for event %s", event_id
            )
        return None

    @staticmethod
    def _resolve_pdf_source(
        research_result: Mapping[str, object]
    ) -> Optional[Union[str, Path, Mapping[str, Any]]]:
        payload = research_result.get("payload")
        if isinstance(payload, Mapping):
            return payload

        artifact_path = research_result.get("artifact_path")
        if isinstance(artifact_path, str) and artifact_path:
            return artifact_path

        if isinstance(payload, Mapping):  # pragma: no cover - defensive double check
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
            logger.info(
                "Run log stored locally at %s", self.master_agent.log_file_path
            )
        except Exception as exc:
            logger.error("Failed to finalise local log storage", exc_info=True)
            self._handle_exception(
                exc,
                handled=True,
                context={"phase": "finalize"},
            )

        logger.info("Orchestration finalized.")

    # ------------------------------------------------------------------
    # Alert helpers
    # ------------------------------------------------------------------
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
        if self.storage_agent and hasattr(self.storage_agent, "increment_failure_count"):
            return self.storage_agent.increment_failure_count(key)

        self._failure_counts[key] = self._failure_counts.get(key, 0) + 1
        return self._failure_counts[key]

    def _reset_failure_count(self, key: str) -> None:
        if self.storage_agent and hasattr(self.storage_agent, "reset_failure_count"):
            self.storage_agent.reset_failure_count(key)
        elif key in self._failure_counts:
            del self._failure_counts[key]
