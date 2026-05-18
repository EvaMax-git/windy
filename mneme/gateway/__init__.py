"""Gateway module — provider routing, capability bindings, unified call entry.

P2-11: Gateway provider/model registration.
P2-12: Gateway unified call entry + audit.
P2-10: Vault credential resolution for Gateway calls.
"""

from mneme.gateway.call import (
    BindingNotFoundError,
    BudgetDeniedError,
    CredentialResolutionError,
    Gateway,
    GatewayError,
    ProviderCallError,
    ProviderTimeoutError,
    get_gateway,
)
from mneme.gateway.vault_bridge import (
    CredentialNotAvailable,
    ResolvedCredential,
    VaultCredentialResolver,
    get_vault_credential_resolver,
)

__all__ = [
    # Vault bridge
    "CredentialNotAvailable",
    "ResolvedCredential",
    "VaultCredentialResolver",
    "get_vault_credential_resolver",
    # Gateway call
    "Gateway",
    "get_gateway",
    "GatewayError",
    "BindingNotFoundError",
    "BudgetDeniedError",
    "CredentialResolutionError",
    "ProviderCallError",
    "ProviderTimeoutError",
]
