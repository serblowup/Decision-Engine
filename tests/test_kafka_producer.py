from __future__ import annotations

import pytest
from aiokafka.errors import KafkaConnectionError

from src.kafka import producer as producer_module
from src.kafka.producer import TaskProducer


class _FakeProducer:
    def __init__(self, fail_start: bool = False):
        self.fail_start = fail_start
        self.started = False

    async def start(self):
        if self.fail_start:
            raise KafkaConnectionError("bootstrap failed")
        self.started = True

    async def stop(self):
        return None

    async def send_and_wait(self, topic, payload):
        return None


@pytest.mark.asyncio
async def test_start_retries_then_succeeds(monkeypatch):
    created = []

    def factory(*args, **kwargs):
        idx = len(created)
        inst = _FakeProducer(fail_start=(idx == 0))
        created.append(inst)
        return inst

    async def no_sleep(_):
        return None

    monkeypatch.setattr(producer_module, "AIOKafkaProducer", factory)
    monkeypatch.setattr(producer_module.asyncio, "sleep", no_sleep)

    producer = TaskProducer()
    await producer.start()

    assert len(created) == 2
    assert created[0].started is False
    assert created[1].started is True


@pytest.mark.asyncio
async def test_start_fails_after_max_retries(monkeypatch):
    attempts = {"count": 0}

    def factory(*args, **kwargs):
        attempts["count"] += 1
        return _FakeProducer(fail_start=True)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(producer_module, "AIOKafkaProducer", factory)
    monkeypatch.setattr(producer_module.asyncio, "sleep", no_sleep)

    producer = TaskProducer()
    with pytest.raises(KafkaConnectionError):
        await producer.start()

    assert attempts["count"] == producer_module.MAX_RETRIES
