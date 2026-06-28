from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg

from src.config import settings
from src.db.network_state import create_pool, get_network_state, get_task_batches, load_topology
from src.db.sync_gate import run_sync_gate
from src.kafka.producer import TaskProducer
from src.metrics_poller import run_metrics_poller
from src.models.network import NetworkState
from src.models.operations import ActionType, ReconfigurationTask
from src.segmenters import (
    ConstraintSet,
    GreedySegmenter,
    SimulatedAnnealingSegmenter,
    SpectralSegmenter,
)
from src.strategies.interface import NetworkContext, StrategyDecision
from src.strategies.orchestrator import decide_with_priority
from src.strategies.rule_based import RuleBasedStrategy
from src.strategies.threshold import ThresholdHeuristic
from src.task_builder.builder import TaskBuilder
from src.topology.graph import Topology
from src.triggers.triggers import AnomalyTrigger, PeriodicTrigger, ThresholdTrigger
from src.victoriametrics.client import VictoriaMetricsClient

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("decision_engine")
WAITING_POLL_INTERVAL_SEC = 10
TERMINAL_BATCH_STATUSES = {"SUCCESS", "FAILED", "ROLLED_BACK", "CANCEL"}
STATUS_NOTIFY_CHANNEL = "reconfiguration_task_status_changed"


def _classify_batch_statuses(statuses: list[str | None]) -> str:
    if any(status == "CANCEL" for status in statuses):
        return "CANCEL"
    if any(status == "FAILED" for status in statuses):
        return "FAILED"
    if any(status == "ROLLED_BACK" for status in statuses):
        return "ROLLED_BACK"
    return "SUCCESS"


def _build_segmenter():
    segmenter_type = settings.segmenter_type.lower()
    if segmenter_type == "spectral":
        return SpectralSegmenter()
    if segmenter_type == "sa":
        return SimulatedAnnealingSegmenter(
            initial_temperature=settings.sa_initial_temperature,
            cooling_rate=settings.sa_cooling_rate,
            max_iterations=settings.sa_max_iterations,
        )
    if segmenter_type == "greedy":
        return GreedySegmenter(
            max_iterations=settings.greedy_max_iterations,
            lambda_coeff=settings.lambda_coeff,
        )

    logger.warning("Unknown SEGMENTER_TYPE=%s, fallback to greedy", settings.segmenter_type)
    return GreedySegmenter(
        max_iterations=settings.greedy_max_iterations,
        lambda_coeff=settings.lambda_coeff,
    )


