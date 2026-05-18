"""Pydantic schemas for project backend & pipeline configuration."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


# ── Backend ──────────────────────────────────────────────────────────

class ProjectBackendRead(BaseModel):
    id: UUID
    backend_type: str
    enabled: bool
    config_json: dict = Field(default_factory=dict)
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class BackendToggleRequest(BaseModel):
    enabled: bool
    config_json: dict | None = None


# ── Pipeline Rules ───────────────────────────────────────────────────

class PipelineRuleItem(BaseModel):
    pattern: str = Field(..., description="File pattern: '*.ts', 'Dockerfile', '*'")
    pipeline_def_id: UUID
    priority: int = 0


class PipelineRulesUpdateRequest(BaseModel):
    rules: list[PipelineRuleItem]


class ProjectPipelineRuleRead(BaseModel):
    id: UUID
    project_id: UUID
    pattern: str
    pipeline_def_id: UUID
    priority: int
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


# ── Project Health ───────────────────────────────────────────────────

class BackendHealthSummary(BaseModel):
    backend_type: str
    enabled: bool
    docs_ready: int = 0
    docs_stale: int = 0
    docs_failed: int = 0
    docs_disabled: int = 0


class ProjectHealthResponse(BaseModel):
    project_id: UUID
    backends: list[BackendHealthSummary]
    overall: str  # 'healthy' | 'degraded' | 'attention'


# ── Tree ─────────────────────────────────────────────────────────────

class TreeNode(BaseModel):
    name: str
    type: str  # 'folder' | 'file'
    path: str = ""
    document_id: UUID | None = None
    lang: str | None = None
    version: int | None = None
    children: list[TreeNode] | None = None

    model_config = {"from_attributes": True}


class TreeResponse(BaseModel):
    project_id: UUID
    tree: list[TreeNode]


# ── Document Content ─────────────────────────────────────────────────

class DocumentContentResponse(BaseModel):
    document_id: UUID
    title: str
    lang: str = "markdown"
    folder_path: str | None = None
    content_markdown: str
    content_hash: str | None = None
    current_version: int
    project_id: UUID
    source_asset_id: UUID | None = None
    pipeline_def_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ContentUpdateRequest(BaseModel):
    content_markdown: str
    affected_block_ids: list[UUID] | None = None


# ── Document create ──────────────────────────────────────────────────

class DocumentCreateRequest(BaseModel):
    project_id: UUID
    title: str
    content_markdown: str | None = None
    folder_path: str | None = None
    lang: str = "markdown"


# ── Document move ────────────────────────────────────────────────────

class DocumentMoveRequest(BaseModel):
    target_project_id: UUID
    target_folder: str | None = None


# ── Index states ─────────────────────────────────────────────────────

class IndexStateItem(BaseModel):
    backend_type: str
    state: str
    indexed_version: int = 0
    target_version: int = 0
    last_error: str | None = None
    error_count: int = 0
    built_at: datetime | None = None


class IndexStatesResponse(BaseModel):
    document_id: UUID
    backends: list[IndexStateItem]
