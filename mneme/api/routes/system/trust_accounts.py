"""信任账户 API — trust_accounts CRUD + call/feedback recording。

接口列表
--------
* ``POST   /api/v4/trust/accounts``              – 创建或获取信任账户
* ``GET    /api/v4/trust/accounts``              – 分页列表，支持过滤
* ``GET    /api/v4/trust/accounts/{id}``         – 单条信任账户详情
* ``POST   /api/v4/trust/accounts/{id}/record-call``     – 记录一次调用结果
* ``POST   /api/v4/trust/accounts/{id}/record-feedback`` – 记录用户反馈
* ``GET    /api/v4/trust/accounts/by-subject``    – 按 subject 查询账户
"""

from __future__ import annotations

import math
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.trust_accounts import (
    get_or_create_trust_account,
    get_trust_account_by_id,
    get_trust_account_by_subject,
    get_trust_accounts,
    record_call,
    record_feedback,
)
from mneme.schemas import (
    PageInfo,
    PaginationParams,
    ResponseEnvelope,
)
from mneme.schemas.trust_accounts import (
    TrustAccountCreate,
    TrustAccountFilterParams,
    TrustAccountListResponse,
    TrustAccountRead,
    TrustAccountRecordCall,
    TrustAccountRecordFeedback,
)

router = APIRouter(prefix="/trust", tags=["trust"])


# ──────────────────────────────────────────────────────────────────────────────
# POST /trust/accounts
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/accounts", response_model=ResponseEnvelope[TrustAccountRead], status_code=201)
def create_or_get_trust_account_endpoint(
    body: TrustAccountCreate,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """创建或获取一个信任账户。

    信任账户按 ``(subject_type, subject_id, capability_id)`` 唯一标识。
    如果已存在则返回现有账户，否则创建新账户。

    默认 trust_score = 0.5000，随着调用和反馈记录动态更新。
    """
    row = get_or_create_trust_account(
        subject_type=body.subject_type.value,
        subject_id=body.subject_id,
        capability_id=body.capability_id,
        metadata_json=body.metadata_json,
    )

    item = TrustAccountRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# GET /trust/accounts
# ──────────────────────────────────────────────────────────────────────────────


@router.get("/accounts", response_model=ResponseEnvelope[TrustAccountListResponse])
def list_trust_accounts(
    pagination: PaginationParams = Depends(),
    filters: TrustAccountFilterParams = Depends(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """列出信任账户，支持可选过滤。

    Query parameters
    ----------------
    * ``page`` / ``page_size`` — pagination (default 1 / 50, max 200).
    * ``subject_type`` — 按主体类型过滤（agent / user / service / system）。
    * ``subject_id`` — 按主体 ID 过滤。
    * ``min_trust_score`` / ``max_trust_score`` — 信任分数范围过滤。
    """
    rows, total = get_trust_accounts(
        page=pagination.page,
        page_size=pagination.page_size,
        subject_type=filters.subject_type.value if filters.subject_type else None,
        subject_id=filters.subject_id,
        min_trust_score=filters.min_trust_score,
        max_trust_score=filters.max_trust_score,
    )

    items = [TrustAccountRead(**row) for row in rows]
    total_pages = max(1, math.ceil(total / max(pagination.page_size, 1)))

    page_info = PageInfo(
        page=pagination.page,
        page_size=pagination.page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=pagination.page < total_pages,
        has_previous=pagination.page > 1,
    )

    data = TrustAccountListResponse(items=items, page_info=page_info)
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# GET /trust/accounts/by-subject
# ──────────────────────────────────────────────────────────────────────────────


@router.get("/accounts/by-subject", response_model=ResponseEnvelope[TrustAccountRead])
def get_trust_account_by_subject_endpoint(
    subject_type: str = Query(..., description="Subject type: agent, user, service, system"),
    subject_id: UUID = Query(..., description="Subject ID"),
    capability_id: UUID | None = Query(default=None, description="Optional capability scope"),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """按主体查询信任账户。"""
    row = get_trust_account_by_subject(
        subject_type=subject_type,
        subject_id=subject_id,
        capability_id=capability_id,
    )

    if row is None:
        raise ApiError(
            404,
            "bad_request",
            f"trust_account not found for {subject_type}/{subject_id}",
        )

    item = TrustAccountRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# GET /trust/accounts/{trust_account_id}
# ──────────────────────────────────────────────────────────────────────────────


@router.get("/accounts/{trust_account_id}", response_model=ResponseEnvelope[TrustAccountRead])
def get_trust_account_detail(
    trust_account_id: UUID,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """通过主键返回单条信任账户详情。"""
    row = get_trust_account_by_id(trust_account_id)
    if row is None:
        raise ApiError(
            404,
            "bad_request",
            f"trust_account '{trust_account_id}' not found",
        )

    item = TrustAccountRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /trust/accounts/{trust_account_id}/record-call
# ──────────────────────────────────────────────────────────────────────────────


@router.post(
    "/accounts/{trust_account_id}/record-call",
    response_model=ResponseEnvelope[TrustAccountRead],
)
def record_call_endpoint(
    trust_account_id: UUID,
    body: TrustAccountRecordCall = TrustAccountRecordCall(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """记录一次 API/能力调用结果。

    每次调用会更新：
    * ``total_calls`` (+1)
    * ``successful_calls`` 或 ``failed_calls`` (+1)
    * ``success_rate`` (重新计算)
    * ``trust_score`` (重新计算：0.5 * success_rate + 0.3 * feedback_ratio + 0.2 * activity_bonus)
    """
    row = record_call(
        trust_account_id=trust_account_id,
        success=body.success,
    )

    if row is None:
        raise ApiError(
            404,
            "bad_request",
            f"trust_account '{trust_account_id}' not found",
        )

    item = TrustAccountRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /trust/accounts/{trust_account_id}/record-feedback
# ──────────────────────────────────────────────────────────────────────────────


@router.post(
    "/accounts/{trust_account_id}/record-feedback",
    response_model=ResponseEnvelope[TrustAccountRead],
)
def record_feedback_endpoint(
    trust_account_id: UUID,
    body: TrustAccountRecordFeedback,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """记录用户反馈（正面 / 负面 / 中性）。

    每次反馈更新：
    * ``positive_feedback`` / ``negative_feedback`` / ``neutral_feedback`` (+1)
    * ``trust_score`` (重新计算)
    """
    row = record_feedback(
        trust_account_id=trust_account_id,
        feedback_type=body.feedback_type,
    )

    if row is None:
        raise ApiError(
            404,
            "bad_request",
            f"trust_account '{trust_account_id}' not found",
        )

    item = TrustAccountRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
