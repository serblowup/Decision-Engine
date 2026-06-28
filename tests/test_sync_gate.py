"""Tests for the initial topology-sync startup gate.

The core wait loop (`wait_for_initial_sync`) is exercised with an injected async
`fetch_status` and an `asyncio.Event` standing in for the LISTEN/NOTIFY wakeup,
so no live DB or real Postgres NOTIFY is required. `get_sync_state` is tested by
monkeypatching `_fetch_row`, and `run_sync_gate`'s disabled path is tested by
flipping the config flag.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest

import src.db.network_state as ns
from src.db import sync_gate
from src.db.sync_gate import (
    RESULT_DISABLED,
    RESULT_READY,
    RESULT_TIMEOUT,
    run_sync_gate,
    wait_for_initial_sync,
)

NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _ok(ts: datetime | None = NOW):
    return {"status": "OK", "last_full_sync_at": ts}


def _row(status: str, ts: datetime | None = None):
    return {"status": status, "last_full_sync_at": ts}


def _seq_fetch(sequence):
    """Async fetch that yields each item then repeats the last forever."""
    state = {"i": 0}

    async def fetch():
        i = state["i"]
        if i < len(sequence):
            state["i"] += 1
            return sequence[i]
        return sequence[-1] if sequence else None

    return fetch, state


# --- Key race case: sync already OK before DE started ----------------------

async def test_ok_before_start_returns_ready_immediately():
    """sync_state already OK at first read -> proceed with no waiting."""
    fetch, state = _seq_fetch([_ok()])
    event = asyncio.Event()

    result = await wait_for_initial_sync(
        fetch,
        notify_event=event,
        timeout_seconds=5,
        poll_interval_seconds=10,  # large: if we waited at all, the test would hang
        max_age_seconds=0,
    )

    assert result == RESULT_READY
    assert state["i"] == 1  # read exactly once, never waited


# --- IN_PROGRESS then NOTIFY delivers OK -----------------------------------

async def test_in_progress_then_notify_ok():
    """IN_PROGRESS at start; a NOTIFY flips it to OK and wakes the loop."""
    event = asyncio.Event()

    async def fetch():
        return _ok() if event.is_set() else _row("IN_PROGRESS")

    async def fire_notify():
        await asyncio.sleep(0.02)
        event.set()  # CM finished -> NOTIFY callback would set this

    asyncio.create_task(fire_notify())
    # Large poll interval so success is attributable to the NOTIFY, not polling.
    result = await asyncio.wait_for(
        wait_for_initial_sync(
            fetch,
            notify_event=event,
            timeout_seconds=5,
            poll_interval_seconds=10,
            max_age_seconds=0,
        ),
        timeout=2.0,
    )
    assert result == RESULT_READY


# --- NOTIFY lost: polling fallback still picks up OK -----------------------

async def test_lost_notify_polling_picks_up_ok():
    """No NOTIFY ever arrives; the table poll fallback still finds OK."""
    fetch, _ = _seq_fetch([_row("IN_PROGRESS"), _row("IN_PROGRESS"), _ok()])
    event = asyncio.Event()  # never set

    result = await asyncio.wait_for(
        wait_for_initial_sync(
            fetch,
            notify_event=event,
            timeout_seconds=5,
            poll_interval_seconds=0.01,
            max_age_seconds=0,
        ),
        timeout=2.0,
    )
    assert result == RESULT_READY


# --- FAILED holds and retries ----------------------------------------------

async def test_failed_then_ok_holds_then_proceeds(caplog):
    fetch, _ = _seq_fetch([_row("FAILED"), _ok()])
    event = asyncio.Event()

    with caplog.at_level(logging.WARNING, logger="src.db.sync_gate"):
        result = await asyncio.wait_for(
            wait_for_initial_sync(
                fetch,
                notify_event=event,
                timeout_seconds=5,
                poll_interval_seconds=0.01,
                max_age_seconds=0,
            ),
            timeout=2.0,
        )
    assert result == RESULT_READY
    assert any("FAILED" in r.message for r in caplog.records)


async def test_failed_only_times_out_and_does_not_proceed():
    """A persistently FAILED sync must NOT be reported ready."""
    fetch, _ = _seq_fetch([_row("FAILED")])
    event = asyncio.Event()

    result = await asyncio.wait_for(
        wait_for_initial_sync(
            fetch,
            notify_event=event,
            timeout_seconds=0.05,
            poll_interval_seconds=0.01,
            max_age_seconds=0,
        ),
        timeout=2.0,
    )
    assert result == RESULT_TIMEOUT


# --- Freshness policy -------------------------------------------------------

async def test_stale_ok_holds_under_freshness_policy(caplog):
    """OK but last_full_sync_at older than max_age -> treated as not usable."""
    stale = NOW - timedelta(seconds=600)
    fetch, _ = _seq_fetch([_ok(stale)])
    event = asyncio.Event()

    with caplog.at_level(logging.WARNING, logger="src.db.sync_gate"):
        result = await asyncio.wait_for(
            wait_for_initial_sync(
                fetch,
                notify_event=event,
                timeout_seconds=0.05,
                poll_interval_seconds=0.01,
                max_age_seconds=60,
                now=lambda: NOW,
            ),
            timeout=2.0,
        )
    assert result == RESULT_TIMEOUT
    assert any("stale" in r.message for r in caplog.records)


async def test_fresh_ok_passes_freshness_policy():
    recent = NOW - timedelta(seconds=5)
    fetch, _ = _seq_fetch([_ok(recent)])
    event = asyncio.Event()

    result = await wait_for_initial_sync(
        fetch,
        notify_event=event,
        timeout_seconds=5,
        poll_interval_seconds=10,
        max_age_seconds=60,
        now=lambda: NOW,
    )
    assert result == RESULT_READY


async def test_max_age_zero_disables_freshness_check():
    """max_age=0 -> any OK accepted even with an ancient/None timestamp."""
    fetch, _ = _seq_fetch([_ok(None)])
    event = asyncio.Event()

    result = await wait_for_initial_sync(
        fetch,
        notify_event=event,
        timeout_seconds=5,
        poll_interval_seconds=10,
        max_age_seconds=0,
    )
    assert result == RESULT_READY


# --- Timeout behaviour ------------------------------------------------------

async def test_timeout_proceed_returns_timeout():
    fetch, _ = _seq_fetch([_row("NEVER_SYNCED")])
    event = asyncio.Event()

    result = await asyncio.wait_for(
        wait_for_initial_sync(
            fetch,
            notify_event=event,
            timeout_seconds=0.05,
            poll_interval_seconds=0.01,
            max_age_seconds=0,
            timeout_behavior="proceed",
        ),
        timeout=2.0,
    )
    assert result == RESULT_TIMEOUT


async def test_timeout_keep_waiting_blocks_then_proceeds_when_ready():
    """keep_waiting must not return on timeout; it proceeds once OK appears."""
    fetch, _ = _seq_fetch([_row("IN_PROGRESS")] * 5 + [_ok()])
    event = asyncio.Event()

    result = await asyncio.wait_for(
        wait_for_initial_sync(
            fetch,
            notify_event=event,
            timeout_seconds=0.02,  # will be exceeded, but keep_waiting holds on
            poll_interval_seconds=0.01,
            max_age_seconds=0,
            timeout_behavior="keep_waiting",
        ),
        timeout=2.0,
    )
    assert result == RESULT_READY


# --- None row treated as NEVER_SYNCED --------------------------------------

async def test_none_row_treated_as_waiting_then_ok():
    fetch, _ = _seq_fetch([None, None, _ok()])
    event = asyncio.Event()

    result = await asyncio.wait_for(
        wait_for_initial_sync(
            fetch,
            notify_event=event,
            timeout_seconds=5,
            poll_interval_seconds=0.01,
            max_age_seconds=0,
        ),
        timeout=2.0,
    )
    assert result == RESULT_READY


# --- run_sync_gate disabled path -------------------------------------------

async def test_run_sync_gate_disabled_skips_db(monkeypatch):
    monkeypatch.setattr(sync_gate.settings, "wait_for_initial_sync", False)

    async def _boom(*_a, **_k):
        raise AssertionError("get_sync_state must not be called when disabled")

    monkeypatch.setattr(sync_gate, "get_sync_state", _boom)

    result = await run_sync_gate(pool=object())
    assert result == RESULT_DISABLED


# --- get_sync_state DB reader (topology_sync_status) -----------------------

async def test_get_sync_state_queries_topology_sync_status_latest_row():
    """The reader must target topology_sync_status and pick the latest row."""
    sql = ns.SYNC_STATUS_SQL
    assert "topology_sync_status" in sql
    assert "ORDER BY updated_at DESC" in sql
    assert "LIMIT 1" in sql
    # Sync has its own table now; no record_type filter belongs in this query.
    assert "record_type" not in sql


async def test_get_sync_state_empty_table_returns_none(monkeypatch):
    """No row yet -> None -> caller treats as NEVER_SYNCED."""
    async def fake_fetch_row(_pool, _query, *params):
        return None

    monkeypatch.setattr(ns, "_fetch_row", fake_fetch_row)
    assert await ns.get_sync_state(object()) is None


async def test_get_sync_state_missing_table_returns_none(monkeypatch):
    """Table missing -> exception -> None -> NEVER_SYNCED (still waiting)."""
    async def fake_fetch_row(_pool, _query, *params):
        raise RuntimeError('relation "topology_sync_status" does not exist')

    monkeypatch.setattr(ns, "_fetch_row", fake_fetch_row)
    assert await ns.get_sync_state(object()) is None


async def test_get_sync_state_ok_passthrough(monkeypatch):
    """CM writes status=OK; the gate sees OK with last_full_sync_at."""
    async def fake_fetch_row(_pool, _query, *params):
        return {"status": "OK", "last_full_sync_at": NOW}

    monkeypatch.setattr(ns, "_fetch_row", fake_fetch_row)
    row = await ns.get_sync_state(object())
    assert row == {"status": "OK", "last_full_sync_at": NOW}


async def test_get_sync_state_in_progress_passthrough(monkeypatch):
    async def fake_fetch_row(_pool, _query, *params):
        return {"status": "IN_PROGRESS", "last_full_sync_at": NOW}

    monkeypatch.setattr(ns, "_fetch_row", fake_fetch_row)
    row = await ns.get_sync_state(object())
    assert row["status"] == "IN_PROGRESS"
    assert row["last_full_sync_at"] == NOW


async def test_get_sync_state_failed_passthrough(monkeypatch):
    async def fake_fetch_row(_pool, _query, *params):
        return {"status": "FAILED", "last_full_sync_at": NOW}

    monkeypatch.setattr(ns, "_fetch_row", fake_fetch_row)
    row = await ns.get_sync_state(object())
    assert row["status"] == "FAILED"


async def test_get_sync_state_null_status_becomes_never_synced(monkeypatch):
    async def fake_fetch_row(_pool, _query, *params):
        return {"status": None, "last_full_sync_at": None}

    monkeypatch.setattr(ns, "_fetch_row", fake_fetch_row)
    row = await ns.get_sync_state(object())
    assert row["status"] == "NEVER_SYNCED"


# --- get_task_batches no longer filters by record_type ---------------------

async def test_get_task_batches_does_not_filter_record_type(monkeypatch):
    """Sync moved out of reconfiguration_task_status; no record_type filter."""
    from uuid import uuid4

    captured = {}

    async def fake_fetch_rows(_pool, query, *params):
        captured["query"] = query
        return []

    monkeypatch.setattr(ns, "_fetch_rows", fake_fetch_rows)
    await ns.get_task_batches(object(), uuid4())

    assert "reconfiguration_task_status" in captured["query"]
    assert "record_type" not in captured["query"]


# --- run_sync_gate enabled orchestration -----------------------------------

async def test_run_sync_gate_enabled_subscribes_first_then_reads(monkeypatch):
    """Enabled gate: subscribe is attempted, then the table read drives readiness."""
    order: list[str] = []

    closed = {"value": False}

    class _FakeConn:
        async def close(self):
            closed["value"] = True

    async def fake_subscribe(_event):
        order.append("subscribe")
        return _FakeConn()

    async def fake_get_sync_state(_pool):
        order.append("read")
        return _ok()

    monkeypatch.setattr(sync_gate.settings, "wait_for_initial_sync", True)
    monkeypatch.setattr(sync_gate.settings, "sync_wait_timeout_seconds", 5)
    monkeypatch.setattr(sync_gate.settings, "sync_poll_interval_seconds", 10)
    monkeypatch.setattr(sync_gate.settings, "sync_max_age_seconds", 0)
    monkeypatch.setattr(sync_gate.settings, "sync_wait_timeout_behavior", "proceed")
    monkeypatch.setattr(sync_gate, "_subscribe", fake_subscribe)
    monkeypatch.setattr(sync_gate, "get_sync_state", fake_get_sync_state)

    result = await run_sync_gate(pool=object())

    assert result == RESULT_READY
    assert order[0] == "subscribe"  # LISTEN happens before the first table read
    assert "read" in order
    assert closed["value"] is True  # listener connection always closed


async def test_subscribe_failure_degrades_to_poll_only(monkeypatch):
    """If LISTEN can't be established, _subscribe returns None (poll-only)."""

    async def boom_connect(*_a, **_k):
        raise OSError("postgres unreachable")

    monkeypatch.setattr(sync_gate.asyncpg, "connect", boom_connect)
    event = asyncio.Event()
    conn = await sync_gate._subscribe(event)
    assert conn is None


async def test_subscribe_uses_topology_sync_channel(monkeypatch):
    """The gate must LISTEN on the dedicated topology_sync_changed channel."""
    assert sync_gate.TOPOLOGY_SYNC_NOTIFY_CHANNEL == "topology_sync_changed"

    captured = {}

    class _FakeConn:
        async def add_listener(self, channel, _cb):
            captured["channel"] = channel

        async def close(self):
            pass

    async def fake_connect(*_a, **_k):
        return _FakeConn()

    monkeypatch.setattr(sync_gate.asyncpg, "connect", fake_connect)
    conn = await sync_gate._subscribe(asyncio.Event())
    assert conn is not None
    assert captured["channel"] == "topology_sync_changed"
