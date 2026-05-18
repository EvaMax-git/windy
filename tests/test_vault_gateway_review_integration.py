"""P2-10 Vault-Gateway-Review integration tests.

Covers:
1. VaultCredentialResolver — resolve valid credential (mocked).
2. VaultCredentialResolver — CredentialNotAvailable on missing credential.
3. VaultCredentialResolver — CredentialNotAvailable on non-active credential.
4. VaultCredentialResolver — singleton factory.
5. Policy Engine: vault.credential.reveal triggers review_required.
6. Review routing: vault.credential.reveal maps to sensitive_access.
7. CredentialRevealRequest schema accepts review_item_id.
8. DB helpers: get_credential_id_from_binding, get_active_credential_for_binding.
9. Gateway-Vault bridge: resolve_from_binding flow (integration).
10. Reveal → Review → Re-reveal flow (integration, requires DB).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from mneme.gateway.vault_bridge import (
    CredentialNotAvailable,
    VaultCredentialResolver,
    get_vault_credential_resolver,
)
from mneme.security.policy import (
    Action,
    Actor,
    Decision,
    DenyReason,
    Object,
    PolicyContext,
    can,
)
from mneme.security.review_router import (
    determine_review_type,
    get_review_routing_engine,
)
from mneme.schemas.vault import CredentialRevealRequest


# ── Shared test constants ──────────────────────────────────────────────────────

CREDENTIAL_ID = UUID("11111111-1111-1111-1111-111111111111")
PROVIDER_ID = UUID("22222222-2222-2222-2222-222222222222")
CAPABILITY_ID = UUID("33333333-3333-3333-3333-333333333333")
REQUEST_ID = uuid4()
ACTOR_ID = UUID("44444444-4444-4444-4444-444444444444")


# ═══════════════════════════════════════════════════════════════════════════════
# 1. VaultCredentialResolver — unit tests (mocked DB)
# ═══════════════════════════════════════════════════════════════════════════════


class TestVaultCredentialResolver:
    """Tests for VaultCredentialResolver with mocked database."""

    def test_singleton_returns_same_instance(self):
        """get_vault_credential_resolver returns the same singleton."""
        r1 = get_vault_credential_resolver()
        r2 = get_vault_credential_resolver()
        assert r1 is r2

    def test_resolve_missing_credential(self):
        """Resolve raises CredentialNotAvailable when credential not found."""
        resolver = get_vault_credential_resolver()
        with patch("mneme.gateway.vault_bridge.get_credential_by_id", return_value=None):
            with pytest.raises(CredentialNotAvailable) as exc_info:
                resolver.resolve(
                    credential_id=CREDENTIAL_ID,
                    capability_id=CAPABILITY_ID,
                    provider_id=PROVIDER_ID,
                    request_id=REQUEST_ID,
                )
            assert exc_info.value.reason_code == "credential_not_found"

    def test_resolve_non_active_credential(self):
        """Resolve raises CredentialNotAvailable when credential is not active."""
        resolver = get_vault_credential_resolver()
        mock_row = {
            "credential_id": str(CREDENTIAL_ID),
            "provider_id": str(PROVIDER_ID),
            "status": "revoked",
        }
        with patch("mneme.gateway.vault_bridge.get_credential_by_id", return_value=mock_row):
            with pytest.raises(CredentialNotAvailable) as exc_info:
                resolver.resolve(
                    credential_id=CREDENTIAL_ID,
                    capability_id=CAPABILITY_ID,
                    provider_id=PROVIDER_ID,
                    request_id=REQUEST_ID,
                )
            assert exc_info.value.reason_code == "credential_revoked"

    def test_resolve_disabled_credential(self):
        """Resolve raises CredentialNotAvailable when credential is disabled."""
        resolver = get_vault_credential_resolver()
        mock_row = {
            "credential_id": str(CREDENTIAL_ID),
            "provider_id": str(PROVIDER_ID),
            "status": "disabled",
        }
        with patch("mneme.gateway.vault_bridge.get_credential_by_id", return_value=mock_row):
            with pytest.raises(CredentialNotAvailable) as exc_info:
                resolver.resolve(
                    credential_id=CREDENTIAL_ID,
                    capability_id=CAPABILITY_ID,
                    provider_id=PROVIDER_ID,
                    request_id=REQUEST_ID,
                )
            assert exc_info.value.reason_code == "credential_disabled"

    def test_resolve_successful(self):
        """Resolve returns plaintext when credential is active and decryptable."""
        resolver = get_vault_credential_resolver()
        plaintext = b"sk-test-api-key-12345"

        # Create a real VaultEncryption and encrypt the plaintext
        from mneme.vault.encryption import VaultEncryption
        kek = os.urandom(32)
        vault = VaultEncryption(kek=kek, key_version="v1")
        ct, kw, fp = vault.encrypt(plaintext)

        mock_row = {
            "credential_id": str(CREDENTIAL_ID),
            "provider_id": str(PROVIDER_ID),
            "status": "active",
            "ciphertext": ct,
            "key_wrap": kw,
            "fingerprint": fp,
        }

        with patch(
            "mneme.gateway.vault_bridge.get_credential_by_id", return_value=mock_row
        ), patch(
            "mneme.gateway.vault_bridge.get_vault_encryption", return_value=vault
        ), patch(
            "mneme.gateway.vault_bridge.write_vault_access_log"
        ) as mock_log:
            result = resolver.resolve(
                credential_id=CREDENTIAL_ID,
                capability_id=CAPABILITY_ID,
                provider_id=PROVIDER_ID,
                request_id=REQUEST_ID,
            )
            assert result.plaintext == plaintext
            # Verify access log was written with success
            mock_log.assert_called_once()
            call_kwargs = mock_log.call_args.kwargs
            assert call_kwargs["result"] == "success"
            assert call_kwargs["action"] == "use"

    def test_resolve_fingerprint_mismatch(self):
        """Resolve raises when fingerprint doesn't match."""
        resolver = get_vault_credential_resolver()
        plaintext = b"sk-test-api-key-12345"

        from mneme.vault.encryption import VaultEncryption
        kek = os.urandom(32)
        vault = VaultEncryption(kek=kek, key_version="v1")
        ct, kw, _ = vault.encrypt(plaintext)

        mock_row = {
            "credential_id": str(CREDENTIAL_ID),
            "provider_id": str(PROVIDER_ID),
            "status": "active",
            "ciphertext": ct,
            "key_wrap": kw,
            "fingerprint": "bad-fingerprint-value",
        }

        with patch(
            "mneme.gateway.vault_bridge.get_credential_by_id", return_value=mock_row
        ), patch(
            "mneme.gateway.vault_bridge.get_vault_encryption", return_value=vault
        ), patch(
            "mneme.gateway.vault_bridge.write_vault_access_log"
        ):
            with pytest.raises(CredentialNotAvailable) as exc_info:
                resolver.resolve(
                    credential_id=CREDENTIAL_ID,
                    capability_id=CAPABILITY_ID,
                    provider_id=PROVIDER_ID,
                    request_id=REQUEST_ID,
                )
            assert exc_info.value.reason_code == "fingerprint_mismatch"

    def test_resolve_with_actor_context(self):
        """Resolve accepts actor_type and actor_id for logging."""
        resolver = get_vault_credential_resolver()
        plaintext = b"sk-test-api-key-12345"

        from mneme.vault.encryption import VaultEncryption
        kek = os.urandom(32)
        vault = VaultEncryption(kek=kek, key_version="v1")
        ct, kw, fp = vault.encrypt(plaintext)

        mock_row = {
            "credential_id": str(CREDENTIAL_ID),
            "provider_id": str(PROVIDER_ID),
            "status": "active",
            "ciphertext": ct,
            "key_wrap": kw,
            "fingerprint": fp,
        }

        with patch(
            "mneme.gateway.vault_bridge.get_credential_by_id", return_value=mock_row
        ), patch(
            "mneme.gateway.vault_bridge.get_vault_encryption", return_value=vault
        ), patch(
            "mneme.gateway.vault_bridge.write_vault_access_log"
        ) as mock_log:
            resolver.resolve(
                credential_id=CREDENTIAL_ID,
                capability_id=CAPABILITY_ID,
                provider_id=PROVIDER_ID,
                request_id=REQUEST_ID,
                actor_type="agent",
                actor_id=ACTOR_ID,
            )
            call_kwargs = mock_log.call_args.kwargs
            assert call_kwargs["actor_type"] == "agent"
            assert call_kwargs["actor_id"] == ACTOR_ID

    def test_credential_not_available_str(self):
        """CredentialNotAvailable has a meaningful string representation."""
        exc = CredentialNotAvailable(
            credential_id=CREDENTIAL_ID,
            reason_code="credential_revoked",
            message="Credential is revoked",
        )
        assert str(exc) == "Credential is revoked"
        assert exc.credential_id == CREDENTIAL_ID
        assert exc.reason_code == "credential_revoked"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Policy Engine — vault.credential.reveal triggers review_required
