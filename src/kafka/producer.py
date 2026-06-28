from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from aiokafka import AIOKafkaProducer
from aiokafka.errors import KafkaConnectionError

from src.config import settings
from src.models.operations import ReconfigurationTask

logger = logging.getLogger(__name__)

MAX_RETRIES = 10
RETRY_DELAY_SEC = 10


class TaskProducer:
    def __init__(self) -> None:
        self._bootstrap = settings.kafka_bootstrap
        self._topic = settings.kafka_tasks_topic
        self._producer: AIOKafkaProducer | None = None
        self._bootstrap_marker_enabled: bool = settings.kafka_bootstrap_marker_enabled

    async def start(self) -> None:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self._producer = AIOKafkaProducer(
                    bootstrap_servers=self._bootstrap,
                    value_serializer=lambda value: json.dumps(value).encode("utf-8"),
                )
                await self._producer.start()
                logger.info("Kafka producer started for topic=%s", self._topic)
                if self._bootstrap_marker_enabled:
                    await self._publish_bootstrap_marker()
                return
            except KafkaConnectionError as exc:
                if attempt == MAX_RETRIES:
                    logger.error(
                        "Kafka unavailable after %s retries, giving up.",
                        MAX_RETRIES,
                    )
                    raise
                logger.warning(
                    "Kafka connection failed (%s/%s): %s. Retrying in %ss...",
                    attempt,
                    MAX_RETRIES,
                    exc,
                    RETRY_DELAY_SEC,
                )
                await asyncio.sleep(RETRY_DELAY_SEC)

    async def _publish_bootstrap_marker(self) -> None:
        if self._producer is None:
            return
        marker = {
            "type": "decision_engine_bootstrap",
            "schema_version": "1.0",
            "source": "decision-engine",
            "status": "producer_connected",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": "Kafka producer is connected and writable",
        }
        await self._producer.send_and_wait(self._topic, marker)
        logger.info("Bootstrap marker published to topic=%s", self._topic)

    async def stop(self) -> None:
        if self._producer is None:
            return
        await self._producer.stop()
        logger.info("Kafka producer stopped")

    async def publish(self, task: ReconfigurationTask) -> None:
        if self._producer is None:
            raise RuntimeError("Producer is not started")
        payload = task.model_dump(mode="json")
        await self._producer.send_and_wait(self._topic, payload)
        logger.info("Published task task_id=%s batches=%s", task.id, len(task.batches))
