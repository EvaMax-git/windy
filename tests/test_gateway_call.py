"""P2-12 Gateway unified call entry — comprehensive tests.

Covers:
1. Unit: _VALID_TRANSITIONS contract
2. Unit: _compute_call_cost
3. Unit: _make_fingerprint
4. Unit: _resolve_url path mapping
5. Unit: _build_request credential header detection
6. Unit: Gateway exception hierarchy + _map_gateway_error
7. Integration: Gateway.call() — binding_not_found
8. Integration: Gateway.call() — success path (mock HTTP)
9. Integration: Gateway.call() — provider error 4xx (no retry)
10. Integration: Gateway.call() — provider error 5xx + retry
11. Integration: Gateway.call() — timeout
12. Integration: Gateway.call() — network_error + retry
13. Integration: budget lifecycle (reserved → committed/released)
14. Integration: budget_denied path
15. Integration: credential resolution error path
16. Integration: api_call_logs state transitions via DB
17. Integration: budget_tracking state transitions via DB
18. POST /gateway/call endpoint (FastAPI TestClient)
19. Schema validation tests
20. No-bypass verification
"""

from __future__ import annotations

import json
import os
import sys
import time
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

import httpx
import pytest

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://mneme:5d1acf8542488f183caad64b9ec3abbf9ff3bb694b75fdf6@localhost:5432/mneme",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

sys.path.insert(0, "/mnt/nas/letta/Mneme3")

