"""Tests for concurrency primitives such as the logging semaphore."""

from __future__ import annotations

import asyncio
from typing import List

import pytest

from utils import concurrency
from utils.concurrency import LoggingSemaphore


pytestmark = pytest.mark.asyncio


async def test_logging_semaphore_limits_concurrent_access() -> None:
    semaphore = LoggingSemaphore("test", 2)
    release_event = asyncio.Event()
    active_counts: List[int] = []

    async def worker() -> None:
        async with semaphore:
            active_counts.append(semaphore.active)
            await release_event.wait()

    tasks = [asyncio.create_task(worker()) for _ in range(3)]

    # Allow two tasks to acquire the semaphore before releasing them.
    while len(active_counts) < semaphore.limit:
        await asyncio.sleep(0)
    assert max(active_counts) <= semaphore.limit

    release_event.set()
    await asyncio.gather(*tasks)

    # Once released the active counter should drop back to zero.
    assert semaphore.active == 0


async def test_logging_semaphore_releases_on_cancellation() -> None:
    semaphore = LoggingSemaphore("test", 1)
    entered = asyncio.Event()
    second_started = asyncio.Event()

    async def holder() -> None:
        async with semaphore:
            entered.set()
            await asyncio.sleep(1)

    async def waiter() -> None:
        async with semaphore:
            second_started.set()

    task1 = asyncio.create_task(holder())
    await entered.wait()

    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    task1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task1

    await asyncio.wait_for(second_started.wait(), timeout=0.2)
    await waiter_task
    assert semaphore.active == 0


async def test_resolve_limit_from_environment(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_HUBSPOT", "7")
    assert concurrency._resolve_limit("MAX_CONCURRENT_HUBSPOT", 5) == 7

    monkeypatch.setenv("MAX_CONCURRENT_HUBSPOT", "not-int")
    assert concurrency._resolve_limit("MAX_CONCURRENT_HUBSPOT", 5) == 5

    monkeypatch.setenv("MAX_CONCURRENT_HUBSPOT", "0")
    assert concurrency._resolve_limit("MAX_CONCURRENT_HUBSPOT", 5) == 5


async def test_normalise_limit_validation():
    assert concurrency._normalise_limit(None, fallback=3, name="test") == 3
    assert concurrency._normalise_limit(4, fallback=3, name="test") == 4
    assert concurrency._normalise_limit("bad", fallback=3, name="test") == 3
    assert concurrency._normalise_limit(0, fallback=3, name="test") == 3


@pytest.mark.asyncio
async def test_run_in_task_group_with_taskgroup():
    results: List[str] = []

    async def runner() -> None:
        results.append("ran")

    await concurrency.run_in_task_group([runner])

    assert results == ["ran"]


@pytest.mark.asyncio
async def test_run_in_task_group_without_taskgroup(monkeypatch):
    if hasattr(asyncio, "TaskGroup"):
        monkeypatch.delattr(asyncio, "TaskGroup")

    results: List[str] = []

    async def runner() -> None:
        results.append("legacy")

    await concurrency.run_in_task_group([runner])

    assert results == ["legacy"]


async def test_reload_limits_updates_semaphores(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_HUBSPOT", "2")
    monkeypatch.setenv("MAX_CONCURRENT_RESEARCH", "4")

    original_hubspot = concurrency._HUBSPOT_LIMIT
    original_research = concurrency._RESEARCH_LIMIT

    try:
        concurrency.reload_limits(hubspot=3, research=5)

        assert concurrency.HUBSPOT_SEMAPHORE.limit == 3
        assert concurrency.RESEARCH_TASK_SEMAPHORE.limit == 5
    finally:
        concurrency.reload_limits(hubspot=original_hubspot, research=original_research)
