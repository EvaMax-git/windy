from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PaginatedData


class AgentCardType(str, Enum):
    identity = "identity"
    soul = "soul"
    tool = "tool"
    user_profile = "user_profile"


class AgentCardStatus(str, Enum):
    active = "active"
    disabled = "disabled"
    archived = "archived"


class ToolItemStatus(str, Enum):
    active = "active"
    disabled = "disabled"
    archived = "archived"


# ── Agent Card ──────────────────────────────────────────────────────────────────

class AgentCardCreateRequest(ApiSchema):
    agent_id: UUID | None = None
    card_type: AgentCardType
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    content_json: dict[str, Any] = Field(default_factory=dict)
    display_order: int = Field(default=0, ge=0)


class AgentCardUpdateRequest(ApiSchema):
    agent_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    content_json: dict[str, Any] | None = None
    status: AgentCardStatus | None = None
    display_order: int | None = Field(default=None, ge=0)


class AgentCardRead(ApiSchema):
    card_id: UUID
    agent_id: UUID | None = None
    card_type: AgentCardType
    name: str
    description: str | None = None
    content_json: dict[str, Any] = Field(default_factory=dict)
    status: AgentCardStatus
    display_order: int = 0
    tool_count: int = 0  # populated by JOIN for tool cards
    created_at: datetime
    updated_at: datetime


class AgentCardListResponse(PaginatedData[AgentCardRead]):
    pass


# ── Agent Tool Item ─────────────────────────────────────────────────────────────

class AgentToolItemCreateRequest(ApiSchema):
    card_id: UUID
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    tool_type: str | None = Field(default=None, max_length=64)
    config_json: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    display_order: int = Field(default=0, ge=0)


class AgentToolItemUpdateRequest(ApiSchema):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    tool_type: str | None = Field(default=None, max_length=64)
    config_json: dict[str, Any] | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    status: ToolItemStatus | None = None
    display_order: int | None = Field(default=None, ge=0)


class AgentToolItemRead(ApiSchema):
    item_id: UUID
    card_id: UUID
    name: str
    description: str | None = None
    tool_type: str | None = None
    config_json: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    status: ToolItemStatus
    display_order: int = 0
    created_at: datetime
    updated_at: datetime


class AgentToolItemListResponse(PaginatedData[AgentToolItemRead]):
    pass


# ── Labels ──

CARD_TYPE_LABELS: dict[str, str] = {
    "identity": "🪪 身份卡",
    "soul": "💫 灵魂卡",
    "tool": "🔧 工具卡",
    "user_profile": "👤 用户画像",
}

TOOL_TYPE_LABELS: dict[str, str] = {
    "api": "API 接口",
    "function": "函数调用",
    "script": "脚本执行",
    "builtin": "内置能力",
    "mcp": "MCP 协议",
}