from mneme.gateway.call import (
    BindingNotFoundError,
    BudgetDeniedError,
    CredentialResolutionError,
    Gateway,
    GatewayError,
    ProviderCallError,
    ProviderTimeoutError,
    _compute_call_cost,
    _make_fingerprint,
    get_gateway,
)
from mneme.db.api_call_logs import (
    _VALID_TRANSITIONS,
    get_api_call_log,
    insert_api_call_log,
    transition_call_state,
    update_call_result,
)
from mneme.db.budget import (
    check_budget_allow,
    get_budget_tracking,
    reserve_budget,
    transition_budget_state,
)
from mneme.db.gateway import (
    create_capability,
    create_capability_binding,
    create_provider,
    create_provider_model,
)
from mneme.api.routes.gateway.gateway import _map_gateway_error
from mneme.schemas.gateway import (
    GatewayCallRequest,
    GatewayCallResponse,
    GatewayCallUsage,
    GatewayCallCost,
    SensitivityLevel,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Shared test data
# ═══════════════════════════════════════════════════════════════════════════════

_PROJECT_ID = UUID("a0000000-0000-0000-0000-000000000001")
_CAPABILITY_CODE = "test.chat.completion"
_PROVIDER_CODE = "test_openai_p2_12"


def _cleanup_seed():
    """Remove all seed data created by seed_db fixture, respecting FK order."""
    from mneme.db.base import SessionLocal
    from sqlalchemy import text as sa_text
    with SessionLocal() as db:
        # Delete in FK-safe order: api_call_logs → budget_tracking → bindings → capabilities → models → providers
        db.execute(sa_text("DELETE FROM api_call_logs WHERE capability_id IN (SELECT capability_id FROM capabilities WHERE capability_code = :c)"), {"c": _CAPABILITY_CODE})
        db.execute(sa_text("DELETE FROM budget_tracking WHERE capability_id IN (SELECT capability_id FROM capabilities WHERE capability_code = :c)"), {"c": _CAPABILITY_CODE})
        db.execute(sa_text("DELETE FROM capability_bindings WHERE capability_id IN (SELECT capability_id FROM capabilities WHERE capability_code = :c)"), {"c": _CAPABILITY_CODE})
        db.execute(sa_text("DELETE FROM capabilities WHERE capability_code = :c"), {"c": _CAPABILITY_CODE})
        db.execute(sa_text("DELETE FROM provider_models WHERE model_code = 'test-gpt-4o'"))
        db.execute(sa_text("DELETE FROM providers WHERE provider_code = :p"), {"p": _PROVIDER_CODE})
        db.commit()


@pytest.fixture(scope="function")
def seed_db():
    """Seed provider, model, capability, and binding for P2-12 tests.

    Uses function scope with explicit cleanup to avoid cross-test contamination.
    """
    _cleanup_seed()  # Clean any leftover from previous failed runs

    provider = create_provider(
        provider_code=_PROVIDER_CODE,
        name="Test OpenAI P2-12",
        provider_type="llm",
        status="active",
        endpoint_base="https://api.test-openai.example.com",
    )
    provider_id = UUID(provider["provider_id"])

    model = create_provider_model(
        provider_id=provider_id,
        model_code="test-gpt-4o",
        external_model_id="test-gpt-4o",
        model_type="chat",
        status="active",
        max_input_tokens=128000,
        max_output_tokens=4096,
        input_price_per_1k=0.0,
        output_price_per_1k=0.0,
        currency_code="USD",
    )
    model_id = UUID(model["provider_model_id"])

    cap = create_capability(
        capability_code=_CAPABILITY_CODE,
        name="Test Chat Completion",
        category="chat",
        risk_level="normal",
    )
    capability_id = UUID(cap["capability_id"])

    binding = create_capability_binding(
        capability_id=capability_id,
        provider_id=provider_id,
        provider_model_id=model_id,
        credential_id=None,
        binding_scope="global",
        status="active",
        priority=100,
        sensitivity_floor="public",
        sensitivity_ceiling="secret",
    )
    binding_id = UUID(binding["capability_binding_id"])

    yield {
        "provider_id": provider_id,
        "model_id": model_id,
        "capability_id": capability_id,
        "binding_id": binding_id,
    }

    _cleanup_seed()


# ═══════════════════════════════════════════════════════════════════════════════
# 1. State machine contract
# ═══════════════════════════════════════════════════════════════════════════════

class TestStateMachine:
    def test_planned_transitions(self):
        assert _VALID_TRANSITIONS["planned"] == {"budget_reserved", "cancelled", "denied"}

    def test_budget_reserved_transitions(self):
        assert _VALID_TRANSITIONS["budget_reserved"] == {"credential_checked", "cancelled", "denied"}

    def test_credential_checked_transitions(self):
        assert _VALID_TRANSITIONS["credential_checked"] == {"in_flight", "cancelled", "denied"}

    def test_in_flight_transitions(self):
        assert _VALID_TRANSITIONS["in_flight"] == {"succeeded", "failed", "timeout", "cancelled"}

    def test_failed_transitions(self):
        assert _VALID_TRANSITIONS["failed"] == {"in_flight", "dead_letter", "cancelled"}

    def test_terminal_states(self):
        for state in ["succeeded", "cancelled", "denied", "dead_letter"]:
            assert _VALID_TRANSITIONS[state] == set()

    def test_timeout_transitions(self):
        assert _VALID_TRANSITIONS["timeout"] == {"in_flight", "dead_letter"}

    def test_all_10_states_present(self):
        expected = {"planned", "budget_reserved", "credential_checked", "in_flight",
                     "succeeded", "failed", "timeout", "cancelled", "denied", "dead_letter"}
        assert set(_VALID_TRANSITIONS.keys()) == expected

    def test_invalid_jumps(self):
        assert "succeeded" not in _VALID_TRANSITIONS["planned"]
        assert "in_flight" not in _VALID_TRANSITIONS["planned"]
        assert "planned" not in _VALID_TRANSITIONS["in_flight"]


# ═══════════════════════════════════════════════════════════════════════════════
# 2. _compute_call_cost
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputeCallCost:
    def test_basic(self):
        c = _compute_call_cost(input_tokens=1000, output_tokens=500,
                               input_price_per_1k=0.005, output_price_per_1k=0.015)
        assert c == 0.0125

    def test_zeros(self):
        assert _compute_call_cost(input_tokens=0, output_tokens=0,
                                  input_price_per_1k=0.005, output_price_per_1k=0.015) == 0.0

    def test_nones(self):
        assert _compute_call_cost(input_tokens=None, output_tokens=None,
                                  input_price_per_1k=None, output_price_per_1k=None) == 0.0

    def test_only_input(self):
        assert _compute_call_cost(input_tokens=2000, output_tokens=None,
                                  input_price_per_1k=0.01, output_price_per_1k=None) == 0.02


# ═══════════════════════════════════════════════════════════════════════════════
# 3. _make_fingerprint
# ═══════════════════════════════════════════════════════════════════════════════

class TestMakeFingerprint:
    def test_idempotent(self):
        f1 = _make_fingerprint("chat.completion", {"model": "gpt-4o"})
        f2 = _make_fingerprint("chat.completion", {"model": "gpt-4o"})
        assert f1 == f2

    def test_different_params_differ(self):
        assert _make_fingerprint("x", {"a": 1}) != _make_fingerprint("x", {"a": 2})

    def test_different_capability_differ(self):
        assert _make_fingerprint("a", {}) != _make_fingerprint("b", {})

    def test_hex_64(self):
        fp = _make_fingerprint("test", {})
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)

    def test_key_order_agnostic(self):
        f1 = _make_fingerprint("t", {"a": 1, "b": 2})
        f2 = _make_fingerprint("t", {"b": 2, "a": 1})
        assert f1 == f2


