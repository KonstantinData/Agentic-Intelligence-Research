"""Async IMAP utilities for processing inbox messages.

Provides production-ready IMAP polling using asyncio + imaplib via to_thread wrappers.
"""

import asyncio
import imaplib
import logging
import re
from dataclasses import dataclass
from email import message_from_bytes
from email.header import decode_header
from email.message import Message
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class EmailMessage:
    """Parsed email message with metadata."""
    uid: str
    subject: str
    from_addr: str
    body: str
    raw_message: Message


def decode_email_header(header: str) -> str:
    """Decode email header handling multiple encodings."""
    if not header:
        return ""
    
    decoded_parts = []
    for part, encoding in decode_header(header):
        if isinstance(part, bytes):
            try:
                decoded_parts.append(part.decode(encoding or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                decoded_parts.append(part.decode("utf-8", errors="replace"))
        else:
            decoded_parts.append(str(part))
    
    return " ".join(decoded_parts)


def extract_body_from_message(msg: Message) -> str:
    """Extract text body from email message."""
    body = ""
    
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            
            if content_type == "text/plain" and "attachment" not in disposition:
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body += payload.decode(charset, errors="replace")
                except Exception as e:
                    logger.warning(f"Failed to decode email part: {e}")
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                body = payload.decode(charset, errors="replace")
        except Exception as e:
            logger.warning(f"Failed to decode email body: {e}")
    
    return body.strip()


def extract_audit_token_from_subject(subject: str) -> Optional[str]:
    """Extract audit token from subject line.
    
    Looks for patterns like [LeadMI #xxxxx] in the subject.
    
    Args:
        subject: Email subject line
        
    Returns:
        Audit ID if found, None otherwise
    """
    # Match [LeadMI #xxxxx] or similar patterns
    match = re.search(r'\[LeadMI\s*#\s*([^\]]+)\]', subject, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


class AsyncIMAPClient:
    """Async IMAP client for reading emails."""
    
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        use_ssl: bool = True,
        mailbox: str = "INBOX",
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.mailbox = mailbox
        self._imap: Optional[imaplib.IMAP4_SSL] = None
    
    async def connect(self) -> None:
        """Connect and authenticate to IMAP server."""
        def _connect():
            if self.use_ssl:
                imap = imaplib.IMAP4_SSL(self.host, self.port)
            else:
                imap = imaplib.IMAP4(self.host, self.port)
            
            imap.login(self.username, self.password)
            imap.select(self.mailbox)
            return imap
        
        self._imap = await asyncio.to_thread(_connect)
        logger.info(f"Connected to IMAP server {self.host}:{self.port}")
    
    async def disconnect(self) -> None:
        """Disconnect from IMAP server."""
        if self._imap:
            def _disconnect():
                try:
                    self._imap.close()
                    self._imap.logout()
                except Exception as e:
                    logger.warning(f"Error during IMAP disconnect: {e}")
            
            await asyncio.to_thread(_disconnect)
            self._imap = None
            logger.info("Disconnected from IMAP server")
    
    async def fetch_unseen_messages(self) -> List[EmailMessage]:
        """Fetch all unseen messages from the mailbox.
        
        Returns:
            List of parsed email messages
        """
        if not self._imap:
            raise RuntimeError("IMAP client not connected")
        
        def _fetch():
            # Search for unseen messages
            status, message_ids = self._imap.search(None, "UNSEEN")
            if status != "OK":
                logger.warning(f"IMAP search failed: {status}")
                return []
            
            if not message_ids[0]:
                return []
            
            messages = []
            for msg_id in message_ids[0].split():
                try:
                    # Fetch message
                    status, msg_data = self._imap.fetch(msg_id, "(RFC822)")
                    if status != "OK":
                        logger.warning(f"Failed to fetch message {msg_id}: {status}")
                        continue
                    
                    # Parse message
                    raw_email = msg_data[0][1]
                    msg = message_from_bytes(raw_email)
                    
                    subject = decode_email_header(msg.get("Subject", ""))
                    from_addr = decode_email_header(msg.get("From", ""))
                    body = extract_body_from_message(msg)
                    
                    messages.append(EmailMessage(
                        uid=msg_id.decode("utf-8"),
                        subject=subject,
                        from_addr=from_addr,
                        body=body,
                        raw_message=msg,
                    ))
                except Exception as e:
                    logger.error(f"Error processing message {msg_id}: {e}")
            
            return messages
        
        return await asyncio.to_thread(_fetch)
    
    async def mark_as_read(self, uid: str) -> None:
        """Mark a message as read.
        
        Args:
            uid: Message UID
        """
        if not self._imap:
            raise RuntimeError("IMAP client not connected")
        
        def _mark():
            self._imap.store(uid.encode("utf-8"), "+FLAGS", "\\Seen")
        
        await asyncio.to_thread(_mark)
    
    async def __aenter__(self):
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()
