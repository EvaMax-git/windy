"""Admin 事件（发件箱）API — 为治理界面列出和查看事件详情。

接口列表
--------
* ``GET /api/v4/admin/events``        – 分页列表，支持过滤
* ``GET /api/v4/admin/events/{id}``   – 单个事件详情（含投递记录）
"""

from __future__ import annotations

import math
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.admin_queries import (
    get_event_by_id,
    get_event_deliveries_for_event,
    get_events,
)
from mneme.schemas import (
    PageInfo,
    PaginationParams,
    ResponseEnvelope,
)
from mneme.schemas.admin import (
    AdminDeliveryRead,
    AdminEventDetailResponse,
    AdminEventFilterParams,
    AdminEventListResponse,
    AdminEventRead,
)

router = APIRouter(prefix="/admin/events", tags=["admin", "outbox"])


@router.get("", response_model=ResponseEnvelope[AdminEventListResponse])
def list_events(
    pagination: PaginationParams = Depends(),
    filters: AdminEventFilterParams = Depends(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """列出事件（发件箱），支持可选过滤。

    查询参数
    --------
    * ``page`` / ``page_size`` – 分页（默认 1 / 50，最大 200）。
    * ``event_type`` – 按事件类型过滤（支持部分 LIKE 匹配）。
    * ``publish_state`` – 按发布状态过滤
      (pending / dispatched / delivered / failed / dead_letter)。
    * ``aggregate_type`` – 按聚合类型过滤。
    * ``occurred_after`` / ``occurred_before`` – 时间区间过滤（ISO-8601）。
    """
    rows, total = get_events(
        page=pagination.page,
        page_size=pagination.page_size,
        event_type=filters.event_type,
        publish_state=filters.publish_state,
        aggregate_type=filters.aggregate_type,
        occurred_after=filters.occurred_after,
        occurred_before=filters.occurred_before,
    )

    items = [AdminEventRead(**row) for row in rows]
    total_pages = max(1, math.ceil(total / max(pagination.page_size, 1)))

    page_info = PageInfo(
        page=pagination.page,
        page_size=pagination.page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=pagination.page < total_pages,
        has_previous=pagination.page > 1,
    )

    data = AdminEventListResponse(items=items, page_info=page_info)
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get(
    "/{event_id}",
    response_model=ResponseEnvelope[AdminEventDetailResponse],
)
def get_event_detail(
    event_id: UUID,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """返回单个事件及其关联的投递记录。"""
    row = get_event_by_id(event_id)
    if row is None:
        raise ApiError(
            404,
            "bad_request",
            f"事件 'event_id' 未找到",
        )

    deliveries = get_event_deliveries_for_event(event_id)

    item = AdminEventDetailResponse(
        deliveries=[AdminDeliveryRead(**d) for d in deliveries],
        **row,
    )
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
