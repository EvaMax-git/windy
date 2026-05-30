"""Storage facade — unified storage path interface.

Provides simple top-level functions for storage path resolution:
    get_storage_path → resolved storage path (local or NAS)
"""

from __future__ import annotations

import logging
from pathlib import Path

from mneme.storage.path_resolver import get_storage_path as _get_storage_path

logger = logging.getLogger("mneme.facade.storage")


def get_storage_path(encrypted: bool = False) -> str:
    """Return the resolved storage path.

    Automatically selects local or NAS storage based on configuration.
    If encrypted=True, returns the keys subdirectory path.

    Falls back to local storage if NAS is unavailable.

    Args:
        encrypted: If True, return the keys (encrypted) directory path.

    Returns:
        Absolute storage path as string.
    """
    try:
        return _get_storage_path(encrypted=encrypted)
    except RuntimeError as e:
        # NAS unavailable — fall back to local storage
        logger.warning("NAS unavailable, falling back to local storage: %s", e)
        from mneme.config import get_settings
        base = Path(get_settings().storage_root)
        if encrypted:
            return str(base / "keys")
        return str(base)