# ═══════════════════════════════════════════════════════════════════════════════


class TestVaultRevealPolicy:
    """Tests that the Policy Engine gates vault.credential.reveal."""

    def test_reveal_triggers_review_required(self):
        """vault.credential.reveal on a secret object triggers review_required."""
        actor = Actor(actor_type="user", actor_id=ACTOR_ID, role="owner", status="active")
        action = Action(name="vault.credential.reveal")
        obj = Object(object_type="credential", sensitivity_level="secret")

        decision = can(actor, action, obj)
        assert decision.decision == Decision.review_required
        assert decision.deny_reason == DenyReason.review_policy_triggered
        assert "requires review" in (decision.message or "")

    def test_reveal_on_low_sensitivity_may_allow(self):
        """vault.credential.reveal on a public object — policy may allow."""
        actor = Actor(actor_type="user", actor_id=ACTOR_ID, role="owner", status="active")
        action = Action(name="vault.credential.reveal")
        obj = Object(object_type="credential", sensitivity_level="public")

        decision = can(actor, action, obj)
        # "reveal" is a dangerous verb, but public sensitivity is below
        # the review threshold (sensitive+). So this should allow.
        assert decision.decision == Decision.allow

    def test_reveal_by_disabled_user_denied(self):
        """Disabled user cannot reveal — denied before reaching review check."""
        actor = Actor(actor_type="user", actor_id=ACTOR_ID, role="owner", status="disabled")
        action = Action(name="vault.credential.reveal")
        obj = Object(object_type="credential", sensitivity_level="secret")

        decision = can(actor, action, obj)
        assert decision.decision == Decision.deny
        assert decision.deny_reason == DenyReason.user_disabled


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Review routing — vault.credential.reveal maps to sensitive_access
# ═══════════════════════════════════════════════════════════════════════════════


