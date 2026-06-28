"""Priority orchestration of decision strategies.

Hard priority: the traffic/anomaly strategy (``ThresholdHeuristic``) is consulted
first; the structural ``RuleBasedStrategy`` runs only when the traffic side is
quiet. First strategy with a non-empty decision wins; the secondary factory is
lazy, so the secondary strategy (and its topology load) is not evaluated at all
when the primary already decided to act.
"""

from __future__ import annotations

import inspect
from typing import Awaitable, Callable

from src.strategies.interface import StrategyDecision


async def decide_with_priority(
    primary_decision: StrategyDecision,
    secondary_factory: Callable[[], StrategyDecision | Awaitable[StrategyDecision]],
) -> tuple[StrategyDecision, bool]:
    """Return ``(decision, used_primary)``.

    ``primary_decision`` is already computed (priority 1). If it has decisions it
    wins and ``secondary_factory`` is never called. Otherwise the secondary
    strategy is evaluated lazily; ``secondary_factory`` may be sync or async.
    """
    if primary_decision.decisions:
        return primary_decision, True
    result = secondary_factory()
    if inspect.isawaitable(result):
        result = await result
    return result, False
