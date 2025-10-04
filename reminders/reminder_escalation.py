import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Optional, Set


class ReminderEscalation:
    """Module for sending reminders and escalation notifications."""

    def __init__(
        self,
        email_agent,
        workflow_log_manager=None,
        run_id=None,
        *,
        task_scheduler: Optional[
            Callable[[asyncio.Task[Any]], asyncio.Task[Any]]
        ] = None,
    ):
        self.email_agent = email_agent
        self.workflow_log_manager = workflow_log_manager
        self.run_id = run_id
        self._task_scheduler = task_scheduler
        self._tasks: Set[asyncio.Task[Any]] = set()
        self._tasks_by_audit: Dict[str, Set[asyncio.Task[Any]]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def send_reminder(
        self,
        recipient,
        subject,
        body,
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not self.email_agent:
            self._append_log(
                "reminder_skipped",
                f"Reminder not sent (email agent unavailable) for {recipient}",
                metadata=metadata,
                error="email_agent_missing",
            )
            return False

        try:
            send_email = getattr(self.email_agent, "send_email_async", None)
            if not callable(send_email):
                raise AttributeError("email_agent must expose 'send_email_async'")
            sent = await send_email(recipient, subject, body)
            step = "reminder_sent" if sent else "reminder_failed"
            error = None if sent else "send_failed"
            self._append_log(
                step,
                f"Reminder {'sent' if sent else 'failed'} to {recipient}: {subject}",
                metadata=metadata,
                error=error,
            )
            return bool(sent)
        except Exception as exc:  # pragma: no cover - defensive logging
            logging.error("Error sending reminder: %s", exc)
            self._append_log(
                "reminder_exception",
                f"Exception during reminder to {recipient}: {exc}",
                metadata=metadata,
                error=str(exc),
            )
            raise

    async def escalate(
        self,
        admin_email,
        subject,
        body,
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not self.email_agent:
            self._append_log(
                "escalation_skipped",
                f"Escalation not sent (email agent unavailable) for {admin_email}",
                metadata=metadata,
                error="email_agent_missing",
            )
            return False

        try:
            send_email = getattr(self.email_agent, "send_email_async", None)
            if not callable(send_email):
                raise AttributeError("email_agent must expose 'send_email_async'")
            sent = await send_email(admin_email, subject, body)
            step = "escalation_sent" if sent else "escalation_failed"
            error = None if sent else "send_failed"
            self._append_log(
                step,
                f"Escalation {'sent' if sent else 'failed'} to {admin_email}: {subject}",
                metadata=metadata,
                error=error,
            )
            return bool(sent)
        except Exception as exc:  # pragma: no cover - defensive logging
            logging.error("Error sending escalation: %s", exc)
            self._append_log(
                "escalation_exception",
                f"Exception during escalation to {admin_email}: {exc}",
                metadata=metadata,
                error=str(exc),
            )
            raise

    def schedule_reminder(
        self,
        recipient: str,
        subject: str,
        body: str,
        delay_seconds: float,
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> asyncio.Task[bool]:
        return self._schedule_action(
            "reminder",
            lambda: self.send_reminder(recipient, subject, body, metadata=metadata),
            delay_seconds,
            recipient,
            subject,
            metadata,
        )

    def schedule_escalation(
        self,
        admin_email: str,
        subject: str,
        body: str,
        delay_seconds: float,
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> asyncio.Task[bool]:
        return self._schedule_action(
            "escalation",
            lambda: self.escalate(admin_email, subject, body, metadata=metadata),
            delay_seconds,
            admin_email,
            subject,
            metadata,
        )

    def schedule_admin_recurring_reminders(
        self,
        admin_email: str,
        subject: str,
        body: str,
        *,
        interval_hours: float,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> asyncio.Task[bool]:
        interval_seconds = max(0.1, float(interval_hours) * 3600.0)

        async def _loop() -> bool:
            try:
                while True:
                    await asyncio.sleep(interval_seconds)
                    await self.send_reminder(
                        admin_email, subject, body, metadata=metadata
                    )
            except asyncio.CancelledError:
                return True

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:  # pragma: no cover - defensive guard
            raise RuntimeError(
                "Admin recurring reminders require an active event loop"
            ) from exc

        task = loop.create_task(_loop())
        return self._register_task(task, metadata=metadata)

    def cancel_pending(self) -> None:
        tasks = list(self._tasks)
        self._tasks.clear()
        self._tasks_by_audit.clear()
        for task in tasks:
            task.cancel()

    def cancel_for_audit(self, audit_id: str) -> None:
        if not audit_id:
            return
        tasks = list(self._tasks_by_audit.pop(str(audit_id), set()))
        for task in tasks:
            self._tasks.discard(task)
            task.cancel()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _schedule_action(
        self,
        action: str,
        sender: Callable[[], Awaitable[bool]],
        delay_seconds: float,
        recipient: str,
        subject: str,
        metadata: Optional[Dict[str, Any]],
    ) -> asyncio.Task[bool]:
        metadata = dict(metadata or {})
        due_time = datetime.now(timezone.utc) + timedelta(seconds=max(delay_seconds, 0))
        self._append_log(
            f"{action}_scheduled",
            f"{action.capitalize()} scheduled for {recipient} at {due_time.isoformat()} ({subject})",
            metadata=metadata,
        )

        async def _execute() -> bool:
            try:
                await asyncio.sleep(max(delay_seconds, 0))
                return await sender()
            except Exception as exc:  # pragma: no cover - defensive logging
                logging.error("Scheduled %s failed for %s: %s", action, recipient, exc)
                raise

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:  # pragma: no cover - defensive guard
            raise RuntimeError(
                "ReminderEscalation scheduling requires an active asyncio event loop"
            ) from exc

        task = loop.create_task(_execute())
        tracked = self._register_task(task, metadata=metadata)
        return tracked

    def _register_task(
        self, task: asyncio.Task[Any], *, metadata: Optional[Dict[str, Any]] = None
    ) -> asyncio.Task[Any]:
        tracked = task
        if self._task_scheduler:
            try:
                scheduled = self._task_scheduler(task)
            except Exception as exc:  # pragma: no cover - defensive logging
                logging.error("Task scheduler failed: %s", exc)
            else:
                if scheduled is not None:
                    tracked = scheduled

        self._tasks.add(tracked)
        tracked.add_done_callback(self._tasks.discard)

        audit_id = None
        if isinstance(metadata, dict):
            audit_id = metadata.get("audit_id")
        if audit_id:
            key = str(audit_id)
            bucket = self._tasks_by_audit.setdefault(key, set())
            bucket.add(tracked)

            def _cleanup(completed: asyncio.Task[Any]) -> None:
                bucket.discard(completed)
                if not bucket:
                    self._tasks_by_audit.pop(key, None)

            tracked.add_done_callback(_cleanup)

        return tracked

    def _append_log(
        self,
        step: str,
        message: str,
        *,
        metadata: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        if not (self.workflow_log_manager and self.run_id):
            return

        workflow_step = None
        if metadata is not None and "workflow_step" in metadata:
            workflow_step = metadata.get("workflow_step")
            metadata = dict(metadata)
            metadata.pop("workflow_step", None)
        suffix = self._format_metadata(metadata)
        step_name = step
        if workflow_step:
            step_name = f"{workflow_step}_{step}"
        self.workflow_log_manager.append_log(
            self.run_id,
            step_name,
            f"{message}{suffix}",
            error=error,
        )

    def _format_metadata(self, metadata: Optional[Dict[str, Any]]) -> str:
        if not metadata:
            return ""
        formatted = []
        for key, value in metadata.items():
            if value is None:
                continue
            formatted.append(f"{key}={value}")
        if not formatted:
            return ""
        return " [" + ", ".join(formatted) + "]"