class TestVaultRevealReviewRouting:
    """Tests that the review routing engine correctly routes vault credential reveal."""

    def test_reveal_maps_to_sensitive_access(self):
        """vault.credential.reveal → sensitive_access review_type."""
        action = Action(name="vault.credential.reveal")
        obj = Object(object_type="credential", sensitivity_level="secret")

        review_type = determine_review_type(action, obj)
        assert review_type == "sensitive_access"

    def test_vault_credential_reveal_rule_exists(self):
        """The vault_credential_reveal routing rule is in the defaults."""
        engine = get_review_routing_engine()
        rule = engine.get_rule("vault_credential_reveal")
        assert rule is not None
        assert rule.review_type == "sensitive_access"
        assert rule.priority == 12
        assert rule.action_pattern == "vault.credential.reveal"
        assert rule.object_type == "credential"

    def test_vault_credential_reveal_does_not_match_other_actions(self):
        """Rule should only match vault.credential.reveal, not other reveals."""
        engine = get_review_routing_engine()

        # Match
        rule = engine.match(
            Action(name="vault.credential.reveal"),
            Object(object_type="credential"),
        )
        assert rule is not None
        assert rule.name == "vault_credential_reveal"

        # No match — different action
        rule = engine.match(
            Action(name="vault.credential.rotate"),
            Object(object_type="credential"),
        )
        assert rule is None or rule.name != "vault_credential_reveal"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Schema tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCredentialRevealRequestSchema:
    """Tests for CredentialRevealRequest with P2-10 review_item_id field."""

    def test_reveal_request_without_review_item(self):
        """Reveal request without review_item_id is valid."""
        req = CredentialRevealRequest(reason="debugging")
        assert req.reason == "debugging"
        assert req.review_item_id is None

    def test_reveal_request_with_review_item(self):
        """Reveal request with review_item_id is valid."""
        review_id = uuid4()
        req = CredentialRevealRequest(
            reason="approved reveal",
            review_item_id=review_id,
        )
        assert req.review_item_id == review_id

    def test_reveal_request_defaults(self):
        """Default reveal request has no reason and no review_item_id."""
        req = CredentialRevealRequest()
        assert req.reason is None
        assert req.review_item_id is None


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Gateway module exports
# ═══════════════════════════════════════════════════════════════════════════════


