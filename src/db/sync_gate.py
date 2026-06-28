"""Startup gate: wait for CM's first successful topology sync.

Before DE runs its first decision cycle it must be sure the topology in
PostgreSQL reflects reality. CM performs an initial full sync and records the
outcome as a new row in the dedicated append-only ``topology_sync_status``
table, also emitting a transient ``topology_sync_changed`` NOTIFY.

Ordering matters to avoid a start race:

    1. LISTEN topology_sync_changed   <- subscribe FIRST, so no NOTIFY fired
                                         after this point can be lost.
    2. read latest topology_sync_status row
                                      <- THEN read the current status, which
                                         catches "sync already finished before
                                         DE started" (the transient NOTIFY for
                                         that is long gone).
    3. loop: OK & fresh -> proceed; FAILED -> hold+retry; NEVER_SYNCED /
       IN_PROGRESS -> wait for NOTIFY (bounded) OR poll the table.

The table read is the race-free source of truth (persistent); NOTIFY is only a
latency optimisation, never the sole mechanism. Polling is therefore a
mandatory fallback, not a backup we hope to avoid.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import asyncpg

from src.config import settings
from src.db.network_state import _normalize_dsn, get_sync_state

logger = logging.getLogger(__name__)

TOPOLOGY_SYNC_NOTIFY_CHANNEL = "topology_sync_changed"

STATUS_OK = "OK"
STATUS_FAILED = "FAILED"
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_NEVER_SYNCED = "NEVER_SYNCED"

# Outcomes of the gate.
RESULT_READY = "ready"          # a usable sync was observed
RESULT_TIMEOUT = "timeout"      # timed out; caller proceeds without confirmed sync
RESULT_DISABLED = "disabled"    # gate switched off via config

BEHAVIOR_PROCEED = "proceed"
BEHAVIOR_KEEP_WAITING = "keep_waiting"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_fresh(
    last_sync_at: Optional[datetime],
    max_age_seconds: int,
    now: Callable[[], datetime],
) -> bool:
    """Return True if an OK sync is fresh enough to use.

    ``max_age_seconds <= 0`` disables the freshness check entirely (any OK is
    accepted) — this matches the "at startup any OK is enough" policy.
    """
    if max_age_seconds <= 0:
        return True
    if last_sync_at is None:
        # OK with no timestamp cannot be proven fresh.
        return False
    if last_sync_at.tzinfo is None:
        last_sync_at = last_sync_at.replace(tzinfo=timezone.utc)
    age = (now() - last_sync_at).total_seconds()
    return age <= max_age_seconds


async def wait_for_initial_sync(
    fetch_status: Callable[[], Awaitable[Optional[dict[str, Any]]]],
    *,
    notify_event: asyncio.Event,
    timeout_seconds: float,
    poll_interval_seconds: float,
    max_age_seconds: int,
    timeout_behavior: str = BEHAVIOR_PROCEED,
    now: Callable[[], datetime] = _utcnow,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """Block until a usable topology sync is observed (or timeout policy fires).

    ``fetch_status`` is an async callable returning the latest SYNC status as
    ``{"status": str, "last_full_sync_at": datetime|None}`` or ``None``
    (treated as NEVER_SYNCED). ``notify_event`` is set by the LISTEN callback;
    each iteration waits on it for at most ``poll_interval_seconds`` so a missed
    NOTIFY is always recovered by the next poll.

    Returns ``RESULT_READY`` or ``RESULT_TIMEOUT``.
    """
    start = monotonic()

    while True:
        row = await fetch_status()
        status = (row or {}).get("status") or STATUS_NEVER_SYNCED
        last_sync_at = (row or {}).get("last_full_sync_at")

        if status == STATUS_OK:
            if _is_fresh(last_sync_at, max_age_seconds, now):
                logger.info(
                    "initial topology sync ready (status=OK, last_full_sync_at=%s); proceeding",
                    last_sync_at,
                )
                return RESULT_READY
            logger.warning(
                "sync status is OK but stale (last_full_sync_at=%s, max_age=%ss); holding for a fresh sync",
                last_sync_at,
                max_age_seconds,
            )
        elif status == STATUS_FAILED:
            logger.warning("sync status=FAILED; holding and retrying, not entering decision loop")
        else:  # NEVER_SYNCED / IN_PROGRESS / anything unknown
            logger.info("sync status=%s; waiting for topology sync", status)

        elapsed = monotonic() - start
        remaining = timeout_seconds - elapsed
        if remaining <= 0:
            if timeout_behavior == BEHAVIOR_KEEP_WAITING:
                logger.warning(
                    "initial sync wait exceeded %ss (status=%s); KEEP_WAITING, continuing to wait",
                    timeout_seconds,
                    status,
                )
                start = monotonic()  # reset so we don't spin on the timeout branch
                remaining = timeout_seconds
            else:
                logger.warning(
                    "initial sync wait timed out after %ss (status=%s); proceeding WITHOUT a confirmed sync",
                    timeout_seconds,
                    status,
                )
                return RESULT_TIMEOUT

        # Wait for a NOTIFY wakeup, but never longer than the poll interval (or
        # the remaining budget) so the persistent table is always re-read.
        wait_budget = min(poll_interval_seconds, remaining) if remaining > 0 else poll_interval_seconds
        wait_budget = max(wait_budget, 0.0)
        notify_event.clear()
        try:
            await asyncio.wait_for(notify_event.wait(), timeout=wait_budget)
            logger.debug("woke on %s NOTIFY", TOPOLOGY_SYNC_NOTIFY_CHANNEL)
        except (asyncio.TimeoutError, TimeoutError):
            pass  # poll fallback — re-read the persistent table


async def _subscribe(
    notify_event: asyncio.Event,
) -> Optional[asyncpg.Connection[Any]]:
    """Open a dedicated connection and LISTEN on the sync channel.

    Returns the connection (so the caller can close it) or ``None`` if LISTEN
    could not be established — in which case the gate degrades to poll-only,
    which is still race-free because it reads the persistent table.
    """
    try:
        conn = await asyncpg.connect(dsn=_normalize_dsn(settings.postgres_dsn))

        def _on_notify(_connection, _pid, _channel, _payload) -> None:
            notify_event.set()

        await conn.add_listener(TOPOLOGY_SYNC_NOTIFY_CHANNEL, _on_notify)
        logger.info("LISTEN connected on channel=%s", TOPOLOGY_SYNC_NOTIFY_CHANNEL)
        return conn
    except Exception as exc:
        logger.warning(
            "LISTEN on %s unavailable (%s); sync gate will rely on table polling only",
            TOPOLOGY_SYNC_NOTIFY_CHANNEL,
            exc,
        )
        return None


async def run_sync_gate(pool: asyncpg.Pool) -> str:
    """Top-level startup gate used by main.py.

    Honours ``settings.wait_for_initial_sync``; subscribes to the NOTIFY channel
    FIRST, then enters the table-driven wait loop. Always closes the listener
    connection before returning.
    """
    if not settings.wait_for_initial_sync:
        logger.info("WAIT_FOR_INITIAL_SYNC=false; skipping initial sync gate")
        return RESULT_DISABLED

    notify_event = asyncio.Event()
    # Step 1: subscribe FIRST so any NOTIFY after this cannot be lost.
    conn = await _subscribe(notify_event)
    try:
        # Step 2 + 3: read the persistent table, then loop.
        return await wait_for_initial_sync(
            lambda: get_sync_state(pool),
            notify_event=notify_event,
            timeout_seconds=settings.sync_wait_timeout_seconds,
            poll_interval_seconds=settings.sync_poll_interval_seconds,
            max_age_seconds=settings.sync_max_age_seconds,
            timeout_behavior=settings.sync_wait_timeout_behavior,
        )
    finally:
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
