"""Persistent encryption key storage."""

from __future__ import annotations

import os
from pathlib import Path

from mneme.config import get_settings


def key_path(name: str = "default") -> Path:
    """Return the full path for a named key file."""
    if not name or not name.strip():
        raise ValueError("密钥名不能为空")
    settings = get_settings()
    return Path(settings.key_dir) / f"{name}.key"


def save_key(key: bytes, name: str = "default") -> Path:
    """Save key to file. Returns the file path."""
    path = key_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows doesn't support chmod
    return path


def load_key(name: str = "default") -> bytes:
    """Load key from file. Raises FileNotFoundError if missing."""
    path = key_path(name)
    if not path.exists():
        raise FileNotFoundError(f"密钥文件不存在: {path}")
    return path.read_bytes()