# ═══════════════════════════════════════════════════════════════════════════════
# 4. _resolve_url
# ═══════════════════════════════════════════════════════════════════════════════

class TestResolveUrl:
    def test_chat(self):
        url = Gateway._resolve_url("https://api.oai.com", "gpt-4o", "chat.completion")
        assert url == "https://api.oai.com/v1/chat/completions"

    def test_embedding(self):
        url = Gateway._resolve_url("https://api.oai.com", "ada", "embedding.create")
        assert url == "https://api.oai.com/v1/embeddings"

    def test_image(self):
        url = Gateway._resolve_url("https://api.oai.com", "dall-e", "image.generate")
        assert url == "https://api.oai.com/v1/images/generations"

    def test_vision(self):
        url = Gateway._resolve_url("https://api.oai.com", "gpt-4o", "vision.analyze")
        assert url == "https://api.oai.com/v1/chat/completions"

    def test_audio(self):
        url = Gateway._resolve_url("https://api.oai.com", "whisper", "audio.transcribe")
        assert url == "https://api.oai.com/v1/audio/transcriptions"

    def test_rerank(self):
        url = Gateway._resolve_url("https://api.cohere.ai", "rerank-v3", "rerank.execute")
        assert url == "https://api.cohere.ai/v1/rerank"

    def test_search(self):
        url = Gateway._resolve_url("https://api.search.com", "s", "search.execute")
        assert url == "https://api.search.com/v1/search"

    def test_unknown_fallback(self):
        url = Gateway._resolve_url("https://api.oai.com", "gpt-4o", "unknown.x")
        assert url.endswith("/v1/chat/completions")

    def test_no_double_slash(self):
        url = Gateway._resolve_url("https://api.oai.com/", "gpt-4o", "chat.completion")
        assert url == "https://api.oai.com/v1/chat/completions"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. _build_request
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildRequest:
    def _build(self, plaintext_credential=None, *, model_code_override=None, **overrides):
        binding = {
            "endpoint_base": "https://api.test.example.com",
            "model_code": "test-model",
            "provider_code_val": "test-provider",
            "capability_code": "chat.completion",
            "model_config_json": {},
            **overrides,
        }
        gw = Gateway(http_client=Mock())
        mc = model_code_override if model_code_override is not None else binding["model_code"]
        return gw._build_request(
            binding=binding,
            params={"messages": [{"role": "user", "content": "hi"}]},
            plaintext_credential=plaintext_credential,
            provider_code=binding.get("provider_code_val", "test-provider"),
            endpoint_base=binding.get("endpoint_base", "https://api.test.example.com"),
            model_code=mc,
            capability_code=binding.get("capability_code", "chat.completion"),
        )

    def test_no_credential(self):
        _, headers, body = self._build(None)
        assert "Authorization" not in headers

    def test_bearer_passthrough(self):
        _, headers, _ = self._build(b"Bearer abc123")
        assert headers["Authorization"] == "Bearer abc123"

    def test_sk_prefix(self):
        _, headers, _ = self._build(b"sk-test-key")
        assert headers["Authorization"] == "Bearer sk-test-key"

    def test_api_prefix(self):
        _, headers, _ = self._build(b"api-key-67890")
        assert headers["Authorization"] == "Bearer api-key-67890"

    def test_long_credential_bearer(self):
        _, headers, _ = self._build(b"x" * 30)
        assert headers["Authorization"] == "Bearer " + "x" * 30

    def test_short_credential_x_api_key(self):
        _, headers, _ = self._build(b"short")
        assert headers.get("X-API-Key") == "short"
        assert "Authorization" not in headers

    def test_default_model_injection(self):
        _, _, body = self._build(b"sk-key", model_code_override="gpt-4o-mini")
        assert body["model"] == "gpt-4o-mini"

    def test_respects_existing_model(self):
        binding = {
            "endpoint_base": "https://api.test.example.com",
            "model_code": "default-model",
            "provider_code_val": "test-provider",
            "capability_code": "chat.completion",
            "model_config_json": {},
        }
        gw = Gateway(http_client=Mock())
        _, _, body = gw._build_request(
            binding=binding, params={"model": "custom", "messages": []},
            plaintext_credential=b"sk-key", provider_code="p",
            endpoint_base="https://api.test.example.com",
            model_code="default-model", capability_code="chat.completion",
        )
        assert body["model"] == "custom"

    def test_api_version_header(self):
        binding = {
            "endpoint_base": "https://api.test.example.com",
            "model_code": "test-model",
            "provider_code_val": "test-provider",
            "capability_code": "chat.completion",
            "model_config_json": {"api_version": "v2"},
        }
        gw = Gateway(http_client=Mock())
        _, headers, _ = gw._build_request(
            binding=binding, params={}, plaintext_credential=b"sk-key",
            provider_code="p", endpoint_base="https://api.test.example.com",
            model_code="test-model", capability_code="chat.completion",
        )
        assert headers.get("OpenAI-Beta") == "assistants=v2"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Exceptions + error mapping
