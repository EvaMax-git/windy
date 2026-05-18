"""Backward-compatibility shim.

New code should import directly from ``mneme.observability``.
"""

from mneme.observability.logging import configure_logging

__all__ = ["configure_logging"]