async def main() -> None:
    pool = await create_pool()
    producer = TaskProducer()
    vm_client = VictoriaMetricsClient(settings.victoriametrics_url)

    segmenter = _build_segmenter()
    strategy = ThresholdHeuristic(
        anomaly_threshold=settings.anomaly_threshold,
        bandwidth_threshold=settings.bandwidth_threshold,
        segmenter=segmenter,
        constraints=ConstraintSet(),
    )
    current_topology: dict[str, Topology | None] = {"value": None}
    task_builder = TaskBuilder(topology_getter=lambda: current_topology["value"])
    rule_based = RuleBasedStrategy(topology_getter=lambda: current_topology["value"])

    anomaly_trigger = AnomalyTrigger(settings.anomaly_threshold)
    threshold_trigger = ThresholdTrigger(change_percent=settings.trigger_change_percent)
    periodic_trigger = PeriodicTrigger(interval_seconds=settings.periodic_interval_seconds)

    await producer.start()

    # Wait for CM's first successful topology sync before the first decision
    # cycle. Reads the latest row in topology_sync_status (source of truth)
    # after subscribing to the topology_sync_changed NOTIFY, so neither a
    # pre-start sync nor a post-start sync is missed.
    await run_sync_gate(pool)

    fsm_state = "IDLE"
    active_task_id: UUID | None = None
    active_batch_ids: set[UUID] = set()
    active_task: ReconfigurationTask | None = None
    waiting_started_at: datetime | None = None
    last_waiting_poll_at: datetime | None = None
    waiting_lock = asyncio.Lock()
    status_notify_event = asyncio.Event()

    def _normalize_dsn(dsn: str) -> str:
        return dsn.replace("postgresql+asyncpg://", "postgresql://")

    def _evaluate_timeout_result(task: ReconfigurationTask, fresh_state: NetworkState) -> str:
        expected_switches: set[tuple[UUID, str, int]] = set()
        for batch in task.batches:
            for action in batch.actions:
                if action.action_type != ActionType.SWITCH_VLAN:
                    continue
                port = str(action.params.get("port", "")).strip()
                target_vlan = action.params.get("target_vlan_id")
                if not port or target_vlan is None:
                    continue
                expected_switches.add((action.device_id, port, int(target_vlan)))

        if not expected_switches:
            return "unknown"

        current_assignments = {
            (assignment.device_id, assignment.port, assignment.vlan_id)
            for assignment in fresh_state.assignments
        }
        matched = len(expected_switches & current_assignments)
        if matched == len(expected_switches):
            return "applied"
        if matched == 0:
            return "not_applied"
        return "partial"

    async def _set_waiting(task: ReconfigurationTask, batch_ids: set[UUID]) -> None:
        nonlocal fsm_state, active_task_id, active_batch_ids, waiting_started_at, last_waiting_poll_at, active_task
        async with waiting_lock:
            fsm_state = "WAITING"
            active_task = task
            active_task_id = task.id
            active_batch_ids = set(batch_ids)
            waiting_started_at = datetime.now(timezone.utc)
            last_waiting_poll_at = None
            status_notify_event.clear()

    async def _reset_to_idle() -> None:
        nonlocal fsm_state, active_task_id, active_batch_ids, waiting_started_at, last_waiting_poll_at, active_task
        async with waiting_lock:
            fsm_state = "IDLE"
            active_task = None
            active_task_id = None
            active_batch_ids = set()
            waiting_started_at = None
            last_waiting_poll_at = None
            status_notify_event.clear()

    async def _status_listener_loop() -> None:
        while True:
            conn: asyncpg.Connection[Any] | None = None
            try:
                conn = await asyncpg.connect(dsn=_normalize_dsn(settings.postgres_dsn))

                def _on_notify(_connection, _pid, _channel, _payload) -> None:
                    status_notify_event.set()

                await conn.add_listener(STATUS_NOTIFY_CHANNEL, _on_notify)
                logger.info("LISTEN connected on channel=%s", STATUS_NOTIFY_CHANNEL)
                while True:
                    await asyncio.sleep(60)
            except asyncio.CancelledError:
                if conn is not None:
                    await conn.close()
                raise
            except Exception as exc:
                logger.warning(
                    "LISTEN/NOTIFY unavailable (%s). Falling back to polling baseline.",
                    exc,
                )
                if conn is not None:
                    try:
                        await conn.close()
                    except Exception:
                        pass
                await asyncio.sleep(5)

    async def _waiting_monitor_loop() -> None:
        nonlocal fsm_state, active_task_id, active_batch_ids, waiting_started_at, last_waiting_poll_at, active_task
        while True:
            await asyncio.sleep(1)
            async with waiting_lock:
                current_state = fsm_state
                current_task_id = active_task_id
                current_batch_ids = set(active_batch_ids)
                current_waiting_started_at = waiting_started_at
                current_task = active_task
                current_last_poll = last_waiting_poll_at

            if current_state != "WAITING":
                continue
            if current_task_id is None or current_waiting_started_at is None or current_task is None:
                logger.warning("WAITING state corrupted, resetting to IDLE")
                await _reset_to_idle()
                continue

            elapsed = (datetime.now(timezone.utc) - current_waiting_started_at).total_seconds()
            if elapsed >= settings.dedup_waiting_timeout_sec:
                fresh_state = await get_network_state(pool)
                reconciliation = _evaluate_timeout_result(current_task, fresh_state)
                if reconciliation == "applied":
                    logger.warning("Task %s timeout, but topology indicates task was applied", current_task_id)
                elif reconciliation == "partial":
                    logger.error("Task %s timeout with partial topology changes detected", current_task_id)
                elif reconciliation == "not_applied":
                    logger.error("Task %s timeout and no expected topology changes detected", current_task_id)
                else:
                    logger.error("Task %s timeout; cannot validate topology changes", current_task_id)
                await _reset_to_idle()
                continue

            now = datetime.now(timezone.utc)
            poll_due = (
                current_last_poll is None
                or (now - current_last_poll).total_seconds() >= WAITING_POLL_INTERVAL_SEC
            )
            notify_due = status_notify_event.is_set()
            if not poll_due and not notify_due:
                continue

            status_notify_event.clear()
            batch_rows = await get_task_batches(pool, current_task_id)
            async with waiting_lock:
                last_waiting_poll_at = datetime.now(timezone.utc)

            latest_status_by_batch: dict[UUID, str] = {}
            for row in batch_rows:
                batch_id = row.get("batch_id")
                if batch_id is None:
                    continue
                batch_uuid = batch_id if isinstance(batch_id, UUID) else UUID(str(batch_id))
                latest_status_by_batch[batch_uuid] = str(row["status"])

            if current_batch_ids and not current_batch_ids.issubset(set(latest_status_by_batch.keys())):
                logger.info("WAITING task_id=%s status=IN_PROGRESS", current_task_id)
                continue

            all_done = bool(current_batch_ids) and all(
                latest_status_by_batch.get(batch_id) in TERMINAL_BATCH_STATUSES
                for batch_id in current_batch_ids
            )
            if not all_done:
                logger.info("WAITING task_id=%s status=IN_PROGRESS", current_task_id)
                continue

            statuses = [latest_status_by_batch.get(batch_id) for batch_id in current_batch_ids]
            outcome = _classify_batch_statuses(statuses)
            if outcome == "CANCEL":
                logger.warning("Task %s was cancelled (CANCEL status received)", current_task_id)
            elif outcome == "FAILED":
                logger.error("Task %s has FAILED batches", current_task_id)
            elif outcome == "ROLLED_BACK":
                logger.warning("Task %s was rolled back", current_task_id)
            else:
                logger.info("Task %s completed successfully", current_task_id)
            await _reset_to_idle()

    async def handle_metrics_batch(metrics: list[dict]) -> None:
        if not isinstance(metrics, list):
            logger.warning("Skipping payload with invalid metrics format")
            return

        async with waiting_lock:
            if fsm_state == "WAITING":
                logger.info("DE_DUP_WAITING: skip new decision cycle")
                return

        state = await get_network_state(pool)

        anomaly_fired = anomaly_trigger.should_trigger(metrics)
        triggered = (
            anomaly_fired
            or threshold_trigger.should_trigger(metrics)
            or periodic_trigger.should_trigger(metrics)
        )
        if not triggered:
            logger.info("NO_TRIGGER")
            return

        ctx = NetworkContext(
            state=state,
            metrics=metrics,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        # Priority 1: traffic / anomaly strategy. Anomalies outrank everything.
        primary_decision = strategy.decide(ctx)

        # Priority 2: structural invariants (RuleBased) only when traffic is quiet.
        # Lazy: topology is loaded and RuleBased evaluated only if priority 1 was empty.
        async def _rule_based_decision() -> "StrategyDecision":
            current_topology["value"] = await load_topology(pool)
            ctx.topology = current_topology["value"]
            return rule_based.decide(ctx)

        strategy_decision, used_primary = await decide_with_priority(
            primary_decision, _rule_based_decision
        )
        active_strategy = strategy if used_primary else rule_based

        if not strategy_decision.decisions:
            logger.info("NO_ACTION")
            return

        task = task_builder.build(
            decision=strategy_decision,
            state=state,
            initiated_by=f"{active_strategy.get_name()}:{active_strategy.get_version()}",
        )

        non_empty_batches = [batch for batch in task.batches if batch.actions]
        if non_empty_batches:
            await producer.publish(task)
            await _set_waiting(task, {batch.id for batch in non_empty_batches})
            return

        logger.warning("NO_TARGETS: decision taken but no target ports found in DB")

    monitor_task = asyncio.create_task(_waiting_monitor_loop(), name="waiting_monitor")
    listener_task = asyncio.create_task(_status_listener_loop(), name="status_listener")
    try:
        await asyncio.gather(
            run_metrics_poller(vm_client, handle_metrics_batch),
            monitor_task,
            listener_task,
        )
    finally:
        monitor_task.cancel()
        listener_task.cancel()
        await asyncio.gather(monitor_task, listener_task, return_exceptions=True)
        await producer.stop()
        await vm_client.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