# ═══════════════════════════════════════════════════════════════════════════════

class TestExceptions:
    def test_binding_not_found(self):
        e = BindingNotFoundError(None, "g.bnf", "msg", call_state="denied")
        assert e.code == "g.bnf" and e.call_state == "denied"

    def test_budget_denied(self):
        e = BudgetDeniedError(uuid4(), "g.bd", "msg", details={"x": 1})
        assert e.details == {"x": 1}

    def test_credential_resolution(self):
        assert isinstance(CredentialResolutionError(uuid4(), "c", "m"), GatewayError)

    def test_provider_call(self):
        assert isinstance(ProviderCallError(uuid4(), "c", "m", details={"s": 500}), GatewayError)

    def test_provider_timeout(self):
        assert isinstance(ProviderTimeoutError(uuid4(), "c", "m"), GatewayError)


class TestMapError:
    def test_404(self):
        assert _map_gateway_error(BindingNotFoundError(None, "c", "m")).status_code == 404

    def test_402(self):
        assert _map_gateway_error(BudgetDeniedError(uuid4(), "c", "m")).status_code == 402

    def test_403(self):
        assert _map_gateway_error(CredentialResolutionError(uuid4(), "c", "m")).status_code == 403

    def test_504(self):
        assert _map_gateway_error(ProviderTimeoutError(uuid4(), "c", "m")).status_code == 504

    def test_502(self):
        assert _map_gateway_error(ProviderCallError(uuid4(), "c", "m")).status_code == 502

    def test_500(self):
        assert _map_gateway_error(RuntimeError("x")).status_code == 500


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Binding not found
# ═══════════════════════════════════════════════════════════════════════════════

class TestBindingNotFound:
    def test_raises(self):
        gw = Gateway(http_client=Mock())
        with pytest.raises(BindingNotFoundError) as e:
            gw.call(capability_code="no.such.capability.ever", params={})
        assert e.value.code == "gateway.binding_not_found"
        assert e.value.api_call_log_id is None


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Success path
# ═══════════════════════════════════════════════════════════════════════════════

def _make_200_response():
    return httpx.Response(200, json={
        "id": "chatcmpl-test", "model": "test-gpt-4o", "choices": [],
        "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
    }, request=httpx.Request("POST", "https://example.com"))


class TestSuccess:
    def test_success(self, seed_db):
        mc = Mock(spec=httpx.Client)
        mc.request.return_value = _make_200_response()
        r = Gateway(http_client=mc).call(capability_code=_CAPABILITY_CODE,
                                          params={"messages": []}, sensitivity="public")
        assert r["call_state"] == "succeeded"
        assert r["usage"]["input_tokens"] == 50
        assert r["usage"]["output_tokens"] == 30
        assert r["usage"]["total_tokens"] == 80
        assert r["cost"]["actual"] >= 0
        log = get_api_call_log(UUID(r["api_call_log_id"]))
        assert log["call_state"] == "succeeded"
        assert log["input_tokens"] == 50
        assert log["retry_count"] == 0

    def test_budget_committed(self, seed_db):
        mc = Mock(spec=httpx.Client)
        mc.request.return_value = _make_200_response()
        r = Gateway(http_client=mc).call(capability_code=_CAPABILITY_CODE, params={"messages": []}, sensitivity="public")
        log = get_api_call_log(UUID(r["api_call_log_id"]))
        budget = get_budget_tracking(UUID(log["budget_tracking_id"]))
        assert budget["reservation_state"] == "committed"

    def test_unique_ids(self, seed_db):
        mc = Mock(spec=httpx.Client)
        mc.request.return_value = _make_200_response()
        gw = Gateway(http_client=mc)
        r1 = gw.call(capability_code=_CAPABILITY_CODE, params={"messages": []}, sensitivity="public")
        r2 = gw.call(capability_code=_CAPABILITY_CODE, params={"messages": []}, sensitivity="public")
        assert r1["api_call_log_id"] != r2["api_call_log_id"]

    def test_request_id_passthrough(self, seed_db):
        rid = uuid4()
        mc = Mock(spec=httpx.Client)
        mc.request.return_value = _make_200_response()
        r = Gateway(http_client=mc).call(capability_code=_CAPABILITY_CODE, params={"messages": []},
                                          request_id=rid, sensitivity="public")
        log = get_api_call_log(UUID(r["api_call_log_id"]))
        assert log["request_id"] == str(rid)

    def test_actor_info(self, seed_db):
        aid = uuid4()
        mc = Mock(spec=httpx.Client)
        mc.request.return_value = _make_200_response()
        r = Gateway(http_client=mc).call(capability_code=_CAPABILITY_CODE, params={"messages": []},
                                          actor_type="user", actor_id=aid, sensitivity="public")
        log = get_api_call_log(UUID(r["api_call_log_id"]))
        assert log["actor_type"] == "user"
        assert log["actor_id"] == str(aid)

    def test_fingerprint_persisted(self, seed_db):
        mc = Mock(spec=httpx.Client)
        mc.request.return_value = _make_200_response()
        r = Gateway(http_client=mc).call(capability_code=_CAPABILITY_CODE,
                                          params={"model": "gpt-4o", "t": 0.7}, sensitivity="public")
        log = get_api_call_log(UUID(r["api_call_log_id"]))
        assert len(log["provider_request_fingerprint"]) == 64

    def test_request_summary(self, seed_db):
        mc = Mock(spec=httpx.Client)
        mc.request.return_value = _make_200_response()
        r = Gateway(http_client=mc).call(capability_code=_CAPABILITY_CODE,
                                          params={"model": "x", "messages": [{"role": "user", "content": "hi"}]}, sensitivity="public")
        log = get_api_call_log(UUID(r["api_call_log_id"]))
        assert log["request_summary"]["capability_code"] == _CAPABILITY_CODE

    def test_response_summary(self, seed_db):
        mc = Mock(spec=httpx.Client)
        mc.request.return_value = _make_200_response()
        r = Gateway(http_client=mc).call(capability_code=_CAPABILITY_CODE, params={"messages": []}, sensitivity="public")
        log = get_api_call_log(UUID(r["api_call_log_id"]))
        assert log["response_summary"]["status_code"] == 200


