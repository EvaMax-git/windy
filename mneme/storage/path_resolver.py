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


def get_storage_path(preferred: str | None = None) -> Path:
    """Return the resolved storage path.

    Priority:
    1. preferred parameter (if given)
    2. NAS path (if storage_mode is 'nas' or 'auto' and NAS is available)
    3. Local storage_root (fallback)
    """
    if preferred:
        return Path(preferred)

    settings = get_settings()

    if settings.storage_mode == "local":
        return Path(settings.storage_root)

    if settings.storage_mode == "nas":
        ok, msg = check_nas_available(settings.nas_path)
        if ok:
            return Path(settings.nas_path)
        raise RuntimeError(f"NAS 不可用: {msg}")

    # auto mode
    if settings.nas_path:
        ok, msg = check_nas_available(settings.nas_path)
        if ok:
            return Path(settings.nas_path)
        logger.warning("NAS 不可用，降级到本地存储: %s", msg)

    return Path(settings.storage_root)
