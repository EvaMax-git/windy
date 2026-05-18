"""P2-11 网关 API — Provider/模型/能力/绑定 注册管理。

接口列表
--------
* ``POST   /api/v4/gateway/providers``                     – 注册 Provider
* ``GET    /api/v4/gateway/providers``                     – 分页列表
* ``GET    /api/v4/gateway/providers/{id}``                – Provider 详情
* ``PUT    /api/v4/gateway/providers/{id}``                – 更新 Provider
* ``POST   /api/v4/gateway/providers/{id}/models``         – 注册模型
* ``GET    /api/v4/gateway/providers/{id}/models``         – 模型列表
* ``GET    /api/v4/gateway/providers/{id}/models/{mid}``   – 模型详情
* ``PUT    /api/v4/gateway/providers/{id}/models/{mid}``   – 更新模型
* ``POST   /api/v4/gateway/capabilities``                  – 创建能力
* ``GET    /api/v4/gateway/capabilities``                  – 能力列表
* ``GET    /api/v4/gateway/capabilities/{id}``             – 能力详情
* ``PUT    /api/v4/gateway/capabilities/{id}``             – 更新能力
* ``POST   /api/v4/gateway/bindings``                      – 创建绑定
* ``GET    /api/v4/gateway/bindings``                       – 绑定列表
* ``GET    /api/v4/gateway/bindings/{id}``                  – 绑定详情
* ``PUT    /api/v4/gateway/bindings/{id}``                  – 更新绑定
* ``POST   /api/v4/gateway/seed/capabilities``              – 初始化预定义能力
"""

from __future__ import annotations

import logging
import math
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError

from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.base import SessionLocal
from mneme.db.gateway import (
    create_capability,
    create_capability_binding,
    create_provider,
    create_provider_model,
    get_capabilities,
    get_capability_binding_by_id,
    get_capability_bindings,
    get_capability_by_code,
    get_capability_by_id,
    get_provider_by_code,
    get_provider_by_id,
    get_provider_model_by_id,
    get_provider_models,
    get_providers,
    resolve_capability_binding,
    seed_capabilities,
    update_capability,
    update_capability_binding,
    update_provider,
    update_provider_model,
)
from mneme.db.audit import add_audit_event
from mneme.db.budget import (
    create_usage_limit,
    delete_usage_limit,
    get_limit_usage,
    get_usage_limit_by_id,
    get_usage_limits,
    update_usage_limit,
)
from mneme.gateway.call import (
    BindingNotFoundError,
    BudgetDeniedError,
    CredentialResolutionError,
    GatewayError,
    ProviderTimeoutError,
    get_gateway,
)
from mneme.schemas import (
    PageInfo,
    PaginationParams,
    ResponseEnvelope,
)
from mneme.schemas.gateway import (
    BindingFilterParams,
    CapabilityBindingCreate,
    CapabilityBindingListResponse,
    CapabilityBindingRead,
    CapabilityBindingUpdate,
    CapabilityCreate,
    CapabilityFilterParams,
    CapabilityListResponse,
    CapabilityRead,
    CapabilityUpdate,
    GatewayCallCost,
    GatewayCallRequest,
    GatewayCallResponse,
    GatewayCallUsage,
    LimitUsageRead,
    ModelFilterParams,
    ProviderCreate,
    ProviderFilterParams,
    ProviderListResponse,
    ProviderModelCreate,
    ProviderModelListResponse,
    ProviderModelRead,
    ProviderModelUpdate,
    ProviderRead,
    ProviderUpdate,
    SEED_CAPABILITIES,
    UsageLimitCreate,
    UsageLimitFilterParams,
    UsageLimitListResponse,
    UsageLimitRead,
    UsageLimitUpdate,
)
from mneme.security.audit import audit_event_for_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/gateway", tags=["gateway"])


# ═══════════════════════════════════════════════════════════════════════════════
# ── Providers ──────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/providers", response_model=ResponseEnvelope[ProviderRead], status_code=201)
def register_provider(
    body: ProviderCreate,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """注册一个新的 Provider（如 OpenAI、Anthropic）。

    ``provider_code`` 必须在所有 Provider 中唯一。
    """
    # Check for duplicate code
    existing = get_provider_by_code(body.provider_code)
    if existing is not None:
        raise ApiError(
            409,
            "idempotency_conflict",
            f"Provider 代码 '{body.provider_code}' 已存在",
        )

    try:
        row = create_provider(
            provider_code=body.provider_code,
            name=body.name,
            provider_type=body.provider_type.value,
            status=body.status.value,
            endpoint_base=body.endpoint_base,
            config_json=body.config_json,
        )
    except IntegrityError:
        raise ApiError(
            409,
            "idempotency_conflict",
            f"Provider 代码 '{body.provider_code}' 已存在",
        )

    provider_id = UUID(row["provider_id"])

    # Audit
    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="gateway.provider.created",
                result="success",
                object_type="provider",
                object_id=provider_id,
                diff_summary={
                    "provider_code": body.provider_code,
                    "name": body.name,
                    "provider_type": body.provider_type.value,
                },
            ),
        )
        db.commit()

    item = ProviderRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/providers", response_model=ResponseEnvelope[ProviderListResponse])
