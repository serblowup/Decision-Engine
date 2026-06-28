"""Priority orchestration: ThresholdHeuristic outranks RuleBasedStrategy."""

from __future__ import annotations

from src.models.operations import BatchCriticality
from src.strategies.interface import StrategyDecision, VlanDecision
from src.strategies.orchestrator import decide_with_priority


def _nonempty():
    return StrategyDecision(decisions=[VlanDecision(10, "ISOLATE", BatchCriticality.CRITICAL)])


def _empty():
    return StrategyDecision(decisions=[])


async def test_primary_wins_and_short_circuits_secondary():
    called = {"secondary": False}

    def secondary():
        called["secondary"] = True
        return _nonempty()

    decision, used_primary = await decide_with_priority(_nonempty(), secondary)
    assert used_primary is True
    assert called["secondary"] is False  # RuleBased not consulted when Threshold fires
    assert decision.decisions


async def test_secondary_runs_when_primary_quiet():
    called = {"secondary": False}

    def secondary():
        called["secondary"] = True
        return _nonempty()

    decision, used_primary = await decide_with_priority(_empty(), secondary)
    assert used_primary is False
    assert called["secondary"] is True
    assert decision.decisions


async def test_async_secondary_supported():
    async def secondary():
        return _nonempty()

    decision, used_primary = await decide_with_priority(_empty(), secondary)
    assert used_primary is False
    assert decision.decisions


async def test_both_empty_returns_empty_secondary():
    decision, used_primary = await decide_with_priority(_empty(), _empty)
    assert used_primary is False
    assert decision.decisions == []