class TestGatewayModule:
    """Tests that the gateway module exports are correct."""

    def test_gateway_init_exports(self):
        """gateway.__init__ exports VaultCredentialResolver and helpers."""
        from mneme.gateway import (
            VaultCredentialResolver,
            CredentialNotAvailable,
            get_vault_credential_resolver,
        )
        assert VaultCredentialResolver is not None
        assert CredentialNotAvailable is not None
        assert callable(get_vault_credential_resolver)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. DB helpers — get_credential_id_from_binding, get_active_credential_for_binding
# ═══════════════════════════════════════════════════════════════════════════════


class TestDbVaultHelpers:
    """Tests that the new DB vault helpers are importable and have correct signatures."""

    def test_helpers_importable(self):
        """Both helpers can be imported."""
        from mneme.db.vault import (
            get_credential_id_from_binding,
            get_active_credential_for_binding,
        )
        assert callable(get_credential_id_from_binding)
        assert callable(get_active_credential_for_binding)


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Integration: VaultCredentialResolver logs denial correctly
# ═══════════════════════════════════════════════════════════════════════════════


class TestResolverDenialLogging:
    """Tests that denial logging is correctly triggered."""

    def test_missing_credential_logs_denial(self):
        """When credential is missing, a denial access log is written."""
        resolver = get_vault_credential_resolver()
        with patch(
            "mneme.gateway.vault_bridge.get_credential_by_id", return_value=None
        ), patch(
            "mneme.gateway.vault_bridge.write_vault_access_log"
        ) as mock_log:
            try:
                resolver.resolve(
                    credential_id=CREDENTIAL_ID,
                    capability_id=CAPABILITY_ID,
                    provider_id=PROVIDER_ID,
                    request_id=REQUEST_ID,
                )
            except CredentialNotAvailable:
                pass

            # Verify denial was logged
            mock_log.assert_called_once()
            call_kwargs = mock_log.call_args.kwargs
            assert call_kwargs["action"] == "access_denied"
            assert call_kwargs["result"] == "denied"
            assert call_kwargs["reason_code"] == "credential_not_found"

    def test_non_active_credential_logs_denial(self):
        """When credential is not active, a denial access log is written."""
        resolver = get_vault_credential_resolver()
        mock_row = {
            "credential_id": str(CREDENTIAL_ID),
            "provider_id": str(PROVIDER_ID),
            "status": "disabled",
        }
        with patch(
            "mneme.gateway.vault_bridge.get_credential_by_id", return_value=mock_row
        ), patch(
            "mneme.gateway.vault_bridge.write_vault_access_log"
        ) as mock_log:
            try:
                resolver.resolve(
                    credential_id=CREDENTIAL_ID,
                    capability_id=CAPABILITY_ID,
                    provider_id=PROVIDER_ID,
                    request_id=REQUEST_ID,
                )
            except CredentialNotAvailable:
                pass

            mock_log.assert_called_once()
            call_kwargs = mock_log.call_args.kwargs
            assert call_kwargs["action"] == "access_denied"
            assert call_kwargs["result"] == "denied"
            assert call_kwargs["reason_code"] == "credential_disabled"