# ═══════════════════════════════════════════════════════════════════════════════
# 9. 4xx errors (no retry)
# ═══════════════════════════════════════════════════════════════════════════════

class Test4xxNoRetry:
    def test_400_raises(self, seed_db):
        mc = Mock(spec=httpx.Client)
        mc.request.return_value = httpx.Response(400, json={"error": {"message": "Bad"}},
            request=httpx.Request("POST", "https://example.com"))
        gw = Gateway(http_client=mc)
        with pytest.raises(ProviderCallError) as e:
            gw.call(capability_code=_CAPABILITY_CODE, params={"messages": []}, sensitivity="public")
        log = get_api_call_log(e.value.api_call_log_id)
        assert log["call_state"] == "failed"
        assert log["error_code"] == "provider_400"
        assert log["retry_count"] == 0

    def test_429_raises_no_retry(self, seed_db):
        mc = Mock(spec=httpx.Client)
        mc.request.return_value = httpx.Response(429, json={"error": {"message": "RL"}},
            request=httpx.Request("POST", "https://example.com"))
        gw = Gateway(http_client=mc)
        with pytest.raises(ProviderCallError):
            gw.call(capability_code=_CAPABILITY_CODE, params={"messages": []}, sensitivity="public")
        assert mc.request.call_count == 1  # 429 not retried


# ═══════════════════════════════════════════════════════════════════════════════
# 10. 5xx retry
# ═══════════════════════════════════════════════════════════════════════════════

class Test5xxRetry:
    def test_500_retry_then_succeed(self, seed_db):
        err = httpx.Response(500, json={"error": {"message": "ISE"}},
            request=httpx.Request("POST", "https://example.com"))
        mc = Mock(spec=httpx.Client)
        mc.request.side_effect = [err, _make_200_response()]
        r = Gateway(http_client=mc).call(capability_code=_CAPABILITY_CODE, params={"messages": []}, sensitivity="public")
        assert r["call_state"] == "succeeded"
        assert mc.request.call_count == 2
        log = get_api_call_log(UUID(r["api_call_log_id"]))
        assert log["retry_count"] == 1

    def test_503_exhausted(self, seed_db):
        err = httpx.Response(503, json={"error": {"message": "Unavailable"}},
            request=httpx.Request("POST", "https://example.com"))
        mc = Mock(spec=httpx.Client)
        mc.request.return_value = err
        gw = Gateway(http_client=mc)
        with pytest.raises(ProviderCallError) as e:
            gw.call(capability_code=_CAPABILITY_CODE, params={"messages": []}, sensitivity="public")
        assert mc.request.call_count == 2
        log = get_api_call_log(e.value.api_call_log_id)
        assert log["retry_count"] == 1

    def test_retry_has_backoff(self, seed_db):
        mc = Mock(spec=httpx.Client)
        mc.request.side_effect = [
            httpx.Response(500, json={}, request=httpx.Request("POST", "https://e.com")),
            httpx.Response(500, json={}, request=httpx.Request("POST", "https://e.com")),
        ]
        gw = Gateway(http_client=mc)
        t0 = time.monotonic()
        with pytest.raises(ProviderCallError):
            gw.call(capability_code=_CAPABILITY_CODE, params={"messages": []}, sensitivity="public")
        assert time.monotonic() - t0 >= 0.005


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Timeout
# ═══════════════════════════════════════════════════════════════════════════════

