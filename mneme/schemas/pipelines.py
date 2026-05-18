"""Pydantic schemas for Pipeline definitions and runs.

P3-04 Pipeline 骨架 — pipeline_defs + pipeline_runs CRUD schemas.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema


# ═══════════════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════════════


class PipelineType(str, Enum):
    """Mirrors pipeline_defs.pipeline_type CHECK constraint."""

    asset_import = "asset_import"
    knowledge_index = "knowledge_index"
    memory_extract = "memory_extract"
    backup = "backup"
    restore = "restore"
    importer = "importer"
    maintenance = "maintenance"


class PipelineDefStatus(str, Enum):
    """Mirrors pipeline_defs.status CHECK constraint."""

    draft = "draft"
    active = "active"
    disabled = "disabled"
    archived = "archived"


class PipelineRunStatus(str, Enum):
    """Mirrors pipeline_runs.status CHECK constraint."""

    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    superseded = "superseded"


class PipelineTriggerType(str, Enum):
    """Mirrors pipeline_runs.trigger_type CHECK constraint."""

    manual = "manual"
    event = "event"
    schedule = "schedule"
    api = "api"
    importer = "importer"
    system = "system"


class PipelineTargetType(str, Enum):
    """Mirrors pipeline_runs.target_type CHECK constraint."""

    asset = "asset"
    document = "document"
    memory = "memory"
    project = "project"
    backup = "backup"
    import_run = "import_run"


# ═══════════════════════════════════════════════════════════════════
# Pipeline Step config (embedded in config_json)
# ═══════════════════════════════════════════════════════════════════


class PipelineStepConfig(ApiSchema):
    """A single step definition within a pipeline config."""

    step_code: str = Field(description="Unique step identifier within the pipeline.")
    handler: str = Field(description="Fully qualified handler function name.")
    timeout_seconds: int = Field(default=300, ge=1, description="Max allowed duration for this step.")
    retry_on_failure: bool = Field(default=True, description="Whether to retry this step on failure.")
    depends_on: list[str] = Field(default_factory=list, description="List of step_codes that must complete before this step.")


# ═══════════════════════════════════════════════════════════════════
# Pipeline Def
# ═══════════════════════════════════════════════════════════════════


class PipelineDefRead(ApiSchema):
    """Read representation of a pipeline definition."""

    pipeline_def_id: UUID = Field(description="Primary key.")
    project_id: UUID | None = Field(default=None, description="Optional project scope.")
    pipeline_code: str = Field(description="Unique code for this pipeline (max 80 chars).")
    pipeline_type: PipelineType = Field(description="Pipeline type category.")
    version: int = Field(default=1, description="Monotonically increasing version number.")
    name: str = Field(description="Human-readable name (max 160 chars).")
    description: str | None = Field(default=None, description="Optional description.")
    config_json: dict[str, Any] = Field(default_factory=dict, description="Pipeline steps configuration.")
    status: PipelineDefStatus = Field(default=PipelineDefStatus.active, description="Lifecycle status.")
    created_by_user_id: UUID | None = Field(default=None)
    created_at: datetime
    updated_at: datetime


class PipelineDefCreateRequest(ApiSchema):
    """Payload to create a pipeline definition."""

    project_id: UUID | None = Field(default=None, description="Optional project scope.")
    pipeline_code: str = Field(min_length=1, max_length=80, description="Unique code for this pipeline.")
    pipeline_type: PipelineType = Field(description="Pipeline type category.")
    name: str = Field(min_length=1, max_length=160, description="Human-readable name.")
    description: str | None = Field(default=None, max_length=2000, description="Optional description.")
    config_json: dict[str, Any] | None = Field(
        default=None,
        description="Pipeline steps configuration. If None, defaults are used based on pipeline_type.",
    )
    status: PipelineDefStatus = Field(default=PipelineDefStatus.active, description="Initial lifecycle status.")


class PipelineDefUpdateRequest(ApiSchema):
    """Payload to update a pipeline definition. Only non-None fields are applied."""

    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    config_json: dict[str, Any] | None = None
    status: PipelineDefStatus | None = None


# ═══════════════════════════════════════════════════════════════════
# Pipeline Run
# ═══════════════════════════════════════════════════════════════════


class PipelineRunRead(ApiSchema):
    """Read representation of a pipeline run."""

    pipeline_run_id: UUID = Field(description="Primary key.")
    pipeline_def_id: UUID = Field(description="FK to pipeline_defs.")
    project_id: UUID | None = Field(default=None, description="Optional project scope.")
    root_job_id: UUID | None = Field(default=None, description="FK to jobs.")
    trigger_type: PipelineTriggerType = Field(description="How this run was triggered.")
    trigger_event_id: UUID | None = Field(default=None, description="FK to events.")
    target_type: PipelineTargetType | None = Field(default=None, description="Polymorphic target type.")
    target_id: UUID | None = Field(default=None, description="Polymorphic target ID.")
    target_version: int | None = Field(default=None, description="Version snapshot at trigger time.")
    status: PipelineRunStatus = Field(default=PipelineRunStatus.pending, description="Current run status.")
    started_at: datetime | None = Field(default=None)
    finished_at: datetime | None = Field(default=None)
    input_json: dict[str, Any] = Field(default_factory=dict, description="Trigger parameters.")
    output_json: dict[str, Any] = Field(default_factory=dict, description="Result summary.")
    error_json: dict[str, Any] = Field(default_factory=dict, description="Error details.")
    idempotency_key: str = Field(description="Unique idempotency key for this run.")
    created_at: datetime
    updated_at: datetime


class PipelineRunDetail(PipelineRunRead):
    """Extended pipeline run with associated job information."""

    root_job: dict[str, Any] | None = Field(default=None, description="The associated root job details.")
    pipeline_def: PipelineDefRead | None = Field(default=None, description="The pipeline definition.")


class PipelineRunCreateRequest(ApiSchema):
    """Payload to manually trigger a pipeline run."""

    pipeline_def_id: UUID = Field(description="Pipeline definition to execute.")
    project_id: UUID | None = Field(default=None, description="Optional project scope.")
    trigger_type: PipelineTriggerType = Field(default=PipelineTriggerType.manual, description="How this run is triggered.")
    target_type: PipelineTargetType | None = Field(default=None, description="Polymorphic target type.")
    target_id: UUID | None = Field(default=None, description="Polymorphic target ID.")
    target_version: int | None = Field(default=None, description="Target version snapshot.")
    input_json: dict[str, Any] | None = Field(
        default=None, description="Trigger parameters (e.g. asset_id, chunking_strategy)."
    )
    trigger_event_id: UUID | None = Field(default=None, description="FK to events if triggered by event.")


# ═══════════════════════════════════════════════════════════════════
# Default pipeline definitions
# ═══════════════════════════════════════════════════════════════════


DEFAULT_ASSET_IMPORT_PIPELINE_CONFIG: dict[str, Any] = {
    "steps": [
        {
            "step_code": "validate_hash",
            "handler": "pipeline.step.validate_hash",
            "timeout_seconds": 60,
            "retry_on_failure": True,
            "depends_on": [],
        },
        {
            "step_code": "extract_metadata",
            "handler": "pipeline.step.extract_metadata",
            "timeout_seconds": 120,
            "retry_on_failure": True,
            "depends_on": ["validate_hash"],
        },
        {
            "step_code": "write_metadata",
            "handler": "pipeline.step.write_metadata",
            "timeout_seconds": 60,
            "retry_on_failure": True,
            "depends_on": ["extract_metadata"],
        },
        {
            "step_code": "update_ingest_state",
            "handler": "pipeline.step.update_ingest_state",
            "timeout_seconds": 30,
            "retry_on_failure": True,
            "depends_on": ["write_metadata"],
        },
        {
            "step_code": "trigger_knowledge_index",
            "handler": "pipeline.step.trigger_knowledge_index",
            "timeout_seconds": 30,
            "retry_on_failure": False,
            "depends_on": ["update_ingest_state"],
        },
    ]
}
