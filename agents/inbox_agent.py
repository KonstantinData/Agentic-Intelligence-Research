"""Production IMAP inbox processing agent.

Polls IMAP inbox for replies to HITL requests and continues workflows automatically.
"""

import asyncio
import logging
import os
from typing import Any, Dict, Optional

from utils.async_imap import AsyncIMAPClient, extract_audit_token_from_subject

logger = logging.getLogger(__name__)


class InboxAgent:
    """Agent for processing IMAP inbox for HITL replies."""
    
    def __init__(
        self,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        use_ssl: bool = True,
        mailbox: str = "INBOX",
    ):
        """Initialize inbox agent with IMAP configuration.
        
        Configuration can be provided via arguments or environment variables:
        - IMAP_HOST
        - IMAP_PORT (default: 993 for SSL, 143 for non-SSL)
        - IMAP_USERNAME
        - IMAP_PASSWORD
        - IMAP_USE_SSL (default: 1)
        - IMAP_MAILBOX (default: INBOX)
        """
        self.host = host or os.getenv("IMAP_HOST")
        self.port = port or int(os.getenv("IMAP_PORT") or (993 if use_ssl else 143))
        self.username = username or os.getenv("IMAP_USERNAME")
        self.password = password or os.getenv("IMAP_PASSWORD")
        self.use_ssl = use_ssl if use_ssl is not None else os.getenv("IMAP_USE_SSL", "1") == "1"
        self.mailbox = mailbox or os.getenv("IMAP_MAILBOX", "INBOX")
        
        # Callback registry for handling replies by audit_id
        self._reply_handlers: Dict[str, Any] = {}
    
    def is_configured(self) -> bool:
        """Check if IMAP is properly configured."""
        return bool(self.host and self.username and self.password)
    
    def register_reply_handler(self, audit_id: str, handler: Any) -> None:
        """Register a handler for replies to a specific audit_id.
        
        Args:
            audit_id: Audit identifier from HITL request
            handler: Callback function to handle the reply
        """
        self._reply_handlers[audit_id] = handler
        logger.debug(f"Registered reply handler for audit_id={audit_id}")
    
    def unregister_reply_handler(self, audit_id: str) -> None:
        """Unregister a reply handler.
        
        Args:
            audit_id: Audit identifier
        """
        self._reply_handlers.pop(audit_id, None)
        logger.debug(f"Unregistered reply handler for audit_id={audit_id}")
    
    async def poll_inbox(self, mark_as_read: bool = True) -> int:
        """Poll inbox for new messages and process replies.
        
        Args:
            mark_as_read: Whether to mark processed messages as read
            
        Returns:
            Number of messages processed
        """
        if not self.is_configured():
            logger.warning("IMAP not configured; skipping inbox poll")
            return 0
        
        try:
            async with AsyncIMAPClient(
                host=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                use_ssl=self.use_ssl,
                mailbox=self.mailbox,
            ) as client:
                messages = await client.fetch_unseen_messages()
                logger.info(f"Fetched {len(messages)} unseen messages")
                
                processed = 0
                for message in messages:
                    try:
                        # Extract audit token from subject
                        audit_id = extract_audit_token_from_subject(message.subject)
                        
                        if audit_id:
                            logger.info(f"Found reply for audit_id={audit_id} from {message.from_addr}")
                            
                            # Check if we have a handler for this audit_id
                            handler = self._reply_handlers.get(audit_id)
                            if handler:
                                try:
                                    # Call handler with message details
                                    if asyncio.iscoroutinefunction(handler):
                                        await handler(message)
                                    else:
                                        handler(message)
                                    
                                    logger.info(f"Successfully processed reply for audit_id={audit_id}")
                                    processed += 1
                                except Exception as e:
                                    logger.error(f"Error handling reply for audit_id={audit_id}: {e}")
                            else:
                                logger.warning(f"No handler registered for audit_id={audit_id}")
                        else:
                            logger.debug(f"Message has no audit token: {message.subject}")
                        
                        # Mark as read if requested
                        if mark_as_read:
                            await client.mark_as_read(message.uid)
                    
                    except Exception as e:
                        logger.error(f"Error processing message {message.uid}: {e}")
                
                return processed
        
        except Exception as e:
            logger.error(f"Error polling inbox: {e}")
            return 0
    
    async def start_polling_loop(self, interval_seconds: float = 60.0) -> None:
        """Start continuous inbox polling loop.
        
        Args:
            interval_seconds: Seconds between polls
        """
        if not self.is_configured():
            logger.warning("IMAP not configured; polling loop not started")
            return
        
        logger.info(f"Starting inbox polling loop (interval={interval_seconds}s)")
        
        while True:
            try:
                await self.poll_inbox()
            except Exception as e:
                logger.error(f"Error in polling loop: {e}")
            
            await asyncio.sleep(interval_seconds)