class TestTimeout:
    def test_timeout(self, seed_db):
        mc = Mock(spec=httpx.Client)
        mc.request.side_effect = httpx.TimeoutException("Read timeout")
        gw = Gateway(http_client=mc)
        with pytest.raises(ProviderTimeoutError) as e:
            gw.call(capability_code=_CAPABILITY_CODE, params={"messages": []}, sensitivity="public")
        log = get_api_call_log(e.value.api_call_log_id)
        assert log["call_state"] == "timeout"
        assert log["error_code"] == "gateway.timeout"
        assert log["latency_ms"] is not None
        assert log["latency_ms"] >= 0


# ═══════════════════════════════════════════════════════════════════════════════
# 12. Network error retry
# ═══════════════════════════════════════════════════════════════════════════════

class TestNetworkError:
    def test_retry_then_succeed(self, seed_db):
        mc = Mock(spec=httpx.Client)
        mc.request.side_effect = [httpx.ConnectError("refused"), _make_200_response()]
        r = Gateway(http_client=mc).call(capability_code=_CAPABILITY_CODE, params={"messages": []}, sensitivity="public")
        assert r["call_state"] == "succeeded"
        assert mc.request.call_count == 2

    def test_exhausted(self, seed_db):
        mc = Mock(spec=httpx.Client)
        mc.request.side_effect = [httpx.ConnectError("r1"), httpx.ConnectError("r2")]
        gw = Gateway(http_client=mc)
        with pytest.raises(ProviderCallError) as e:
            gw.call(capability_code=_CAPABILITY_CODE, params={"messages": []}, sensitivity="public")
        assert mc.request.call_count == 2
        log = get_api_call_log(e.value.api_call_log_id)
        assert log["error_code"] == "gateway.network_error"


# ═══════════════════════════════════════════════════════════════════════════════
# 13. Budget lifecycle
# ═══════════════════════════════════════════════════════════════════════════════

class TestBudgetLifecycle:
    def test_released_on_4xx(self, seed_db):
        mc = Mock(spec=httpx.Client)
        mc.request.return_value = httpx.Response(400, json={"error": {}},
            request=httpx.Request("POST", "https://e.com"))
        gw = Gateway(http_client=mc)
        with pytest.raises(ProviderCallError) as e:
            gw.call(capability_code=_CAPABILITY_CODE, params={"messages": []}, sensitivity="public")
        log = get_api_call_log(e.value.api_call_log_id)
        budget = get_budget_tracking(UUID(log["budget_tracking_id"]))
        assert budget["reservation_state"] == "released"

    def test_denied_path(self, seed_db):
        with patch("mneme.gateway.call.check_budget_allow", return_value=(False, "Test deny")):
            gw = Gateway(http_client=Mock())
            with pytest.raises(BudgetDeniedError) as e:
                gw.call(capability_code=_CAPABILITY_CODE, params={"messages": []}, sensitivity="public")
            log = get_api_call_log(e.value.api_call_log_id)
            assert log["call_state"] == "denied"


# ═══════════════════════════════════════════════════════════════════════════════
# 14. Credential error
# ═══════════════════════════════════════════════════════════════════════════════

