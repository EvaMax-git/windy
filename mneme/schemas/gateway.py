"""P2-11 Gateway schemas — providers, provider_models, capabilities, capability_bindings.

Schema alignment
----------------
All enumerations and field names match the DDL CHECK constraints defined in
``0001_baseline_45_tables.py``.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PaginatedData


# ═══════════════════════════════════════════════════════════════════════════════
# Provider enums
# ═══════════════════════════════════════════════════════════════════════════════


class ProviderType(str, Enum):
    """``providers.provider_type`` CHECK constraint values."""
    llm = "llm"
    embedding = "embedding"
    ocr = "ocr"
    search = "search"
    storage = "storage"
    webhook = "webhook"


class ProviderStatus(str, Enum):
    """``providers.status`` CHECK constraint values."""
    active = "active"
    disabled = "disabled"
    degraded = "degraded"


# ═══════════════════════════════════════════════════════════════════════════════
# Provider model enums
# ═══════════════════════════════════════════════════════════════════════════════


class ModelType(str, Enum):
    """``provider_models.model_type`` CHECK constraint values."""
    chat = "chat"
    embedding = "embedding"
    rerank = "rerank"
    ocr = "ocr"
    vision = "vision"
    audio = "audio"
    search = "search"
    storage = "storage"
    custom_http = "custom_http"


class ModelStatus(str, Enum):
    """``provider_models.status`` CHECK constraint values."""
    active = "active"
    disabled = "disabled"
    degraded = "degraded"
    deprecated = "deprecated"


class SensitivityLevel(str, Enum):
    """Sensitivity levels used by models, capability bindings, etc."""
    public = "public"
    normal = "normal"
    private = "private"
    sensitive = "sensitive"
    secret = "secret"


# ═══════════════════════════════════════════════════════════════════════════════
# Capability enums
# ═══════════════════════════════════════════════════════════════════════════════


class CapabilityCategory(str, Enum):
    """``capabilities.category`` CHECK constraint values."""
    chat = "chat"
    embedding = "embedding"
    ocr = "ocr"
    rerank = "rerank"
    search = "search"
    export = "export"
    admin = "admin"


class RiskLevel(str, Enum):
    """``capabilities.risk_level`` CHECK constraint values."""
    low = "low"
    normal = "normal"
    high = "high"
    critical = "critical"


class DefaultBudgetMode(str, Enum):
    """``capabilities.default_budget_mode`` CHECK constraint values."""
    free = "free"
    metered = "metered"
    approval_required = "approval_required"


# ═══════════════════════════════════════════════════════════════════════════════
# Capability binding enums
# ═══════════════════════════════════════════════════════════════════════════════


class BindingScope(str, Enum):
    """``capability_bindings.binding_scope`` CHECK constraint values."""
    global_ = "global"
    project = "project"
    sensitivity = "sensitivity"
    project_sensitivity = "project_sensitivity"


class BindingStatus(str, Enum):
    """``capability_bindings.status`` CHECK constraint values."""
    active = "active"
    disabled = "disabled"
    degraded = "degraded"
    shadow = "shadow"


class BindingBudgetMode(str, Enum):
    """``capability_bindings.budget_mode`` CHECK constraint values."""
    free = "free"
    metered = "metered"
    approval_required = "approval_required"


# ═══════════════════════════════════════════════════════════════════════════════
# Filter params
# ═══════════════════════════════════════════════════════════════════════════════


class ProviderFilterParams(ApiSchema):
    """Query-string filters for ``GET /gateway/providers``."""
    provider_type: ProviderType | None = None
    status: ProviderStatus | None = None
    search: str | None = Field(default=None, description="Search in provider name or code")


class ModelFilterParams(ApiSchema):
    """Query-string filters for ``GET /gateway/providers/{id}/models``."""
    model_type: ModelType | None = None
    status: ModelStatus | None = None
    search: str | None = Field(default=None, description="Search in model_code, external_model_id, or display_name")


class CapabilityFilterParams(ApiSchema):
    """Query-string filters for ``GET /gateway/capabilities``."""
    category: CapabilityCategory | None = None
    risk_level: RiskLevel | None = None
    search: str | None = Field(default=None, description="Search in capability_code or name")


class BindingFilterParams(ApiSchema):
    """Query-string filters for ``GET /gateway/bindings``."""
    capability_id: UUID | None = None
    provider_id: UUID | None = None
    project_id: UUID | None = None
    status: BindingStatus | None = None
    binding_scope: BindingScope | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# Provider schemas
# ═══════════════════════════════════════════════════════════════════════════════


class ProviderCreate(ApiSchema):
    """Request body for ``POST /gateway/providers``."""
    provider_code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    provider_type: ProviderType
    status: ProviderStatus = ProviderStatus.active
    endpoint_base: str | None = None
    config_json: dict[str, Any] = Field(default_factory=dict)


class ProviderUpdate(ApiSchema):
    """Request body for ``PUT /gateway/providers/{id}``."""
    name: str | None = Field(default=None, min_length=1, max_length=120)
    provider_type: ProviderType | None = None
    status: ProviderStatus | None = None
    endpoint_base: str | None = None
    config_json: dict[str, Any] | None = None


class ProviderRead(ApiSchema):
    """A ``providers`` row returned by API endpoints."""
    provider_id: UUID
    provider_code: str
    name: str
    provider_type: str
    status: str
    endpoint_base: str | None = None
    config_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProviderListResponse(PaginatedData[ProviderRead]):
    """Paginated list of providers."""
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# Provider model schemas
# ═══════════════════════════════════════════════════════════════════════════════


class ProviderModelCreate(ApiSchema):
    """Request body for ``POST /gateway/providers/{id}/models``."""
    model_code: str = Field(min_length=1, max_length=120)
    external_model_id: str = Field(min_length=1, max_length=160)
    model_type: ModelType
    status: ModelStatus = ModelStatus.active
    display_name: str | None = Field(default=None, max_length=160)
    version_label: str | None = Field(default=None, max_length=80)
    context_window_tokens: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    input_price_per_1k: float | None = None
    output_price_per_1k: float | None = None
    currency_code: str = "USD"
    supports_streaming: bool = False
    supports_json_mode: bool = False
    supports_tools: bool = False
    supports_vision: bool = False
    sensitivity_ceiling: SensitivityLevel = SensitivityLevel.private
    config_json: dict[str, Any] = Field(default_factory=dict)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    deprecated_at: datetime | None = None


class ProviderModelUpdate(ApiSchema):
    """Request body for ``PUT /gateway/providers/{id}/models/{model_id}``."""
    status: ModelStatus | None = None
    display_name: str | None = Field(default=None, max_length=160)
    version_label: str | None = Field(default=None, max_length=80)
    context_window_tokens: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    input_price_per_1k: float | None = None
    output_price_per_1k: float | None = None
    supports_streaming: bool | None = None
    supports_json_mode: bool | None = None
    supports_tools: bool | None = None
    supports_vision: bool | None = None
    sensitivity_ceiling: SensitivityLevel | None = None
    config_json: dict[str, Any] | None = None
    metadata_json: dict[str, Any] | None = None
    deprecated_at: datetime | None = None


class ProviderModelRead(ApiSchema):
    """A ``provider_models`` row returned by API endpoints."""
    provider_model_id: UUID
    provider_id: UUID
    model_code: str
    external_model_id: str
    model_type: str
    status: str
    display_name: str | None = None
    version_label: str | None = None
    context_window_tokens: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    input_price_per_1k: float | None = None
    output_price_per_1k: float | None = None
    currency_code: str = "USD"
    supports_streaming: bool = False
    supports_json_mode: bool = False
    supports_tools: bool = False
    supports_vision: bool = False
    sensitivity_ceiling: str = "private"
    config_json: dict[str, Any] = Field(default_factory=dict)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    deprecated_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProviderModelListResponse(PaginatedData[ProviderModelRead]):
    """Paginated list of provider models."""
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# Capability schemas
# ═══════════════════════════════════════════════════════════════════════════════


class CapabilityCreate(ApiSchema):
    """Request body for ``POST /gateway/capabilities``."""
    capability_code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    category: CapabilityCategory
    risk_level: RiskLevel = RiskLevel.normal
    default_budget_mode: DefaultBudgetMode = DefaultBudgetMode.metered


class CapabilityUpdate(ApiSchema):
    """Request body for ``PUT /gateway/capabilities/{id}``."""
    name: str | None = Field(default=None, min_length=1, max_length=120)
    category: CapabilityCategory | None = None
    risk_level: RiskLevel | None = None
    default_budget_mode: DefaultBudgetMode | None = None


class CapabilityRead(ApiSchema):
    """A ``capabilities`` row returned by API endpoints."""
    capability_id: UUID
    capability_code: str
    name: str
    category: str
    risk_level: str
    default_budget_mode: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CapabilityListResponse(PaginatedData[CapabilityRead]):
    """Paginated list of capabilities."""
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# Capability binding schemas
# ═══════════════════════════════════════════════════════════════════════════════


class CapabilityBindingCreate(ApiSchema):
    """Request body for ``POST /gateway/bindings``."""
    capability_id: UUID
    provider_id: UUID
    provider_model_id: UUID | None = None
    credential_id: UUID | None = None
    project_id: UUID | None = None
    binding_scope: BindingScope = BindingScope.global_
    status: BindingStatus = BindingStatus.active
    priority: int = Field(default=100, ge=0, le=1000)
    sensitivity_floor: SensitivityLevel = SensitivityLevel.public
    sensitivity_ceiling: SensitivityLevel = SensitivityLevel.private
    budget_mode: BindingBudgetMode = BindingBudgetMode.metered
    require_review: bool = False
    allow_streaming: bool = True
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    rate_limit_key: str | None = Field(default=None, max_length=120)
    policy_json: dict[str, Any] = Field(default_factory=dict)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    created_by_user_id: UUID | None = None


class CapabilityBindingUpdate(ApiSchema):
    """Request body for ``PUT /gateway/bindings/{id}``."""
    provider_model_id: UUID | None = None
    credential_id: UUID | None = None
    status: BindingStatus | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)
    sensitivity_floor: SensitivityLevel | None = None
    sensitivity_ceiling: SensitivityLevel | None = None
    budget_mode: BindingBudgetMode | None = None
    require_review: bool | None = None
    allow_streaming: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    rate_limit_key: str | None = Field(default=None, max_length=120)
    policy_json: dict[str, Any] | None = None
    metadata_json: dict[str, Any] | None = None


class CapabilityBindingRead(ApiSchema):
    """A ``capability_bindings`` row returned by API endpoints."""
    capability_binding_id: UUID
    capability_id: UUID
    provider_id: UUID
    provider_model_id: UUID | None = None
    credential_id: UUID | None = None
    project_id: UUID | None = None
    binding_scope: str = "global"
    status: str = "active"
    priority: int = 100
    sensitivity_floor: str = "public"
    sensitivity_ceiling: str = "private"
    budget_mode: str = "metered"
    require_review: bool = False
    allow_streaming: bool = True
    timeout_seconds: int = 120
    rate_limit_key: str | None = None
    policy_json: dict[str, Any] = Field(default_factory=dict)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    created_by_user_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CapabilityBindingListResponse(PaginatedData[CapabilityBindingRead]):
    """Paginated list of capability bindings."""
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# P2-12 Gateway unified call schemas
# ═══════════════════════════════════════════════════════════════════════════════


class GatewayCallRequest(ApiSchema):
    """Request body for ``POST /gateway/call`` — unified provider call entry."""
    capability_code: str = Field(
        min_length=1, max_length=64,
        description="Capability code, e.g. 'chat.completion'",
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Provider-specific parameters (model, messages, temperature, ...)",
    )
    project_id: UUID | None = Field(
        default=None,
        description="Project context for binding resolution",
    )
    sensitivity: SensitivityLevel = Field(
        default=SensitivityLevel.private,
        description="Data sensitivity level of this call",
    )
    call_type: str = Field(
        default="chat",
        description="Category: chat, embedding, ocr, vision, audio, search, storage, custom_http",
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=255,
        description="Unique idempotency key. Auto-generated if not provided.",
    )


class GatewayCallUsage(ApiSchema):
    """Token usage from a Gateway call."""
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class GatewayCallCost(ApiSchema):
    """Cost breakdown for a Gateway call."""
    estimated: float = 0.0
    actual: float = 0.0


class GatewayCallData(ApiSchema):
    """Response data from a Gateway call."""
    provider_response: dict[str, Any] = Field(default_factory=dict)


class GatewayCallResponse(ApiSchema):
    """Response body for ``POST /gateway/call``."""
    api_call_log_id: UUID
    call_state: str
    latency_ms: int | None = None
    usage: GatewayCallUsage = Field(default_factory=GatewayCallUsage)
    cost: GatewayCallCost = Field(default_factory=GatewayCallCost)
    data: dict[str, Any] = Field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════════
# Seed data definitions
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# P2-13 Usage Limits / Budget schemas
# ═══════════════════════════════════════════════════════════════════════════════


class LimitSubjectType(str, Enum):
    """``usage_limits.subject_type`` CHECK constraint values."""
    user = "user"
    agent = "agent"
    project = "project"
    capability = "capability"
    provider = "provider"


class LimitScope(str, Enum):
    """``usage_limits.limit_scope`` CHECK constraint values."""
    global_ = "global"
    provider = "provider"
    capability = "capability"
    project = "project"


class LimitWindowUnit(str, Enum):
    """``usage_limits.window_unit`` CHECK constraint values."""
    minute = "minute"
    hour = "hour"
    day = "day"
    month = "month"


class ReservationState(str, Enum):
    """``budget_tracking.reservation_state`` CHECK constraint values."""
    reserved = "reserved"
    committed = "committed"
    released = "released"
    denied = "denied"
    refunded = "refunded"


class UsageLimitCreate(ApiSchema):
    """Request body for ``POST /gateway/limits``."""
    subject_type: LimitSubjectType
    subject_id: UUID
    capability_id: UUID | None = None
    provider_id: UUID | None = None
    project_id: UUID | None = None
    limit_scope: LimitScope = LimitScope.global_
    window_unit: LimitWindowUnit = LimitWindowUnit.day
    max_requests: int | None = Field(default=None, ge=0)
    max_input_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=0)
    max_total_tokens: int | None = Field(default=None, ge=0)
    max_cost: float | None = Field(default=None, ge=0)
    approval_threshold_cost: float | None = Field(default=None, ge=0)
    block_threshold_cost: float | None = Field(default=None, ge=0)
    enabled: bool = True


class UsageLimitUpdate(ApiSchema):
    """Request body for ``PUT /gateway/limits/{id}``."""
    max_requests: int | None = Field(default=None, ge=0)
    max_input_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=0)
    max_total_tokens: int | None = Field(default=None, ge=0)
    max_cost: float | None = Field(default=None, ge=0)
    approval_threshold_cost: float | None = Field(default=None, ge=0)
    block_threshold_cost: float | None = Field(default=None, ge=0)
    enabled: bool | None = None
    window_unit: LimitWindowUnit | None = None


class UsageLimitRead(ApiSchema):
    """A ``usage_limits`` row returned by API endpoints."""
    usage_limit_id: UUID
    subject_type: str
    subject_id: UUID
    capability_id: UUID | None = None
    provider_id: UUID | None = None
    project_id: UUID | None = None
    limit_scope: str
    window_unit: str
    max_requests: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_cost: float | None = None
    approval_threshold_cost: float | None = None
    block_threshold_cost: float | None = None
    enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


class UsageLimitListResponse(PaginatedData[UsageLimitRead]):
    """Paginated list of usage limits."""
    pass


class UsageLimitFilterParams(ApiSchema):
    """Query-string filters for ``GET /gateway/limits``."""
    subject_type: LimitSubjectType | None = None
    subject_id: UUID | None = None
    capability_id: UUID | None = None
    provider_id: UUID | None = None
    project_id: UUID | None = None
    limit_scope: LimitScope | None = None
    enabled: bool | None = None


class LimitUsageRead(ApiSchema):
    """Current usage data for a specific usage limit."""
    usage_limit_id: UUID
    window_unit: str
    window_start: datetime
    window_end: datetime
    total_requests: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_total_tokens: int = 0
    total_committed_cost: float = 0.0
    limits: UsageLimitRead


class BudgetTrackingRead(ApiSchema):
    """A ``budget_tracking`` row."""
    budget_tracking_id: UUID
    request_id: UUID
    correlation_id: UUID
    subject_type: str
    subject_id: UUID
    capability_id: UUID | None = None
    provider_id: UUID | None = None
    project_id: UUID | None = None
    reservation_state: str
    currency_code: str = "USD"
    estimated_input_tokens: int | None = None
    estimated_output_tokens: int | None = None
    actual_input_tokens: int | None = None
    actual_output_tokens: int | None = None
    reserved_cost: float = 0.0
    committed_cost: float = 0.0
    released_cost: float = 0.0
    denied_reason: str | None = None
    provider_request_fingerprint: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


SEED_CAPABILITIES: list[dict[str, str]] = [
    {
        "capability_code": "chat.completion",
        "name": "Chat Completion",
        "category": "chat",
        "risk_level": "normal",
        "default_budget_mode": "metered",
    },
    {
        "capability_code": "chat.completion.streaming",
        "name": "Chat Completion (Streaming)",
        "category": "chat",
        "risk_level": "normal",
        "default_budget_mode": "metered",
    },
    {
        "capability_code": "embedding.create",
        "name": "Create Embeddings",
        "category": "embedding",
        "risk_level": "low",
        "default_budget_mode": "metered",
    },
    {
        "capability_code": "image.generate",
        "name": "Image Generation",
        "category": "chat",
        "risk_level": "normal",
        "default_budget_mode": "metered",
    },
    {
        "capability_code": "vision.analyze",
        "name": "Vision Analysis",
        "category": "chat",
        "risk_level": "normal",
        "default_budget_mode": "metered",
    },
    {
        "capability_code": "audio.transcribe",
        "name": "Audio Transcription",
        "category": "chat",
        "risk_level": "low",
        "default_budget_mode": "metered",
    },
    {
        "capability_code": "rerank.execute",
        "name": "Rerank Documents",
        "category": "rerank",
        "risk_level": "low",
        "default_budget_mode": "metered",
    },
    {
        "capability_code": "ocr.extract",
        "name": "OCR Text Extraction",
        "category": "ocr",
        "risk_level": "normal",
        "default_budget_mode": "metered",
    },
    {
        "capability_code": "search.execute",
        "name": "Search Execution",
        "category": "search",
        "risk_level": "low",
        "default_budget_mode": "metered",
    },
]
