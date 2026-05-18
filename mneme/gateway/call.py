"""P2-12 Gateway unified call entry — ``Gateway.call()``.

This is the **single entry point** for all external provider API calls in Mneme.
Every call is routed through capability bindings, goes through budget check,
credential resolution, and is fully recorded in ``api_call_logs``.

Architecture::

    1. Resolve capability binding  (capability_code → provider/model/credential)
    2. Budget check + reserve      (usage_limits → budget_tracking)
    3. Resolve Vault credential    (credential_vault → plaintext)
    4. Execute HTTP request        (httpx → provider endpoint)
    5. Record result               (tokens, latency, cost → api_call_logs)
    6. Commit/release budget       (budget_tracking state transition)

The 10-state pipeline in ``api_call_logs.call_state``::

    planned → budget_reserved → credential_checked → in_flight → succeeded
                                                              → failed
                                                              → timeout
    cancelled / denied / dead_letter (terminal)
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any
from uuid import UUID, uuid4

import httpx

from mneme.api.context import RequestContext, get_current_context
from mneme.config import get_settings
from mneme.db.api_call_logs import (
    get_api_call_log,
    increment_retry_count,
    insert_api_call_log,
    link_budget_tracking,
    link_credential_access,
    transition_call_state,
    update_call_result,
)
from mneme.db.budget import (
    check_budget_allow,
    reserve_budget,
    transition_budget_state,
)
from mneme.db.gateway import resolve_capability_binding
from mneme.gateway.vault_bridge import (
    CredentialNotAvailable,
    ResolvedCredential,
    get_vault_credential_resolver,
)

logger = logging.getLogger(__name__)

# ── Exceptions ─────────────────────────────────────────────────────────────────


class GatewayError(Exception):
    """Base exception for Gateway call failures."""

    def __init__(
        self,
        api_call_log_id: UUID | None,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        call_state: str | None = None,
    ) -> None:
        super().__init__(message)
        self.api_call_log_id = api_call_log_id
        self.code = code
        self.message = message
        self.details = details or {}
        self.call_state = call_state


class BindingNotFoundError(GatewayError):
    """No suitable capability binding was found."""


class BudgetDeniedError(GatewayError):
    """Budget/limit check failed — call was denied."""


class CredentialResolutionError(GatewayError):
    """Vault credential could not be resolved."""


class ProviderCallError(GatewayError):
    """The external provider returned an error."""


class ProviderTimeoutError(GatewayError):
    """The external provider call timed out."""


# ── Fingerprint helper ─────────────────────────────────────────────────────────


def _make_fingerprint(capability_code: str, params: dict[str, Any]) -> str:
    """Generate a stable provider_request_fingerprint from input params.

    This is stored in both budget_tracking and api_call_logs for idempotency
    and trace correlation.
    """
    payload = json.dumps(
        {"capability_code": capability_code, "params": params},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:64]


def _compute_call_cost(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    input_price_per_1k: float | None,
    output_price_per_1k: float | None,
) -> float:
    """Estimate cost from token usage and pricing (per-1k-token model)."""
    cost = 0.0
    if input_tokens and input_price_per_1k:
        cost += (input_tokens / 1000.0) * input_price_per_1k
    if output_tokens and output_price_per_1k:
        cost += (output_tokens / 1000.0) * output_price_per_1k
    return round(cost, 6)


# ── Gateway class ──────────────────────────────────────────────────────────────


class Gateway:
    """Stateless Gateway service providing the ``.call()`` entry point.

    Usage::

        from mneme.gateway import Gateway

        gw = Gateway()
        try:
            result = gw.call(
                capability_code="chat.completion",
                params={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
                project_id=uuid4(),
                sensitivity="private",
            )
            print(result["data"])
        except GatewayError as exc:
            logger.error("Gateway call failed: %s", exc)
    """

    def __init__(self, *, http_client: httpx.Client | None = None) -> None:
        self._settings = get_settings()
        self._resolver = get_vault_credential_resolver()
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(
                connect=10.0,
                read=self._settings.gateway_call_timeout_seconds,
                write=30.0,
                pool=10.0,
            ),
        )

    # ── Public API ─────────────────────────────────────────────────────────────

    def call(
        self,
        capability_code: str,
        params: dict[str, Any],
        *,
        project_id: UUID | None = None,
        sensitivity: str = "private",
        actor_type: str = "system",
        actor_id: UUID | None = None,
        auth_context_type: str | None = None,
        auth_context_id: UUID | None = None,
        request_id: UUID | None = None,
        correlation_id: UUID | None = None,
        idempotency_key: str | None = None,
        call_type: str = "chat",
    ) -> dict[str, Any]:
        """Execute a provider API call through the Gateway.

        This is the unified entry point. All external API calls must go through
        this method (no bypass).

        Parameters
        ----------
        capability_code : str
            The capability to invoke (e.g. ``"chat.completion"``).
        params : dict
            Parameters to forward to the provider. Typically includes model,
            messages, temperature, etc.
        project_id : UUID | None
            The project context for binding resolution.
        sensitivity : str
            The data sensitivity level (``public``/``normal``/``private``/
            ``sensitive``/``secret``).
        actor_type : str
            Who is calling (``system``, ``user``, ``agent``, ``service``).
        actor_id : UUID | None
            The actor's primary key.
        auth_context_type : str | None
            ``user_session``, ``agent_token``, etc.
        auth_context_id : UUID | None
            The auth context primary key.
        request_id : UUID | None
            Trace request_id. Auto-generated if None.
        correlation_id : UUID | None
            Trace correlation_id. Defaults to request_id.
        idempotency_key : str | None
            Unique idempotency key. Auto-generated if None.
        call_type : str
            Category of the call (``chat``, ``embedding``, ``ocr``, etc.).

        Returns
        -------
        dict
            {
                "api_call_log_id": str,
                "data": <provider response>,
                "usage": {"input_tokens": int, "output_tokens": int, ...},
                "latency_ms": int,
                "cost": {"estimated": float, "actual": float},
                "call_state": str,
            }

        Raises
        ------
        BindingNotFoundError : no suitable capability binding.
        BudgetDeniedError : budget/limits exceeded.
        CredentialResolutionError : credential not available.
        ProviderCallError : provider returned an error.
        ProviderTimeoutError : provider call timed out.
        """
        req_id = request_id or uuid4()
        corr_id = correlation_id or req_id
        ikey = idempotency_key or f"gateway-call-{uuid4()}"
        fingerprint = _make_fingerprint(capability_code, params)

        # 1. Resolve capability binding
        binding = resolve_capability_binding(
            capability_code=capability_code,
            project_id=project_id,
            sensitivity=sensitivity,
        )

        if binding is None:
            raise BindingNotFoundError(
                api_call_log_id=None,
                code="gateway.binding_not_found",
                message=(
                    f"No active binding for capability '{capability_code}'"
                    + (f" in project '{project_id}'" if project_id else "")
                    + f" at sensitivity '{sensitivity}'"
                ),
                call_state="denied",
            )

        capability_id = UUID(binding["capability_id"])
        binding_id = UUID(binding["capability_binding_id"])
        provider_id = UUID(binding["provider_id"])
        model_id = (
            UUID(binding["provider_model_id"])
            if binding.get("provider_model_id")
            else None
        )
        credential_id = (
            UUID(binding["credential_id"])
            if binding.get("credential_id")
            else None
        )

        # 2. Create api_call_logs row (planned)
        log_id = insert_api_call_log(
            request_id=req_id,
            correlation_id=corr_id,
            idempotency_key=ikey,
            project_id=project_id,
            actor_type=actor_type,
            actor_id=actor_id,
            auth_context_type=auth_context_type,
            auth_context_id=auth_context_id,
            capability_id=capability_id,
            capability_binding_id=binding_id,
            provider_id=provider_id,
            provider_model_id=model_id,
            credential_id=credential_id,
            call_type=call_type,
            call_state="planned",
            provider_request_fingerprint=fingerprint,
            request_summary={
                "capability_code": capability_code,
                "sensitivity": sensitivity,
                "params_keys": list(params.keys()),
            },
            currency_code=binding.get("currency_code", "USD"),
        )

        try:
            return self._execute(
                log_id=log_id,
                binding=binding,
                params=params,
                fingerprint=fingerprint,
                provider_id=provider_id,
                credential_id=credential_id,
                capability_id=capability_id,
                call_type=call_type,
                project_id=project_id,
                actor_type=actor_type,
                actor_id=actor_id,
                req_id=req_id,
                corr_id=corr_id,
            )
        except GatewayError:
            raise
        except Exception as exc:
            # Catch-all: mark as failed
            try:
                update_call_result(
                    api_call_log_id=log_id,
                    new_state="failed",
                    error_code="internal_error",
                    error_message=str(exc)[:1024],
                    latency_ms=0,
                )
            except Exception:
                logger.exception("Failed to update api_call_log %s after error", log_id)
            raise ProviderCallError(
                api_call_log_id=log_id,
                code="gateway.internal_error",
                message=str(exc),
                call_state="failed",
            ) from exc

    # ── Internal execution ─────────────────────────────────────────────────────

    def _execute(
        self,
        *,
        log_id: UUID,
        binding: dict[str, Any],
        params: dict[str, Any],
        fingerprint: str,
        provider_id: UUID,
        credential_id: UUID | None,
        capability_id: UUID,
        call_type: str,
        project_id: UUID | None,
        actor_type: str,
        actor_id: UUID | None,
        req_id: UUID,
        corr_id: UUID,
    ) -> dict[str, Any]:
        """Internal state-machine driven call execution."""

        # ── Step A: Budget check + reserve ─────────────────────────────────

        # Estimate cost from pricing in binding
        input_price = binding.get("input_price_per_1k")
        output_price = binding.get("output_price_per_1k")
        estimated_input = params.get("max_tokens") or binding.get("max_output_tokens") or 0
        estimated_cost = _compute_call_cost(
            input_tokens=estimated_input,
            output_tokens=0,
            input_price_per_1k=input_price,
            output_price_per_1k=output_price,
        )

        allowed, deny_reason = check_budget_allow(
            subject_type=actor_type,
            subject_id=actor_id or provider_id,
            capability_id=capability_id,
            provider_id=provider_id,
            project_id=project_id,
            estimated_cost=estimated_cost,
        )

        if not allowed:
            transition_call_state(
                api_call_log_id=log_id,
                new_state="denied",
                expected_state="planned",
            )
            raise BudgetDeniedError(
                api_call_log_id=log_id,
                code="gateway.budget_denied",
                message=deny_reason or "Budget limit exceeded",
                call_state="denied",
                details={"deny_reason": deny_reason},
            )

        budget_id = reserve_budget(
            request_id=req_id,
            correlation_id=corr_id,
            subject_type=actor_type,
            subject_id=actor_id or provider_id,
            capability_id=capability_id,
            provider_id=provider_id,
            project_id=project_id,
            currency_code=binding.get("currency_code", "USD"),
            estimated_input_tokens=estimated_input,
            reserved_cost=estimated_cost,
            provider_request_fingerprint=fingerprint,
        )
        link_budget_tracking(api_call_log_id=log_id, budget_tracking_id=budget_id)
        transition_call_state(
            api_call_log_id=log_id,
            new_state="budget_reserved",
            expected_state="planned",
        )

        # ── Step B: Resolve Vault credential ──────────────────────────────

        plaintext_credential: bytes | None = None
        vault_access_log_id: UUID | None = None
        if credential_id is not None:
            try:
                resolved = self._resolver.resolve(
                    credential_id=credential_id,
                    capability_id=capability_id,
                    provider_id=provider_id,
                    request_id=req_id,
                    correlation_id=corr_id,
                    actor_type=actor_type,
                    actor_id=actor_id,
                )
                plaintext_credential = resolved.plaintext
                vault_access_log_id = resolved.vault_access_log_id
            except CredentialNotAvailable as exc:
                transition_call_state(
                    api_call_log_id=log_id,
                    new_state="denied",
                    expected_state="budget_reserved",
                )
                transition_budget_state(
                    budget_tracking_id=budget_id,
                    new_state="denied",
                    expected_state="reserved",
                    denied_reason=exc.reason_code,
                )
                raise CredentialResolutionError(
                    api_call_log_id=log_id,
                    code="gateway.credential_unavailable",
                    message=str(exc),
                    call_state="denied",
                    details={"reason_code": exc.reason_code},
                ) from exc

        # Link the vault access log to the api_call_log
        if vault_access_log_id is not None and credential_id is not None:
            try:
                link_credential_access(
                    api_call_log_id=log_id,
                    credential_id=credential_id,
                    vault_access_log_id=vault_access_log_id,
                )
            except Exception:
                logger.exception("Failed to link vault access log %s", vault_access_log_id)

        transition_call_state(
            api_call_log_id=log_id,
            new_state="credential_checked",
            expected_state="budget_reserved",
        )

        # ── Step C: Build HTTP request ────────────────────────────────────

        endpoint_base = binding.get("endpoint_base", "")
        model_code = binding.get("model_code") or binding.get("external_model_id", "")
        provider_code = binding.get("provider_code_val", "")

        url, headers, body = self._build_request(
            binding=binding,
            params=params,
            plaintext_credential=plaintext_credential,
            provider_code=provider_code,
            endpoint_base=endpoint_base,
            model_code=model_code,
            capability_code=binding.get("capability_code", ""),
        )

        # ── Step D: Execute with retry ────────────────────────────────────

        max_retries = 1  # P2-12: one automatic retry
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            if attempt > 0:
                increment_retry_count(log_id)
                logger.info(
                    "Gateway retry %d/%d for %s", attempt, max_retries, log_id,
                )

            transition_call_state(
                api_call_log_id=log_id,
                new_state="in_flight",
                expected_state="credential_checked" if attempt == 0 else "failed",
            )

            start_time = time.monotonic()
            try:
                http_resp = self._http.request(
                    method="POST",
                    url=url,
                    headers=headers,
                    json=body,
                )
                latency_ms = int((time.monotonic() - start_time) * 1000)

                # Parse tokens from response
                resp_data = http_resp.json() if http_resp.content else {}
                usage = resp_data.get("usage", {}) if isinstance(resp_data, dict) else {}
                input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens")
                output_tokens = usage.get("output_tokens") or usage.get("completion_tokens")
                total_tokens = usage.get("total_tokens") or (
                    (input_tokens or 0) + (output_tokens or 0)
                )

                actual_cost = _compute_call_cost(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    input_price_per_1k=input_price,
                    output_price_per_1k=output_price,
                )

                if http_resp.is_error:
                    # Provider error (4xx/5xx)
                    error_code = f"provider_{http_resp.status_code}"
                    error_msg = str(resp_data)[:1024]

                    update_call_result(
                        api_call_log_id=log_id,
                        new_state="failed",
                        error_code=error_code,
                        error_message=error_msg,
                        latency_ms=latency_ms,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        total_tokens=total_tokens,
                        estimated_cost=estimated_cost,
                        actual_cost=actual_cost,
                        response_summary={
                            "status_code": http_resp.status_code,
                            "error": error_msg[:256],
                        },
                    )

                    # Decide whether to retry
                    if attempt < max_retries and http_resp.status_code >= 500:
                        last_error = ProviderCallError(
                            api_call_log_id=log_id,
                            code=f"gateway.{error_code}",
                            message=f"Provider returned {http_resp.status_code}",
                            call_state="failed",
                            details={"status_code": http_resp.status_code},
                        )
                        time.sleep(1.0 * (attempt + 1))  # simple backoff
                        continue

                    transition_budget_state(
                        budget_tracking_id=budget_id,
                        new_state="released",
                        expected_state="reserved",
                        actual_input_tokens=input_tokens,
                        actual_output_tokens=output_tokens,
                        released_cost=actual_cost,
                    )
                    raise ProviderCallError(
                        api_call_log_id=log_id,
                        code=f"gateway.{error_code}",
                        message=f"Provider returned {http_resp.status_code}: {error_msg[:200]}",
                        call_state="failed",
                        details={"status_code": http_resp.status_code},
                    )

                # ── Success ────────────────────────────────────────────

                update_call_result(
                    api_call_log_id=log_id,
                    new_state="succeeded",
                    external_request_id=resp_data.get("id", str(uuid4())),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    estimated_cost=estimated_cost,
                    actual_cost=actual_cost,
                    latency_ms=latency_ms,
                    response_summary={
                        "status_code": http_resp.status_code,
                        "model": resp_data.get("model", model_code),
                        "usage": usage,
                    },
                )

                transition_budget_state(
                    budget_tracking_id=budget_id,
                    new_state="committed",
                    expected_state="reserved",
                    actual_input_tokens=input_tokens,
                    actual_output_tokens=output_tokens,
                    committed_cost=actual_cost,
                )

                logger.info(
                    "Gateway call succeeded: %s latency=%dms tokens=%s cost=%s",
                    log_id, latency_ms, total_tokens, actual_cost,
                )

                return {
                    "api_call_log_id": str(log_id),
                    "data": resp_data,
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": total_tokens,
                    },
                    "latency_ms": latency_ms,
                    "cost": {
                        "estimated": estimated_cost,
                        "actual": actual_cost,
                    },
                    "call_state": "succeeded",
                }

            except httpx.TimeoutException as exc:
                latency_ms = int((time.monotonic() - start_time) * 1000)
                update_call_result(
                    api_call_log_id=log_id,
                    new_state="timeout",
                    error_code="gateway.timeout",
                    error_message=f"Provider call timed out after {latency_ms}ms",
                    latency_ms=latency_ms,
                )
                transition_budget_state(
                    budget_tracking_id=budget_id,
                    new_state="released",
                    expected_state="reserved",
                )
                raise ProviderTimeoutError(
                    api_call_log_id=log_id,
                    code="gateway.timeout",
                    message=f"Provider call timed out after {latency_ms}ms",
                    call_state="timeout",
                ) from exc

            except httpx.NetworkError as exc:
                latency_ms = int((time.monotonic() - start_time) * 1000)
                last_error = exc
                if attempt < max_retries:
                    update_call_result(
                        api_call_log_id=log_id,
                        new_state="failed",
                        error_code="gateway.network_error",
                        error_message=str(exc)[:1024],
                        latency_ms=latency_ms,
                    )
                    time.sleep(1.0 * (attempt + 1))
                    continue

                update_call_result(
                    api_call_log_id=log_id,
                    new_state="failed",
                    error_code="gateway.network_error",
                    error_message=str(exc)[:1024],
                    latency_ms=latency_ms,
                )
                transition_budget_state(
                    budget_tracking_id=budget_id,
                    new_state="released",
                    expected_state="reserved",
                )
                raise ProviderCallError(
                    api_call_log_id=log_id,
                    code="gateway.network_error",
                    message=str(exc),
                    call_state="failed",
                ) from exc

        # Exhausted retries — should not reach here normally
        raise ProviderCallError(
            api_call_log_id=log_id,
            code="gateway.retries_exhausted",
            message="All retries exhausted",
            call_state="dead_letter",
        )

    # ── HTTP request builder ───────────────────────────────────────────────────

    def _build_request(
        self,
        *,
        binding: dict[str, Any],
        params: dict[str, Any],
        plaintext_credential: bytes | None,
        provider_code: str,
        endpoint_base: str,
        model_code: str,
        capability_code: str,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """Build the HTTP request (URL, headers, body) for the provider call.

        Subclasses or future phases can override this to support different
        provider API formats (OpenAI-compatible, Anthropic, etc.).
        """
        headers: dict[str, str] = {"Content-Type": "application/json"}

        # Add credential as Bearer token if available
        if plaintext_credential is not None:
            credential_str = plaintext_credential.decode("utf-8").strip()
            # Detect if credential is already "Bearer xxx" or just raw key
            if credential_str.lower().startswith("bearer "):
                headers["Authorization"] = credential_str
            elif credential_str.startswith("sk-") or credential_str.startswith("api-"):
                headers["Authorization"] = f"Bearer {credential_str}"
            elif len(credential_str) > 20:
                headers["Authorization"] = f"Bearer {credential_str}"
            else:
                # Short credential — might be a different auth scheme
                headers["X-API-Key"] = credential_str

        # Build URL path based on capability
        url = self._resolve_url(
            endpoint_base=endpoint_base,
            model_code=model_code,
            capability_code=capability_code,
        )

        # Build body — pass through params, inject model if not present
        body = dict(params)
        if "model" not in body and model_code:
            body["model"] = model_code

        # Provider-specific headers from binding config
        model_config = binding.get("model_config_json") or {}
        if isinstance(model_config, dict) and model_config.get("api_version"):
            headers["OpenAI-Beta"] = f"assistants={model_config['api_version']}"

        return url, headers, body

    @staticmethod
    def _resolve_url(
        endpoint_base: str,
        model_code: str,
        capability_code: str,
    ) -> str:
        """Construct the provider endpoint URL from capability code.

        Maps Mneme capability codes to provider API paths (OpenAI-compatible).
        """
        base = endpoint_base.rstrip("/")

        # OpenAI-compatible path mapping
        capability_paths: dict[str, str] = {
            "chat.completion": "/v1/chat/completions",
            "chat.completion.streaming": "/v1/chat/completions",
            "embedding.create": "/v1/embeddings",
            "image.generate": "/v1/images/generations",
            "vision.analyze": "/v1/chat/completions",
            "audio.transcribe": "/v1/audio/transcriptions",
            "rerank.execute": "/v1/rerank",
            "ocr.extract": "/v1/chat/completions",
            "search.execute": "/v1/search",
        }

        path = capability_paths.get(capability_code, "/v1/chat/completions")
        return f"{base}{path}"


# ── Module-level singleton ─────────────────────────────────────────────────────

_gateway: Gateway | None = None


def get_gateway() -> Gateway:
    """Return the module-level :class:`Gateway` singleton."""
    global _gateway
    if _gateway is None:
        _gateway = Gateway()
    return _gateway