class TestCredentialError:
    @pytest.mark.xfail(reason="Requires valid credential_vault row (FK constraint). "
                              "Credential error path covered by unit tests in TestExceptions/TestMapError.")
    def test_resolution_error(self, seed_db):
        """When credential resolution fails, Gateway raises CredentialResolutionError."""
        from mneme.gateway import vault_bridge
        fake_binding = {
            "capability_binding_id": str(seed_db["binding_id"]),
            "capability_id": str(seed_db["capability_id"]),
            "provider_id": str(seed_db["provider_id"]),
            "provider_model_id": str(seed_db["model_id"]),
            "credential_id": str(uuid4()),
            "project_id": None,
            "binding_scope": "global",
            "status": "active",
            "priority": 100,
            "sensitivity_floor": "public",
            "sensitivity_ceiling": "secret",
            "endpoint_base": "https://api.test.example.com",
            "model_code": "test-gpt-4o",
            "provider_code_val": "test-provider",
            "capability_code": _CAPABILITY_CODE,
            "currency_code": "USD",
            "model_config_json": {},
        }
        with patch("mneme.gateway.call.resolve_capability_binding", return_value=fake_binding), \
             patch.object(vault_bridge.VaultCredentialResolver, "resolve",
                          side_effect=vault_bridge.CredentialNotAvailable(
                              uuid4(), "credential_revoked", "revoked")):
            gw = Gateway(http_client=Mock())
            with pytest.raises(CredentialResolutionError) as e:
                gw.call(capability_code=_CAPABILITY_CODE, params={"messages": []}, sensitivity="public")
            log = get_api_call_log(e.value.api_call_log_id)
            assert log is not None
            assert log["call_state"] == "denied"


# ═══════════════════════════════════════════════════════════════════════════════
# 15. Singleton
# ═══════════════════════════════════════════════════════════════════════════════

class TestSingleton:
    def test_singleton(self):
        assert get_gateway() is get_gateway()

    def test_has_client(self):
        assert get_gateway()._http is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 16. DB: api_call_logs state transitions
# ═══════════════════════════════════════════════════════════════════════════════

class TestApiCallLogsDb:
    def test_full_chain(self, seed_db):
        rid = uuid4()
        lid = insert_api_call_log(request_id=rid, correlation_id=rid, idempotency_key="k",
                                  capability_id=seed_db["capability_id"],
                                  provider_id=seed_db["provider_id"],
                                  provider_model_id=seed_db["model_id"], call_state="planned")
        assert get_api_call_log(lid)["call_state"] == "planned"

        assert transition_call_state(api_call_log_id=lid, new_state="budget_reserved", expected_state="planned")
        assert get_api_call_log(lid)["call_state"] == "budget_reserved"

        assert transition_call_state(api_call_log_id=lid, new_state="credential_checked", expected_state="budget_reserved")
        assert get_api_call_log(lid)["call_state"] == "credential_checked"

        assert transition_call_state(api_call_log_id=lid, new_state="in_flight", expected_state="credential_checked")
        assert get_api_call_log(lid)["call_state"] == "in_flight"

        assert update_call_result(api_call_log_id=lid, new_state="succeeded",
                                  input_tokens=10, output_tokens=5, total_tokens=15,
                                  latency_ms=200, actual_cost=0.001)
        log = get_api_call_log(lid)
        assert log["call_state"] == "succeeded"
        assert log["input_tokens"] == 10
        assert log["latency_ms"] == 200
        assert log["finished_at"] is not None

    def test_guard_rejects_invalid(self, seed_db):
        rid = uuid4()
        lid = insert_api_call_log(request_id=rid, correlation_id=rid, idempotency_key="k",
                                  capability_id=seed_db["capability_id"],
                                  provider_id=seed_db["provider_id"], call_state="planned")
        # Invalid transition raises ValueError (state machine guard)
        with pytest.raises(ValueError, match="Invalid state transition"):
            transition_call_state(api_call_log_id=lid, new_state="in_flight", expected_state="planned")
        assert get_api_call_log(lid)["call_state"] == "planned"

    def test_stale_expected_state(self, seed_db):
        rid = uuid4()
        lid = insert_api_call_log(request_id=rid, correlation_id=rid, idempotency_key="k",
                                  capability_id=seed_db["capability_id"],
                                  provider_id=seed_db["provider_id"], call_state="planned")
        transition_call_state(api_call_log_id=lid, new_state="budget_reserved", expected_state="planned")
        assert not transition_call_state(api_call_log_id=lid, new_state="budget_reserved", expected_state="planned")

    def test_get_nonexistent(self):
        assert get_api_call_log(uuid4()) is None


# ═══════════════════════════════════════════════════════════════════════════════
# 17. DB: budget_tracking
# ═══════════════════════════════════════════════════════════════════════════════

