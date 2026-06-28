from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from src.config import settings
from src.victoriametrics.client import VictoriaMetricsClient

logger = logging.getLogger(__name__)


async def run_metrics_poller(
    vm_client: VictoriaMetricsClient,
    handler: Callable[[list[dict]], Awaitable[None]],
) -> None:
    try:
        while True:
            metrics = await vm_client.get_latest_metrics()
            if metrics:
                await handler(metrics)
            else:
                logger.warning("No metrics fetched from VictoriaMetrics")
            await asyncio.sleep(settings.metrics_poll_interval_sec)
    except asyncio.CancelledError:
        logger.info("Metrics poller cancelled")
        return