def list_providers(
    pagination: PaginationParams = Depends(),
    filters: ProviderFilterParams = Depends(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """列出所有已注册的 Provider，支持可选过滤。

    Query parameters:
    * ``page`` / ``page_size`` — pagination (default 1/50, max 200).
    * ``provider_type`` — filter by type (llm, embedding, ocr, ...).
    * ``status`` — filter by status (active, disabled, degraded).
    * ``search`` — search in provider_code or name (ILIKE).
    """
    rows, total = get_providers(
        page=pagination.page,
        page_size=pagination.page_size,
        provider_type=filters.provider_type.value if filters.provider_type else None,
        status=filters.status.value if filters.status else None,
        search=filters.search,
    )

    items = [ProviderRead(**row) for row in rows]
    total_pages = max(1, math.ceil(total / max(pagination.page_size, 1)))

    page_info = PageInfo(
        page=pagination.page,
        page_size=pagination.page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=pagination.page < total_pages,
        has_previous=pagination.page > 1,
    )

    data = ProviderListResponse(items=items, page_info=page_info)
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/providers/{provider_id}", response_model=ResponseEnvelope[ProviderRead])
def get_provider(
    provider_id: UUID,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """通过主键获取单个 Provider。"""
    row = get_provider_by_id(provider_id)
    if row is None:
        raise ApiError(404, "bad_request", f"Provider 'provider_id' 未找到")

    item = ProviderRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.put("/providers/{provider_id}", response_model=ResponseEnvelope[ProviderRead])
def update_provider_endpoint(
    provider_id: UUID,
    body: ProviderUpdate,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """更新已有的 Provider。仅更新提供的字段。"""
    row = get_provider_by_id(provider_id)
    if row is None:
        raise ApiError(404, "bad_request", f"Provider 'provider_id' 未找到")

    updated = update_provider(
        provider_id=provider_id,
        name=body.name,
        provider_type=body.provider_type.value if body.provider_type else None,
        status=body.status.value if body.status else None,
        endpoint_base=body.endpoint_base,
        config_json=body.config_json,
    )

    if not updated:
        raise ApiError(409, "bad_request", f"Provider '{provider_id}' 更新失败")

    # Audit
    diff = {}
    if body.name is not None:
        diff["name"] = body.name
    if body.provider_type is not None:
        diff["provider_type"] = body.provider_type.value
    if body.status is not None:
        diff["status"] = body.status.value
    if body.endpoint_base is not None:
        diff["endpoint_base"] = body.endpoint_base
    if body.config_json is not None:
        diff["config_json_updated"] = True

    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="gateway.provider.updated",
                result="success",
                object_type="provider",
                object_id=provider_id,
                diff_summary=diff,
            ),
        )
        db.commit()

    row = get_provider_by_id(provider_id)
    item = ProviderRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ── Provider Models ────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.post(
    "/providers/{provider_id}/models",
    response_model=ResponseEnvelope[ProviderModelRead],
    status_code=201,
)
def register_model(
    provider_id: UUID,
    body: ProviderModelCreate,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """在指定 Provider 下注册模型。

    The ``model_code`` and ``external_model_id`` must both be unique within
    the provider (enforced by DB UNIQUE constraints).
    """
    # Verify provider exists
    provider = get_provider_by_id(provider_id)
    if provider is None:
        raise ApiError(404, "bad_request", f"Provider 'provider_id' 未找到")

    # Check provider status
    if provider.get("status") == "disabled":
        raise ApiError(
            403,
            "permission_denied",
            f"Provider '{provider_id}' 已禁用 — 无法注册模型",
        )

    try:
        row = create_provider_model(
            provider_id=provider_id,
            model_code=body.model_code,
            external_model_id=body.external_model_id,
            model_type=body.model_type.value,
            status=body.status.value,
            display_name=body.display_name,
            version_label=body.version_label,
            context_window_tokens=body.context_window_tokens,
            max_input_tokens=body.max_input_tokens,
            max_output_tokens=body.max_output_tokens,
            input_price_per_1k=body.input_price_per_1k,
            output_price_per_1k=body.output_price_per_1k,
            currency_code=body.currency_code,
            supports_streaming=body.supports_streaming,
            supports_json_mode=body.supports_json_mode,
            supports_tools=body.supports_tools,
            supports_vision=body.supports_vision,
            sensitivity_ceiling=body.sensitivity_ceiling.value,
            config_json=body.config_json,
            metadata_json=body.metadata_json,
            deprecated_at=body.deprecated_at.isoformat() if body.deprecated_at else None,
        )
    except IntegrityError:
        raise ApiError(
            409,
            "idempotency_conflict",
            f"model with code '{body.model_code}' or external_id "
            f"'{body.external_model_id}' already exists for provider '{provider_id}'",
        )

    model_id = UUID(row["provider_model_id"])

    # Audit
    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="gateway.model.created",
                result="success",
                object_type="provider_model",
                object_id=model_id,
                diff_summary={
                    "provider_id": str(provider_id),
                    "model_code": body.model_code,
                    "model_type": body.model_type.value,
                },
            ),
        )
        db.commit()

    item = ProviderModelRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get(
    "/providers/{provider_id}/models",
    response_model=ResponseEnvelope[ProviderModelListResponse],
)
def list_models(
    provider_id: UUID,
    pagination: PaginationParams = Depends(),
    filters: ModelFilterParams = Depends(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """列出指定 Provider 下所有已注册的模型。

    Query parameters:
    * ``page`` / ``page_size`` — pagination.
    * ``model_type`` — filter by type (chat, embedding, ...).
    * ``status`` — filter by status (active, disabled, degraded, deprecated).
    * ``search`` — search in model_code, external_model_id, or display_name.
    """
    provider = get_provider_by_id(provider_id)
    if provider is None:
        raise ApiError(404, "bad_request", f"Provider 'provider_id' 未找到")

    rows, total = get_provider_models(
        provider_id=provider_id,
        page=pagination.page,
        page_size=pagination.page_size,
        model_type=filters.model_type.value if filters.model_type else None,
        status=filters.status.value if filters.status else None,
        search=filters.search,
    )

    items = [ProviderModelRead(**row) for row in rows]
    total_pages = max(1, math.ceil(total / max(pagination.page_size, 1)))

    page_info = PageInfo(
        page=pagination.page,
        page_size=pagination.page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=pagination.page < total_pages,
        has_previous=pagination.page > 1,
    )

    data = ProviderModelListResponse(items=items, page_info=page_info)
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get(
    "/providers/{provider_id}/models/{model_id}",
    response_model=ResponseEnvelope[ProviderModelRead],
)
def get_model(
    provider_id: UUID,
    model_id: UUID,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """通过主键获取单个模型。"""
    row = get_provider_model_by_id(model_id)
    if row is None:
        raise ApiError(404, "bad_request", f"Provider模型 'model_id' 未找到")

    if str(row.get("provider_id", "")) != str(provider_id):
        raise ApiError(404, "bad_request", f"模型 '{model_id}' 不属于 Provider '{provider_id}'")

    item = ProviderModelRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.put(
    "/providers/{provider_id}/models/{model_id}",
    response_model=ResponseEnvelope[ProviderModelRead],
)
def update_model_endpoint(
    provider_id: UUID,
    model_id: UUID,
    body: ProviderModelUpdate,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """更新已有的模型。仅更新提供的字段。"""
    row = get_provider_model_by_id(model_id)
    if row is None:
        raise ApiError(404, "bad_request", f"Provider模型 'model_id' 未找到")

    if str(row.get("provider_id", "")) != str(provider_id):
        raise ApiError(404, "bad_request", f"模型 '{model_id}' 不属于 Provider '{provider_id}'")

    updated = update_provider_model(
        provider_model_id=model_id,
        status=body.status.value if body.status else None,
        display_name=body.display_name,
        version_label=body.version_label,
        context_window_tokens=body.context_window_tokens,
        max_input_tokens=body.max_input_tokens,
        max_output_tokens=body.max_output_tokens,
        input_price_per_1k=body.input_price_per_1k,
        output_price_per_1k=body.output_price_per_1k,
        supports_streaming=body.supports_streaming,
        supports_json_mode=body.supports_json_mode,
        supports_tools=body.supports_tools,
        supports_vision=body.supports_vision,
        sensitivity_ceiling=body.sensitivity_ceiling.value if body.sensitivity_ceiling else None,
        config_json=body.config_json,
        metadata_json=body.metadata_json,
        deprecated_at=body.deprecated_at.isoformat() if body.deprecated_at else None,
    )

    if not updated:
        raise ApiError(409, "bad_request", f"Provider 模型 '{model_id}' 更新失败")

    # Audit
    diff = {}
    if body.status is not None:
        diff["status"] = body.status.value
    if body.display_name is not None:
        diff["display_name"] = body.display_name
    if body.deprecated_at is not None:
        diff["deprecated"] = True

    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="gateway.model.updated",
                result="success",
                object_type="provider_model",
                object_id=model_id,
                diff_summary=diff,
            ),
        )
        db.commit()

    row = get_provider_model_by_id(model_id)
    item = ProviderModelRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ── Capabilities ───────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/capabilities", response_model=ResponseEnvelope[CapabilityRead], status_code=201)
def register_capability(
    body: CapabilityCreate,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """创建新的能力定义。

    The ``capability_code`` must be unique across all capabilities.
    """
    existing = get_capability_by_code(body.capability_code)
    if existing is not None:
        raise ApiError(
            409,
            "idempotency_conflict",
            f"能力代码 '{body.capability_code}' 已存在",
        )

    try:
        row = create_capability(
            capability_code=body.capability_code,
            name=body.name,
            category=body.category.value,
            risk_level=body.risk_level.value,
            default_budget_mode=body.default_budget_mode.value,
        )
    except IntegrityError:
        raise ApiError(
            409,
            "idempotency_conflict",
            f"能力代码 '{body.capability_code}' 已存在",
        )

    capability_id = UUID(row["capability_id"])

    # Audit
    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="gateway.capability.created",
                result="success",
                object_type="capability",
                object_id=capability_id,
                diff_summary={
                    "capability_code": body.capability_code,
                    "name": body.name,
                    "category": body.category.value,
                },
            ),
        )
        db.commit()

    item = CapabilityRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/capabilities", response_model=ResponseEnvelope[CapabilityListResponse])
def list_capabilities(
    pagination: PaginationParams = Depends(),
    filters: CapabilityFilterParams = Depends(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """列出所有能力定义，支持可选过滤。

    Query parameters:
    * ``page`` / ``page_size`` — pagination.
    * ``category`` — filter by category (chat, embedding, ...).
    * ``risk_level`` — filter by risk level (low, normal, high, critical).
    * ``search`` — search in capability_code or name.
    """
    rows, total = get_capabilities(
        page=pagination.page,
        page_size=pagination.page_size,
        category=filters.category.value if filters.category else None,
        risk_level=filters.risk_level.value if filters.risk_level else None,
        search=filters.search,
    )

    items = [CapabilityRead(**row) for row in rows]
    total_pages = max(1, math.ceil(total / max(pagination.page_size, 1)))

    page_info = PageInfo(
        page=pagination.page,
        page_size=pagination.page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=pagination.page < total_pages,
        has_previous=pagination.page > 1,
    )

    data = CapabilityListResponse(items=items, page_info=page_info)
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get(
    "/capabilities/{capability_id}",
    response_model=ResponseEnvelope[CapabilityRead],
)
def get_capability(
    capability_id: UUID,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """通过主键获取单个能力。"""
    row = get_capability_by_id(capability_id)
    if row is None:
        raise ApiError(404, "bad_request", f"能力 'capability_id' 未找到")

    item = CapabilityRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.put(
    "/capabilities/{capability_id}",
    response_model=ResponseEnvelope[CapabilityRead],
)
def update_capability_endpoint(
    capability_id: UUID,
    body: CapabilityUpdate,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """更新已有的能力。仅更新提供的字段。"""
    row = get_capability_by_id(capability_id)
    if row is None:
        raise ApiError(404, "bad_request", f"能力 'capability_id' 未找到")

    updated = update_capability(
        capability_id=capability_id,
        name=body.name,
        category=body.category.value if body.category else None,
        risk_level=body.risk_level.value if body.risk_level else None,
        default_budget_mode=body.default_budget_mode.value if body.default_budget_mode else None,
    )

    if not updated:
        raise ApiError(409, "bad_request", f"能力 '{capability_id}' 更新失败")

    diff = {}
    if body.name is not None:
        diff["name"] = body.name
    if body.category is not None:
        diff["category"] = body.category.value
    if body.risk_level is not None:
        diff["risk_level"] = body.risk_level.value
    if body.default_budget_mode is not None:
        diff["default_budget_mode"] = body.default_budget_mode.value

    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="gateway.capability.updated",
                result="success",
                object_type="capability",
                object_id=capability_id,
                diff_summary=diff,
            ),
        )
        db.commit()

    row = get_capability_by_id(capability_id)
    item = CapabilityRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ── Seed Capabilities ──────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.post(
    "/seed/capabilities",
    response_model=ResponseEnvelope[CapabilityListResponse],
    status_code=201,
    summary="初始化预定义能力",
)
def seed_capabilities_endpoint(
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """初始化预定义能力定义（幂等操作）。

    Uses ``ON CONFLICT ... DO UPDATE`` so this endpoint may be called
    multiple times safely — existing capabilities are updated to match
    the seed data.

    Pre-defined capabilities include:
    ``chat.completion``, ``chat.completion.streaming``, ``embedding.create``,
    ``image.generate``, ``vision.analyze``, ``audio.transcribe``,
    ``rerank.execute``, ``ocr.extract``, ``search.execute``.
    """
    results = seed_capabilities(SEED_CAPABILITIES)

    items = [
        CapabilityRead(
            capability_id=UUID(r["capability_id"]),
            capability_code=r["capability_code"],
            name=r["name"],
            category=r["category"],
            risk_level=r["risk_level"],
            default_budget_mode=r["default_budget_mode"],
        )
        for r in results
    ]

    # Audit
    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="gateway.capabilities.seeded",
                result="success",
                object_type="capability",
                diff_summary={"count": len(results)},
            ),
        )
        db.commit()

    page_info = PageInfo(
        page=1,
        page_size=max(len(results), 1),
        total_items=len(results),
        total_pages=1,
        has_next=False,
        has_previous=False,
    )

    data = CapabilityListResponse(items=items, page_info=page_info)
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ── Capability Bindings ────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


@router.post(
    "/bindings",
    response_model=ResponseEnvelope[CapabilityBindingRead],
    status_code=201,
)
def create_binding(
    body: CapabilityBindingCreate,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """创建能力绑定 — 将能力链接到 Provider/模型/凭据。

    A binding defines the route for a specific capability:
    * Which provider and model handles it
    * Which credential to use (from Vault)
    * Scope (global, project, sensitivity-based)
    * Sensitivity floor/ceiling boundaries
    * Budget mode and review requirements
    """
    # Validate referenced entities exist
    cap = get_capability_by_id(body.capability_id)
    if cap is None:
        raise ApiError(404, "bad_request", f"能力 'body.capability_id' 未找到")

    prov = get_provider_by_id(body.provider_id)
    if prov is None:
        raise ApiError(404, "bad_request", f"Provider 'body.provider_id' 未找到")

    if body.provider_model_id is not None:
        model = get_provider_model_by_id(body.provider_model_id)
        if model is None:
            raise ApiError(404, "bad_request", f"Provider模型 'body.provider_model_id' 未找到")
        if str(model.get("provider_id", "")) != str(body.provider_id):
            raise ApiError(
                422,
                "bad_request",
                f"model '{body.provider_model_id}' does not belong to provider '{body.provider_id}'",
            )

    # Check provider status: disabled providers cannot have active bindings
    if prov.get("status") == "disabled" and body.status.value == "active":
        raise ApiError(
            422,
            "bad_request",
            f"provider '{body.provider_id}' is disabled — cannot create active bindings",
        )

    try:
        row = create_capability_binding(
            capability_id=body.capability_id,
            provider_id=body.provider_id,
            provider_model_id=body.provider_model_id,
            credential_id=body.credential_id,
            project_id=body.project_id,
            binding_scope=body.binding_scope.value,
            status=body.status.value,
            priority=body.priority,
            sensitivity_floor=body.sensitivity_floor.value,
            sensitivity_ceiling=body.sensitivity_ceiling.value,
            budget_mode=body.budget_mode.value,
            require_review=body.require_review,
            allow_streaming=body.allow_streaming,
            timeout_seconds=body.timeout_seconds,
            rate_limit_key=body.rate_limit_key,
            policy_json=body.policy_json,
            metadata_json=body.metadata_json,
            created_by_user_id=body.created_by_user_id or context.actor.actor_id,
        )
    except IntegrityError:
        raise ApiError(
            500,
            "internal_error",
            "创建能力绑定失败",
        )

    binding_id = UUID(row["capability_binding_id"])

    # Audit
    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="gateway.binding.created",
                result="success",
                object_type="capability_binding",
                object_id=binding_id,
                diff_summary={
                    "capability_id": str(body.capability_id),
                    "provider_id": str(body.provider_id),
                    "binding_scope": body.binding_scope.value,
                },
            ),
        )
        db.commit()

    item = CapabilityBindingRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/bindings", response_model=ResponseEnvelope[CapabilityBindingListResponse])
def list_bindings(
    pagination: PaginationParams = Depends(),
    filters: BindingFilterParams = Depends(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """列出所有能力绑定，支持可选过滤。

    Query parameters:
    * ``page`` / ``page_size`` — pagination.
    * ``capability_id`` — filter by capability.
    * ``provider_id`` — filter by provider.
    * ``project_id`` — filter by project scope.
    * ``status`` — filter by status (active, disabled, degraded, shadow).
    * ``binding_scope`` — filter by scope (global, project, sensitivity, ...).
    """
    rows, total = get_capability_bindings(
        page=pagination.page,
        page_size=pagination.page_size,
        capability_id=filters.capability_id,
        provider_id=filters.provider_id,
        project_id=filters.project_id,
        status=filters.status.value if filters.status else None,
        binding_scope=filters.binding_scope.value if filters.binding_scope else None,
    )

    items = [CapabilityBindingRead(**row) for row in rows]
    total_pages = max(1, math.ceil(total / max(pagination.page_size, 1)))

    page_info = PageInfo(
        page=pagination.page,
        page_size=pagination.page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=pagination.page < total_pages,
        has_previous=pagination.page > 1,
    )

    data = CapabilityBindingListResponse(items=items, page_info=page_info)
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get(
    "/bindings/{binding_id}",
    response_model=ResponseEnvelope[CapabilityBindingRead],
)
def get_binding(
    binding_id: UUID,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """通过主键获取单个能力绑定。"""
    row = get_capability_binding_by_id(binding_id)
    if row is None:
        raise ApiError(404, "bad_request", f"能力绑定 'binding_id' 未找到")

    item = CapabilityBindingRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.put(
    "/bindings/{binding_id}",
    response_model=ResponseEnvelope[CapabilityBindingRead],
)
def update_binding_endpoint(
    binding_id: UUID,
    body: CapabilityBindingUpdate,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """更新已有的能力绑定。仅更新提供的字段。"""
    row = get_capability_binding_by_id(binding_id)
    if row is None:
        raise ApiError(404, "bad_request", f"能力绑定 'binding_id' 未找到")

    # Validate model/provider consistency if model is being changed
    if body.provider_model_id is not None:
        model = get_provider_model_by_id(body.provider_model_id)
        if model is None:
            raise ApiError(404, "bad_request", f"Provider模型 'body.provider_model_id' 未找到")
        if str(model.get("provider_id", "")) != str(row["provider_id"]):
            raise ApiError(
                422,
                "bad_request",
                f"模型 '{body.provider_model_id}' 不属于该绑定的 Provider",
            )

    updated = update_capability_binding(
        capability_binding_id=binding_id,
        provider_model_id=body.provider_model_id,
        credential_id=body.credential_id,
        status=body.status.value if body.status else None,
        priority=body.priority,
        sensitivity_floor=body.sensitivity_floor.value if body.sensitivity_floor else None,
        sensitivity_ceiling=body.sensitivity_ceiling.value if body.sensitivity_ceiling else None,
        budget_mode=body.budget_mode.value if body.budget_mode else None,
        require_review=body.require_review,
        allow_streaming=body.allow_streaming,
        timeout_seconds=body.timeout_seconds,
        rate_limit_key=body.rate_limit_key,
        policy_json=body.policy_json,
        metadata_json=body.metadata_json,
    )

    if not updated:
        raise ApiError(409, "bad_request", f"能力绑定 '{binding_id}' 更新失败")

    diff = {}
    if body.status is not None:
        diff["status"] = body.status.value
    if body.priority is not None:
        diff["priority"] = body.priority
    if body.require_review is not None:
        diff["require_review"] = body.require_review

    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="gateway.binding.updated",
                result="success",
                object_type="capability_binding",
                object_id=binding_id,
                diff_summary=diff,
            ),
        )
        db.commit()

    row = get_capability_binding_by_id(binding_id)
    item = CapabilityBindingRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ── P2-12 Gateway Unified Call ─────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════


def _map_gateway_error(exc: Exception) -> ApiError:
    """将网关特定异常映射为 ApiError 并返回适当的状态码。"""
    if isinstance(exc, BindingNotFoundError):
        return ApiError(
            404,
            "bad_request",
            str(exc),
            details=exc.details,
        )
    elif isinstance(exc, BudgetDeniedError):
        return ApiError(
            402,
            "gateway.budget_denied",
            str(exc),
            details=exc.details,
        )
    elif isinstance(exc, CredentialResolutionError):
        return ApiError(
            403,
            "permission_denied",
            str(exc),
            details=exc.details,
        )
    elif isinstance(exc, ProviderTimeoutError):
        return ApiError(
            504,
            "gateway.provider_timeout",
            str(exc),
            details=exc.details,
        )
    elif isinstance(exc, GatewayError):
        return ApiError(
            502,
            "gateway.provider_error",
            str(exc),
            details=exc.details,
        )
    else:
        return ApiError(
            500,
            "internal_error",
            str(exc),
        )


@router.post(
    "/call",
    response_model=ResponseEnvelope[GatewayCallResponse],
    summary="通过网关执行 Provider API 调用",
)
def gateway_call(
    body: GatewayCallRequest,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """所有外部 Provider API 调用的统一入口。

    Routes the request through capability binding resolution, budget check,
    Vault credential resolution, executes the provider call, and records
    everything in ``api_call_logs``.

    This is the **only** way to call external providers in Mneme.
    No bypass is allowed.

    Request body:
    * ``capability_code`` — e.g. ``chat.completion``, ``embedding.create``
    * ``params`` — provider-specific parameters
    * ``project_id`` — (optional) project context for binding resolution
    * ``sensitivity`` — data sensitivity level
    * ``call_type`` — category of the call
    """
    gw = get_gateway()

    try:
        result = gw.call(
            capability_code=body.capability_code,
            params=body.params,
            project_id=body.project_id,
            sensitivity=body.sensitivity.value,
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            auth_context_type=context.actor.auth_context_type,
            auth_context_id=context.actor.auth_context_id,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            idempotency_key=body.idempotency_key,
            call_type=body.call_type,
        )

        response_data = GatewayCallResponse(
            api_call_log_id=UUID(result["api_call_log_id"]),
            call_state=result["call_state"],
            latency_ms=result.get("latency_ms"),
            usage=GatewayCallUsage(**result.get("usage", {})),
            cost=GatewayCallCost(**result.get("cost", {})),
            data=result.get("data", {}),
        )

        # Audit the gateway call
        with SessionLocal() as db:
            add_audit_event(
                db,
                context,
                audit_event_for_action(
                    action="gateway.call.completed",
                    result="success",
                    object_type="api_call",
                    object_id=UUID(result["api_call_log_id"]),
                    diff_summary={
                        "capability_code": body.capability_code,
                        "call_state": result["call_state"],
                        "latency_ms": result.get("latency_ms"),
                    },
                ),
            )
            db.commit()

        return envelope(
            response_data.model_dump(),
            request_id=context.request_id,
            correlation_id=context.correlation_id,
        )

    except GatewayError as exc:
        # Gateway errors are expected — they have proper api_call_logs records
        logger.warning(
            "Gateway call failed: code=%s log_id=%s message=%s",
            exc.code, exc.api_call_log_id, str(exc),
        )
        # Audit the failure
        if exc.api_call_log_id:
            with SessionLocal() as db:
                add_audit_event(
                    db,
                    context,
                    audit_event_for_action(
                        action="gateway.call.completed",
                        result="failure",
                        object_type="api_call",
                        object_id=exc.api_call_log_id,
                        diff_summary={
                            "capability_code": body.capability_code,
                            "error_code": exc.code,
                            "call_state": exc.call_state or "failed",
                        },
                    ),
                )
                db.commit()
        raise _map_gateway_error(exc)


# ═══════════════════════════════════════════════════════════════════════════════
# P2-13 Budget / Usage Limits API
# ═══════════════════════════════════════════════════════════════════════════════


@router.post(
    "/limits",
    response_model=ResponseEnvelope[UsageLimitRead],
    status_code=201,
    summary="创建用量限制规则",
)
def create_limit(
    body: UsageLimitCreate,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """创建新的用量限制规则。

    Defines budget/rate limits for a subject (user/agent/project/capability/provider)
    within a time window.
    """
    row = create_usage_limit(
        subject_type=body.subject_type.value,
        subject_id=body.subject_id,
        capability_id=body.capability_id,
        provider_id=body.provider_id,
        project_id=body.project_id,
        limit_scope=body.limit_scope.value,
        window_unit=body.window_unit.value,
        max_requests=body.max_requests,
        max_input_tokens=body.max_input_tokens,
        max_output_tokens=body.max_output_tokens,
        max_total_tokens=body.max_total_tokens,
        max_cost=body.max_cost,
        approval_threshold_cost=body.approval_threshold_cost,
        block_threshold_cost=body.block_threshold_cost,
        enabled=body.enabled,
    )

    limit_id = UUID(row["usage_limit_id"])

    # Audit
    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="gateway.limit.created",
                result="success",
                object_type="usage_limit",
                object_id=limit_id,
                diff_summary={
                    "subject_type": body.subject_type.value,
                    "subject_id": str(body.subject_id),
                    "limit_scope": body.limit_scope.value,
                    "window_unit": body.window_unit.value,
                },
            ),
        )
        db.commit()

    item = UsageLimitRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get(
    "/limits",
    response_model=ResponseEnvelope[UsageLimitListResponse],
    summary="列出用量限制",
)
def list_limits(
    pagination: PaginationParams = Depends(),
    filters: UsageLimitFilterParams = Depends(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """列出所有用量限制，支持可选过滤。

    Query parameters:
    * ``page`` / ``page_size`` — pagination (default 1/50, max 200).
    * ``subject_type`` — filter by subject type (user, agent, project, ...).
    * ``subject_id`` — filter by exact subject.
    * ``capability_id`` / ``provider_id`` / ``project_id`` — filter by FK.
    * ``limit_scope`` — filter by scope (global, provider, ...).
    * ``enabled`` — filter by enabled status.
    """
    rows, total = get_usage_limits(
        page=pagination.page,
        page_size=pagination.page_size,
        subject_type=filters.subject_type.value if filters.subject_type else None,
        subject_id=filters.subject_id,
        capability_id=filters.capability_id,
        provider_id=filters.provider_id,
        project_id=filters.project_id,
        limit_scope=filters.limit_scope.value if filters.limit_scope else None,
        enabled=filters.enabled,
    )

    items = [UsageLimitRead(**row) for row in rows]
    total_pages = max(1, math.ceil(total / max(pagination.page_size, 1)))

    page_info = PageInfo(
        page=pagination.page,
        page_size=pagination.page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=pagination.page < total_pages,
        has_previous=pagination.page > 1,
    )

    data = UsageLimitListResponse(items=items, page_info=page_info)
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get(
    "/limits/{limit_id}",
    response_model=ResponseEnvelope[UsageLimitRead],
    summary="获取用量限制",
)
def get_limit(
    limit_id: UUID,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """通过主键获取单个用量限制。"""
    row = get_usage_limit_by_id(limit_id)
    if row is None:
        raise ApiError(404, "bad_request", f"用量限制 'limit_id' 未找到")

    item = UsageLimitRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.put(
    "/limits/{limit_id}",
    response_model=ResponseEnvelope[UsageLimitRead],
    summary="更新用量限制",
)
def update_limit(
    limit_id: UUID,
    body: UsageLimitUpdate,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """更新已有的用量限制。仅更新提供的字段。"""
    row = get_usage_limit_by_id(limit_id)
    if row is None:
        raise ApiError(404, "bad_request", f"用量限制 'limit_id' 未找到")

    updated = update_usage_limit(
        usage_limit_id=limit_id,
        max_requests=body.max_requests,
        max_input_tokens=body.max_input_tokens,
        max_output_tokens=body.max_output_tokens,
        max_total_tokens=body.max_total_tokens,
        max_cost=body.max_cost,
        approval_threshold_cost=body.approval_threshold_cost,
        block_threshold_cost=body.block_threshold_cost,
        enabled=body.enabled,
        window_unit=body.window_unit.value if body.window_unit else None,
    )

    if not updated:
        raise ApiError(409, "bad_request", f"用量限制 '{limit_id}' 更新失败")

    # Audit
    diff: dict = {}
    if body.max_cost is not None:
        diff["max_cost"] = body.max_cost
    if body.enabled is not None:
        diff["enabled"] = body.enabled
    if body.window_unit is not None:
        diff["window_unit"] = body.window_unit.value

    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="gateway.limit.updated",
                result="success",
                object_type="usage_limit",
                object_id=limit_id,
                diff_summary=diff,
            ),
        )
        db.commit()

    row = get_usage_limit_by_id(limit_id)
    item = UsageLimitRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.delete(
    "/limits/{limit_id}",
    response_model=ResponseEnvelope[dict],
    summary="删除用量限制",
)
def delete_limit(
    limit_id: UUID,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """删除用量限制规则。"""
    row = get_usage_limit_by_id(limit_id)
    if row is None:
        raise ApiError(404, "bad_request", f"用量限制 'limit_id' 未找到")

    deleted = delete_usage_limit(limit_id)
    if not deleted:
        raise ApiError(409, "bad_request", f"用量限制 '{limit_id}' 删除失败")

    # Audit
    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="gateway.limit.deleted",
                result="success",
                object_type="usage_limit",
                object_id=limit_id,
            ),
        )
        db.commit()

    return envelope(
        {"deleted": True, "usage_limit_id": str(limit_id)},
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get(
    "/limits/{limit_id}/usage",
    response_model=ResponseEnvelope[LimitUsageRead],
    summary="获取用量限制的当前使用数据",
)
def get_limit_usage_endpoint(
    limit_id: UUID,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """返回指定用量限制的当前使用数据。

    Aggregates committed costs, tokens, and requests from ``budget_tracking``
    within the limit's configured time window.
    """
    usage_data = get_limit_usage(limit_id)
    if usage_data is None:
        raise ApiError(404, "bad_request", f"用量限制 'limit_id' 未找到")

    item = LimitUsageRead(**usage_data)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
