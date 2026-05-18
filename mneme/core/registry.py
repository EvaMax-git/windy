"""P5-04 Module Registration Mechanism.

Provides a central registry where each Mneme subsystem can register its
public capabilities. Other modules and external tooling can query the
registry to discover available implementations without hard-coding
import paths.

Usage
-----
.. code-block:: python

    from mneme.core.registry import ModuleRegistry, register

    @register("memory.search", "hybrid")
    class HybridSearchEngine:
        ...

    engines = ModuleRegistry.list("memory.search")
    default = ModuleRegistry.get("memory.search", "hybrid")
"""

from __future__ import annotations

import logging
from typing import Any, Callable, TypeVar, Generic, ClassVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_registry: dict[str, dict[str, Any]] = {}


class ModuleRegistry:
    """Central registry for Mneme subsystem capabilities.

    Each subsystem (memory, context, security, migration, etc.) registers
    one or more named implementations under a ``namespace`` (e.g.
    ``"memory.search"``). Callers resolve implementations by namespace
    and optional name (defaulting to the last registered).

    Thread-safe for reads; registration should happen at import time
    (single-threaded).
    """

    @classmethod
    def register(
        cls,
        namespace: str,
        name: str,
        impl: Any,
        *,
        override: bool = False,
    ) -> None:
        """Register an implementation under a namespace+name.

        Parameters
        ----------
        namespace : str
            Dotted namespace (e.g. ``"memory.search"``).
        name : str
            Implementation name (e.g. ``"hybrid"``, ``"fts_only"``).
        impl : Any
            The class, function, or object to register.
        override : bool
            If True, overwrite an existing entry. Default raises ValueError.
        """
        ns = _registry.setdefault(namespace, {})
        if name in ns and not override:
            raise ValueError(
                f"Implementation '{name}' already registered "
                f"under '{namespace}'. Use override=True to replace."
            )
        ns[name] = impl
        logger.debug("Registered %s / %s → %s", namespace, name, type(impl).__name__)

    @classmethod
    def get(cls, namespace: str, name: str | None = None) -> Any:
        """Retrieve a registered implementation.

        Parameters
        ----------
        namespace : str
            Dotted namespace.
        name : str | None
            Implementation name. If None, returns the most recently
            registered entry for that namespace.

        Returns
        -------
        Any
            The registered implementation.

        Raises
        ------
        KeyError
            If the namespace or name is not found.
        """
        ns = _registry.get(namespace)
        if ns is None:
            raise KeyError(f"Namespace '{namespace}' not registered")

        if name is not None:
            impl = ns.get(name)
            if impl is None:
                raise KeyError(
                    f"Implementation '{name}' not found in '{namespace}'. "
                    f"Available: {list(ns.keys())}"
                )
            return impl

        # Return last-registered (most recent)
        if not ns:
            raise KeyError(f"No implementations registered under '{namespace}'")
        return list(ns.values())[-1]

    @classmethod
    def list(cls, namespace: str | None = None) -> dict[str, dict[str, Any]]:
        """List registered implementations.

        Parameters
        ----------
        namespace : str | None
            Filter to a specific namespace. If None, returns all namespaces.

        Returns
        -------
        dict
            Mapping of namespace → {name → implementation}.
        """
        if namespace is not None:
            ns = _registry.get(namespace, {})
            return {namespace: dict(ns)}
        return {k: dict(v) for k, v in _registry.items()}

    @classmethod
    def unregister(cls, namespace: str, name: str | None = None) -> None:
        """Remove a registered implementation or an entire namespace.

        Parameters
        ----------
        namespace : str
            Namespace to operate on.
        name : str | None
            If given, remove only that name. If None, remove entire namespace.
        """
        if name is None:
            _registry.pop(namespace, None)
            logger.debug("Unregistered namespace '%s'", namespace)
        else:
            ns = _registry.get(namespace)
            if ns is not None:
                ns.pop(name, None)
                if not ns:
                    _registry.pop(namespace, None)
                logger.debug("Unregistered %s / %s", namespace, name)

    @classmethod
    def clear(cls) -> None:
        """Remove all registrations (useful in tests)."""
        _registry.clear()
        logger.debug("Registry cleared")


# ── Decorator shorthand ────────────────────────────────────────────────────

def register(namespace: str, name: str, *, override: bool = False) -> Callable[[T], T]:
    """Decorator to register a class or function.

    Usage::

        @register("memory.search", "hybrid")
        class HybridSearchEngine:
            ...
    """
    def decorator(impl: T) -> T:
        ModuleRegistry.register(namespace, name, impl, override=override)
        return impl
    return decorator
