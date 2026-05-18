from __future__ import annotations

from enum import Enum
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


DataT = TypeVar("DataT")
ItemT = TypeVar("ItemT")


class ApiSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ActorType(str, Enum):
    user = "user"
    agent = "agent"
    service = "service"
    system = "system"


class AuthContextType(str, Enum):
    user_session = "user_session"
    agent_token = "agent_token"
    service_identity = "service_identity"
    system_job = "system_job"


class SensitivityLevel(str, Enum):
    public = "public"
    normal = "normal"
    private = "private"
    sensitive = "sensitive"
    secret = "secret"


class ErrorCode(str, Enum):
    bad_request = "bad_request"
    auth_required = "auth_required"
    permission_denied = "permission_denied"
    idempotency_conflict = "idempotency_conflict"
    review_required = "review_required"
    step_up_required = "step_up_required"
    rate_limited = "rate_limited"
    dependency_unavailable = "dependency_unavailable"
    internal_error = "internal_error"


class ActorRef(ApiSchema):
    actor_type: ActorType
    actor_id: UUID | None = None
    auth_context_type: AuthContextType | None = None
    auth_context_id: UUID | None = None


class RequestHeaders(ApiSchema):
    x_request_id: UUID | None = Field(
        default=None,
        alias="X-Request-Id",
        description="可选的请求ID。如果省略，API 将生成一个并返回到响应信封和响应头中。",
    )
    x_correlation_id: UUID | None = Field(
        default=None,
        alias="X-Correlation-Id",
        description="可选的关联ID。如果省略，将默认为 X-Request-Id 或生成的请求ID。",
    )
    idempotency_key: str | None = Field(
        default=None,
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        description="读取请求中可选。在创建审计/发件箱记录的 Phase 1 写入请求中必需。",
    )


class WriteRequestHeaders(RequestHeaders):
    idempotency_key: str = Field(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        description="必需的写入幂等键。重复使用不应创建重复的业务对象或发件箱事件。",
    )


class ErrorBody(ApiSchema):
    code: ErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(ApiSchema):
    request_id: UUID
    correlation_id: UUID
    error: ErrorBody


class ResponseEnvelope(ApiSchema, Generic[DataT]):
    request_id: UUID
    correlation_id: UUID
    data: DataT
    meta: dict[str, Any] = Field(default_factory=dict)


class PageInfo(ApiSchema):
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=200)
    total_items: int = Field(ge=0)
    total_pages: int = Field(ge=0)
    has_next: bool
    has_previous: bool


class PaginationParams(ApiSchema):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class PaginatedData(ApiSchema, Generic[ItemT]):
    items: list[ItemT]
    page_info: PageInfo


def envelope(
    data: Any,
    *,
    request_id: UUID,
    correlation_id: UUID,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "request_id": str(request_id),
        "correlation_id": str(correlation_id),
        "data": data,
        "meta": meta or {},
    }


def error_envelope(
    *,
    request_id: UUID,
    correlation_id: UUID,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "request_id": str(request_id),
        "correlation_id": str(correlation_id),
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        },
    }

