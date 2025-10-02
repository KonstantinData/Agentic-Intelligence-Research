"""Tests for :mod:`utils.async_smtp`."""

from __future__ import annotations

import pytest

from utils import async_smtp


class StubSMTP:
    def __init__(self, *, hostname: str, port: int, use_tls: bool, timeout: float):
        self.hostname = hostname
        self.port = port
        self.use_tls = use_tls
        self.timeout = timeout
        self.connected = False
        self.logged_in = None
        self.sent = None
        self.quit_called = False
        self.closed = False

    async def connect(self):
        self.connected = True

    async def login(self, username: str, password: str):
        self.logged_in = (username, password)

    async def sendmail(self, *, from_addr: str, to_addrs, message: str):
        self.sent = (from_addr, tuple(to_addrs), message)

    async def quit(self):
        self.quit_called = True

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_send_email_ssl_happy_path(monkeypatch):
    stub = StubSMTP(hostname="smtp", port=123, use_tls=True, timeout=10.0)
    monkeypatch.setattr(async_smtp.aiosmtplib, "SMTP", lambda **kwargs: stub)

    await async_smtp.send_email_ssl(
        host="smtp",
        username="user@example.com",
        password="secret",
        message="hello",
        to_addrs=["rcpt@example.com"],
        port=123,
        timeout=10.0,
    )

    assert stub.connected is True
    assert stub.logged_in == ("user@example.com", "secret")
    assert stub.sent == ("user@example.com", ("rcpt@example.com",), "hello")
    assert stub.quit_called is True
    assert stub.closed is False


@pytest.mark.asyncio
async def test_send_email_ssl_falls_back_to_close(monkeypatch):
    class ErrorQuitStub(StubSMTP):
        async def quit(self):
            raise async_smtp.aiosmtplib.errors.SMTPException("fail")

    stub = ErrorQuitStub(hostname="smtp", port=123, use_tls=True, timeout=10.0)
    monkeypatch.setattr(async_smtp.aiosmtplib, "SMTP", lambda **kwargs: stub)

    await async_smtp.send_email_ssl(
        host="smtp",
        username="user@example.com",
        password="secret",
        message="hello",
        to_addrs=["rcpt@example.com"],
    )

    assert stub.quit_called is False
    assert stub.closed is True


@pytest.mark.asyncio
async def test_send_email_ssl_requires_recipient():
    with pytest.raises(ValueError):
        await async_smtp.send_email_ssl(
            host="smtp",
            username="user@example.com",
            password="secret",
            message="hello",
            to_addrs=[],
        )
