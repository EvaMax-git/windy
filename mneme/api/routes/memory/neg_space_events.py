"""负空间事件 API — neg_space_events CRUD + 聚合摘要。

接口列表
--------
* ``POST   /api/v4/neg-space/events``           – 创建负空间事件
* ``GET    /api/v4/neg-space/events``           – 分页列表，支持过滤
* ``GET    /api/v4/neg-space/events/{event_id}`` – 单条事件详情
* ``GET    /api/v4/neg-space/summary``          – 按 agent/conversation 聚合摘要
"""

from __future__ import annotations

import math
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query

from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.neg_space_events import (
    get_neg_space_event_by_id,
    get_neg_space_events,
    get_neg_space_summary,
    insert_neg_space_event,
)
from mneme.schemas import (
    PageInfo,
    PaginationParams,
    ResponseEnvelope,
)
from mneme.schemas.neg_space_events import (
    NegSpaceEventCreate,
    NegSpaceEventFilterParams,
    NegSpaceEventListResponse,
    NegSpaceEventRead,
    NegSpaceSummary,
)

router = APIRouter(prefix="/neg-space", tags=["neg-space"])


# ──────────────────────────────────────────────────────────────────────────────
# POST /neg-space/events
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/events", response_model=ResponseEnvelope[NegSpaceEventRead], status_code=201)
def create_neg_space_event_endpoint(
    body: NegSpaceEventCreate,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """记录一次负空间事件（绕开话题 / 删句 / 沉默 / 拒绝 / 重定向）。

    当 AI 决定不回答某个问题、删除某句话、保持沉默或拒绝执行操作时，
    调用此端点记录事件，用于后续分析和安全审计。
    """
    event_id = insert_neg_space_event(
        agent_id=body.agent_id,
        conversation_id=body.conversation_id,
        message_id=body.message_id,
        event_category=body.event_category.value,
        event_type=body.event_type,
        trigger_text=body.trigger_text,
        reason=body.reason,
        severity=body.severity.value,
        context_json=body.context_json,
    )

    row = get_neg_space_event_by_id(event_id)
    if row is None:
        raise ApiError(500, "internal_error", "Failed to create neg_space_event")

    item = NegSpaceEventRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# GET /neg-space/events
# ──────────────────────────────────────────────────────────────────────────────


@router.get("/events", response_model=ResponseEnvelope[NegSpaceEventListResponse])
def list_neg_space_events(
    pagination: PaginationParams = Depends(),
    filters: NegSpaceEventFilterParams = Depends(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """列出负空间事件，支持可选过滤。

    Query parameters
    ----------------
    * ``page`` / ``page_size`` — pagination (default 1 / 50, max 200).
    * ``event_category`` — 按事件大类过滤。
    * ``severity`` — 按严重程度过滤。
    * ``agent_id`` — 按 Agent 过滤。
    * ``conversation_id`` — 按会话过滤。
    * ``created_after`` / ``created_before`` — 时间范围过滤。
    """
    rows, total = get_neg_space_events(
        page=pagination.page,
        page_size=pagination.page_size,
        event_category=filters.event_category.value if filters.event_category else None,
        severity=filters.severity.value if filters.severity else None,
        agent_id=filters.agent_id,
        conversation_id=filters.conversation_id,
        created_after=filters.created_after,
        created_before=filters.created_before,
    )

    items = [NegSpaceEventRead(**row) for row in rows]
    total_pages = max(1, math.ceil(total / max(pagination.page_size, 1)))

    page_info = PageInfo(
        page=pagination.page,
        page_size=pagination.page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=pagination.page < total_pages,
        has_previous=pagination.page > 1,
    )

    data = NegSpaceEventListResponse(items=items, page_info=page_info)
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# GET /neg-space/events/{event_id}
# ──────────────────────────────────────────────────────────────────────────────


@router.get("/events/{event_id}", response_model=ResponseEnvelope[NegSpaceEventRead])
def get_neg_space_event_detail(
    event_id: UUID,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """通过主键返回单条负空间事件详情。"""
    row = get_neg_space_event_by_id(event_id)
    if row is None:
        raise ApiError(
            404,
            "bad_request",
            f"neg_space_event '{event_id}' not found",
        )

    item = NegSpaceEventRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# GET /neg-space/summary
# ──────────────────────────────────────────────────────────────────────────────


@router.get("/summary", response_model=ResponseEnvelope[NegSpaceSummary])
def get_neg_space_summary_endpoint(
    agent_id: UUID | None = Query(default=None, description="Filter by agent"),
    conversation_id: UUID | None = Query(default=None, description="Filter by conversation"),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """获取负空间事件的聚合摘要。

    按 agent_id 或 conversation_id 分组统计各类事件数量及严重程度分布。
    两者均为可选；同时指定时优先使用 conversation_id。
    """
    if agent_id is None and conversation_id is None:
        raise ApiError(
            400,
            "bad_request",
            "At least one of 'agent_id' or 'conversation_id' must be provided",
        )

    summary = get_neg_space_summary(
        agent_id=agent_id,
        conversation_id=conversation_id,
    )

    data = NegSpaceSummary(**summary)
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
