"""Strategy Registry — pluggable injection strategy discovery.

Uses the central ``ModuleRegistry`` to store strategies.  Built-in
strategies are auto-registered at import time.  Third-party or custom
strategies can be registered via::

    from mneme.context.strategies.registry import register_strategy

    @register_strategy("my_strategy")
    class MyStrategy(IInjectionStrategy):
        ...

The default card-type → strategy mapping is also stored here rather
than in the schema layer, making it configurable without touching
the engine core.
"""

from __future__ import annotations

from typing import Callable, TypeVar

from mneme.context.strategies.base import IInjectionStrategy
from mneme.core.registry import ModuleRegistry

T = TypeVar("T", bound=IInjectionStrategy)

# ── Namespace for injection strategies ────────────────────────────────────
_STRATEGY_NS = "context.injection_strategy"

# ── Default card-type → strategy-name mapping ─────────────────────────────
DEFAULT_CARD_STRATEGY_MAP: dict[str, str] = {
    "soul_card":      "always",
    "identity_card":  "always",
    "tool_catalog":   "always",
    "user_profile":   "moderate",
    "tool_detail":    "on_demand",
    # New card types can be added here without touching the engine.
}


def register_strategy(
    name: str,
    *,
    override: bool = False,
    card_types: list[str] | None = None,
) -> Callable[[T], T]:
    """Decorator to register an injection strategy.

    Usage::

        @register_strategy("summarize", card_types=["user_profile"])
        class SummarizeStrategy(IInjectionStrategy):
            ...

    Parameters
    ----------
    name : str
        Unique strategy name (e.g. ``"always"``, ``"summarize"``).
    override : bool
        If True, replace an existing registration.
    card_types : list[str] | None
        Optional list of card types this strategy should be the default for.
        Updates ``DEFAULT_CARD_STRATEGY_MAP`` in-place.

    Returns
    -------
    Callable
        Decorator that registers the strategy class.
    """

    def decorator(impl: T) -> T:
        ModuleRegistry.register(_STRATEGY_NS, name, impl, override=override)

        # Auto-update card→strategy mapping if card_types provided
        if card_types:
            for ct in card_types:
                DEFAULT_CARD_STRATEGY_MAP[ct] = name

        return impl

    return decorator


def get_strategy(name: str) -> IInjectionStrategy:
    """Resolve a strategy by name.

    Parameters
    ----------
    name : str
        Strategy name.

    Returns
    -------
    IInjectionStrategy
        An instance of the strategy class.

    Raises
    ------
    KeyError
        If the strategy is not registered.
    """
    cls = ModuleRegistry.get(_STRATEGY_NS, name)
    return cls()  # Instantiate the strategy


def list_strategies() -> dict[str, type]:
    """List all registered strategies.

    Returns
    -------
    dict[str, type]
        Mapping of strategy name → strategy class.
    """
    ns = ModuleRegistry.list(_STRATEGY_NS)
    return ns.get(_STRATEGY_NS, {})


def get_card_strategy_map(
    overrides: dict[str, str] | None = None,
    expand_cards: list[str] | None = None,
) -> dict[str, str]:
    """Resolve card-type → strategy-name mapping.

    Parameters
    ----------
    overrides : dict | None
        Per-request strategy overrides (e.g. ``{"soul_card": "moderate"}``).
    expand_cards : list[str] | None
        Card types to force-expand (treated as "always").

    Returns
    -------
    dict[str, str]
        Merged card → strategy mapping.
    """
    merged = dict(DEFAULT_CARD_STRATEGY_MAP)

    if overrides:
        merged.update(overrides)

    if expand_cards:
        for ct in expand_cards:
            merged[ct] = "always"

    return merged


# ── Auto-register built-in strategies ─────────────────────────────────────

# Import at module level to trigger registration.
# Done here rather than in __init__.py to avoid circular imports.

from mneme.context.strategies.always_strategy import AlwaysStrategy  # noqa: E402
from mneme.context.strategies.moderate_strategy import ModerateStrategy  # noqa: E402
from mneme.context.strategies.on_demand_strategy import OnDemandStrategy  # noqa: E402

ModuleRegistry.register(_STRATEGY_NS, AlwaysStrategy.name, AlwaysStrategy)
ModuleRegistry.register(_STRATEGY_NS, ModerateStrategy.name, ModerateStrategy)
ModuleRegistry.register(_STRATEGY_NS, OnDemandStrategy.name, OnDemandStrategy)
