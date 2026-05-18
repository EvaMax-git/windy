"""Mneme Core — module registration and cross-cutting infrastructure (P5-04).

Provides a central registry for subsystem capability discovery.
"""

from mneme.core.registry import (
    ModuleRegistry,
    register,
)

__all__ = [
    "ModuleRegistry",
    "register",
]
