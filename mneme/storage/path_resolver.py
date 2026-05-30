"""Storage path resolution — local vs NAS auto-detection."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from mneme.config import get_settings

logger = logging.getLogger(__name__)


def check_nas_available(nas_path: str) -> tuple[bool, str]:
    """Check if NAS path is accessible for read/write.

    Returns:
        (is_available, message)
    """
    if not nas_path or not nas_path.strip():
        return False, "NAS 路径未配置"
    p = Path(nas_path)
    if not p.exists():
        return False, f"NAS 路径不存在: {nas_path}"
    if not os.access(p, os.R_OK | os.W_OK):
        return False, f"NAS 路径无读写权限: {nas_path}"
    return True, "NAS 可用"


def get_storage_path(encrypted: bool = False, preferred: str | None = None) -> str:
    """Return the resolved storage path.

    Args:
        encrypted: If True, returns path for encrypted storage (under keys subdirectory).
        preferred: Override path (highest priority if given).

    Returns:
        Absolute storage path as string.

    Priority:
    1. preferred parameter (if given)
    2. NAS path (if storage_mode is 'nas' or 'auto' and NAS is available)
    3. Local storage_root (fallback)
    """
    if preferred:
        return str(Path(preferred).resolve())

    settings = get_settings()

    # Determine base path
    if settings.storage_mode == "local":
        base_path = Path(settings.storage_root)
    elif settings.storage_mode == "nas":
        ok, msg = check_nas_available(settings.nas_path)
        if ok:
            base_path = Path(settings.nas_path)
        else:
            raise RuntimeError(f"NAS 不可用: {msg}")
    else:
        # auto mode
        if settings.nas_path:
            ok, msg = check_nas_available(settings.nas_path)
            if ok:
                base_path = Path(settings.nas_path)
            else:
                logger.warning("NAS 不可用，降级到本地存储: %s", msg)
                base_path = Path(settings.storage_root)
        else:
            base_path = Path(settings.storage_root)

    # Append keys subdirectory for encrypted storage
    if encrypted:
        base_path = base_path / "keys"

    return str(base_path.resolve())
