"""InboxAgent: Polling helper for processing email replies in the HITL pipeline."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class InboxMessage:
    """Normalized representation of an inbound email message."""

    message_id: str
    subject: Optional[str]
    from_addr: Optional[str]
    body: Optional[str]
    audit_id: Optional[str] = None
    headers: Optional[Mapping[str, str]] = None
    raw: Any = None


ReplyHandler = Callable[[InboxMessage], Awaitable[None]]


class InboxAgent:
    """Poll an IMAP inbox and dispatch replies to registered handlers."""

    def __init__(
        self,
        *,
        host: Optional[str],
        port: Optional[int],
        username: Optional[str],
        password: Optional[str],
        use_ssl: bool = True,
        mailbox: str = "INBOX",
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.mailbox = mailbox

        self._reply_handlers: Dict[str, List[ReplyHandler]] = {}
        self._handler_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------
    def is_configured(self) -> bool:
        """Return ``True`` when minimum IMAP configuration is available."""

        return bool(self.host and self.username and self.password)

    # ------------------------------------------------------------------
    # Handler registration / dispatch
    # ------------------------------------------------------------------
    def register_reply_handler(self, audit_id: str, handler: ReplyHandler) -> None:
        """Register a callback for responses tied to ``audit_id``."""

        if not audit_id:
            raise ValueError("audit_id is required for reply handler registration")
        if handler is None:
            raise ValueError("handler must be provided")
        handlers = self._reply_handlers.setdefault(audit_id, [])
        handlers.append(handler)

    async def _dispatch_message(self, message: InboxMessage) -> None:
        audit_ids = self._extract_audit_ids(message)
        if not audit_ids:
            return

        async with self._handler_lock:
            for audit_id in audit_ids:
                handlers = list(self._reply_handlers.get(audit_id, ()))
                for handler in handlers:
                    try:
                        await handler(message)
                    except Exception:  # pragma: no cover - defensive logging
                        logger.exception(
                            "Inbox reply handler failed for audit_id=%s", audit_id
                        )

    def _extract_audit_ids(self, message: InboxMessage) -> Sequence[str]:
        seen: set[str] = set()
        if message.audit_id:
            seen.add(str(message.audit_id))

        headers = message.headers or {}
        for key in ("X-Audit-ID", "X-HITL-Audit", "X-Workflow-Audit"):
            value = headers.get(key)
            if value:
                seen.add(value.strip())

        if not seen and message.subject:
            match = re.search(r"AUDIT-([0-9A-Za-z]+)", message.subject)
            if match:
                seen.add(match.group(1))

        return list(seen)

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------
    async def start_polling_loop(self, interval_seconds: float = 60.0) -> None:
        """Continuously poll the IMAP inbox for replies."""

        if not self.is_configured():
            logger.info("InboxAgent configuration incomplete; polling disabled")
            return

        interval = max(0.1, float(interval_seconds))

        try:
            while True:
                await self.poll_once()
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive logging
            logger.exception("Inbox polling loop terminated unexpectedly")
            raise

    async def poll_once(self) -> None:
        """Fetch and dispatch any new replies from the inbox."""

        messages = await self.fetch_new_messages()
        for message in messages:
            await self._dispatch_message(message)

    async def fetch_new_messages(self) -> Iterable[InboxMessage]:
        """Fetch messages awaiting processing.

        Production implementations should override this method to integrate with the
        actual IMAP backend. The default implementation returns an empty sequence so
        that tests can stub the method without network access.
        """

        return []

    # ------------------------------------------------------------------
    # Reply parsing helpers
    # ------------------------------------------------------------------
    @staticmethod
    def parse_dossier_decision(body: str) -> Optional[str]:
        if not body:
            return None
        for line in body.splitlines():
            text = line.strip().lower()
            if not text:
                continue
            if text in {"yes", "y", "approve", "approved", "ok", "okay", "ja"}:
                return "approved"
            if text in {"no", "n", "decline", "declined", "reject", "nein"}:
                return "declined"
            break
        return None

    @staticmethod
    def parse_missing_info_key_values(body: str) -> Dict[str, str]:
        result: Dict[str, str] = {}
        if not body:
            return result

        pattern = re.compile(r"^\s*([A-Za-z0-9_ \-]+)\s*:\s*(.+?)\s*$")
        for line in body.splitlines():
            match = pattern.match(line)
            if not match:
                continue
            key = match.group(1).strip().lower().replace(" ", "_")
            value = match.group(2).strip()
            if not value:
                continue
            if key in {"company_name", "web_domain"}:
                result[key] = value
        return result