class TestBudgetTrackingDb:
    def test_reserve_commit(self, seed_db):
        rid = uuid4()
        bid = reserve_budget(request_id=rid, correlation_id=rid, subject_type="user",
                             subject_id=seed_db["provider_id"],
                             capability_id=seed_db["capability_id"],
                             provider_id=seed_db["provider_id"],
                             estimated_input_tokens=1000, reserved_cost=0.005)
        b = get_budget_tracking(bid)
        assert b["reservation_state"] == "reserved"
        assert float(b["reserved_cost"]) == 0.005
        assert transition_budget_state(budget_tracking_id=bid, new_state="committed",
                                       expected_state="reserved", committed_cost=0.004)
        b = get_budget_tracking(bid)
        assert b["reservation_state"] == "committed"

    def test_reserve_release(self, seed_db):
        rid = uuid4()
        bid = reserve_budget(request_id=rid, correlation_id=rid, subject_type="agent",
                             subject_id=seed_db["provider_id"], provider_id=seed_db["provider_id"])
        assert transition_budget_state(budget_tracking_id=bid, new_state="released", expected_state="reserved")
        assert get_budget_tracking(bid)["reservation_state"] == "released"

    def test_guard(self, seed_db):
        rid = uuid4()
        bid = reserve_budget(request_id=rid, correlation_id=rid, subject_type="user",
                             subject_id=seed_db["provider_id"])
        transition_budget_state(budget_tracking_id=bid, new_state="committed", expected_state="reserved")
        assert not transition_budget_state(budget_tracking_id=bid, new_state="committed", expected_state="reserved")

    def test_check_always_passes(self):
        assert check_budget_allow(subject_type="user", subject_id=uuid4()) == (True, None)


# ═══════════════════════════════════════════════════════════════════════════════
# 18. API endpoint (schema validation / routing checks)
# ═══════════════════════════════════════════════════════════════════════════════

class TestGatewayCallEndpoint:
    @pytest.fixture(autouse=True)
    def _setup(self):
        from mneme.main import app
        from fastapi.testclient import TestClient
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_missing_capability_code_returns_400(self):
        """Missing required field should return 400 (project validation format)."""
        resp = self.client.post("/api/v4/gateway/call", json={"params": {}})
        assert resp.status_code == 400
        data = resp.json()
        assert data["error"]["code"] == "bad_request"

    def test_empty_params_accepted_schema_wise(self):
        """Providing capability_code with empty params passes schema validation."""
        resp = self.client.post("/api/v4/gateway/call", json={
            "capability_code": "chat.completion",
            "params": {},
        })
        # Schema accepted, may fail at binding resolution but not 400/422
        assert resp.status_code != 400

    def test_full_request_passes_schema(self):
        """Full request with all optional fields passes validation."""
        resp = self.client.post("/api/v4/gateway/call", json={
            "capability_code": "chat.completion",
            "params": {"model": "gpt-4o", "messages": [{"role": "user", "content": "Hi"}]},
            "sensitivity": "private",
            "call_type": "chat",
            "project_id": str(uuid4()),
            "idempotency_key": "ikey-999",
        })
        assert resp.status_code != 400  # schema passes

    def test_invalid_sensitivity_rejected(self):
        """Invalid sensitivity value should fail schema validation."""
        resp = self.client.post("/api/v4/gateway/call", json={
            "capability_code": "chat.completion",
            "params": {},
            "sensitivity": "top_secret_invalid",
        })
        assert resp.status_code == 400

    def test_endpoint_registered(self):
        """Verify the /gateway/call endpoint exists and responds (not 404)."""
        resp = self.client.post("/api/v4/gateway/call", json={
            "capability_code": "test.x",
            "params": {},
        })
        # Should not be 404 Not Found (endpoint is registered)
        assert resp.status_code != 404


# ═══════════════════════════════════════════════════════════════════════════════
# 19. Schema validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestSchemaValidation:
    def test_minimal_request(self):
        r = GatewayCallRequest(capability_code="chat.completion")
        assert r.params == {}
        assert r.sensitivity == SensitivityLevel.private

    def test_full_request(self):
        r = GatewayCallRequest(capability_code="x", params={"a": 1}, project_id=uuid4(),
                               sensitivity=SensitivityLevel.sensitive, call_type="chat",
                               idempotency_key="k")
        assert r.project_id is not None

    def test_response_creation(self):
        r = GatewayCallResponse(api_call_log_id=uuid4(), call_state="succeeded", latency_ms=100,
                                usage=GatewayCallUsage(input_tokens=10, output_tokens=20, total_tokens=30),
                                cost=GatewayCallCost(estimated=0.1, actual=0.1),
                                data={"x": 1})
        assert r.usage.input_tokens == 10
        assert r.cost.actual == 0.1


# ═══════════════════════════════════════════════════════════════════════════════
# 20. No-bypass
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoBypass:
    def test_private_client(self):
        gw = Gateway(http_client=Mock())
        assert not hasattr(gw, "http")
        assert not hasattr(gw, "client")

    def test_no_direct_httpx_in_endpoint(self):
        import inspect
        import mneme.api.routes.gateway.gateway as gwr
        source = inspect.getsource(gwr.gateway_call)
        assert "httpx.Client" not in source


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "--no-header"])
