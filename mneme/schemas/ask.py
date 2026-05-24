"""Schemas for the /api/v4/ask endpoint (A3: AI 回答 MVP)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


SensitivityLevel = Literal["public", "normal", "private", "sensitive", "secret"]


class AskRequest(BaseModel):
    """Request body for POST /api/v4/ask."""
    question: str = Field(..., min_length=1, max_length=2000, description="用户问题")
    project_id: UUID | None = Field(None, description="限定项目范围")
    max_citations: int = Field(5, ge=1, le=20, description="最大引用数")
    sensitivity_floor: SensitivityLevel | None = Field(
        None,
        description="最低敏感度",
    )


class AskCitation(BaseModel):
    """A single citation from search results."""
    chunk_id: str
    document_title: str
    snippet: str
    rank: float


class AskResponse(BaseModel):
    """Response body for POST /api/v4/ask."""
    answer: str = Field(..., description="AI 生成的回答")
    citations: list[AskCitation] = Field(default_factory=list, description="引用来源")
    context_token_count: int = Field(0, description="上下文 token 数")
    model: str | None = Field(None, description="使用的模型")
    degraded: bool = Field(False, description="是否降级（未调用 AI）")
    degradation_reason: str | None = Field(None, description="降级原因")
