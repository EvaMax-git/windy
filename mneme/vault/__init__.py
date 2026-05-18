"""Vault module — credential encryption, key management, access logging.

P2-08: Vault credential encryption storage.
P2-09: Vault access logs.
"""

from mneme.vault.encryption import VaultEncryption
from mneme.vault.access_log import (
    VaultAccessAction,
    VaultAccessResult,
    write_vault_access_log,
)

__all__ = [
    "VaultAccessAction",
    "VaultAccessResult",
    "VaultEncryption",
    "write_vault_access_log",
]
