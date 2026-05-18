"""Pluggable Injection Strategies for Context Assembly.

Strategies encapsulate *how* card-type memory content is loaded and
formatted. New strategies can be registered via::

    from mneme.context.strategies.registry import register_strategy
    from mneme.context.strategies.base import IInjectionStrategy

    @register_strategy("my_strategy", card_types=["new_card_type"])
    class MyStrategy(IInjectionStrategy):
        ...
"""

from mneme.context.strategies.base import (
    IInjectionStrategy,
    StrategyResult,
)
from mneme.context.strategies.registry import (
    DEFAULT_CARD_STRATEGY_MAP,
    get_strategy,
    list_strategies,
    register_strategy,
    get_card_strategy_map,
)

__all__ = [
    "IInjectionStrategy",
    "StrategyResult",
    "DEFAULT_CARD_STRATEGY_MAP",
    "get_strategy",
    "list_strategies",
    "register_strategy",
    "get_card_strategy_map",
]
