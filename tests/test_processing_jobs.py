"""Contract tests for Processing Jobs DAL.

Tests the ``mneme.db.processing_jobs`` module covering:

* CRUD: create / get / get_status / list
* Status transitions: queued→processing→done, queued→processing→failed
* Invalid transitions are rejected
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from mneme.db.processing_jobs import (
    create_processing_job,
    get_processing_job,
    get_processing_job_status,
    list_processing_jobs,
    advance_job_status,
)
from mneme.db.pipeline_registry import create_pipeline_registry
from mneme.schemas.processing_jobs import ProcessingJobCreateRequest
from mneme.schemas.pipeline_registry import PipelineRegistryCreateRequest


# ── Helpers ─────────────────────────────────────────────────────────────────


def _ensure_pipeline(db) -> uuid.UUID:
    """Ensure a pipeline_registry entry exists, return its ID."""
    payload = PipelineRegistryCreateRequest(
        name="Test Pipeline",
        input_formats=["text/plain"],
        processor_module=f"test_pipe_{uuid.uuid4().hex[:8]}",
        accept_chunk_types=["text"],
        target_stores=["knowledge_store"],
    )
    created = create_pipeline_registry(db, payload)
    return created.id


def _ensure_asset(db, project_id: uuid.UUID | None = None) -> uuid.UUID:
    """Insert a minimal asset record and return its ID."""
    asset_id = uuid.uuid4()
    db.execute(text("""
        INSERT INTO assets (asset_id, project_id, asset_uid, title, asset_type,
                            storage_ref, content_hash, ingest_state)
        VALUES (:aid, :pid, :uid, :title, 'document', 'test_ref', :hash, 'staged')
        ON CONFLICT DO NOTHING
    """), {
        "aid": asset_id,
        "pid": project_id,
        "uid": f"test-asset-{asset_id.hex[:8]}",
        "title": f"Test Asset {asset_id.hex[:6]}",
        "hash": asset_id.hex,
    })
    db.flush()
    return asset_id


def _ensure_project(db) -> uuid.UUID:
    """Insert a minimal project and return its ID."""
    pid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO projects (project_id, project_code, name, status, sensitivity_default)
        VALUES (:pid, :code, :name, 'active', 'normal')
        ON CONFLICT (project_code) DO NOTHING
    """), {
        "pid": pid,
        "code": f"PJ-{pid.hex[:8].upper()}",
        "name": f"Test Project {pid.hex[:6]}",
    })
    db.flush()
    return pid


def _make_payload(asset_id: uuid.UUID, pipeline_id: uuid.UUID) -> ProcessingJobCreateRequest:
    return ProcessingJobCreateRequest(
        asset_id=asset_id,
        pipeline_id=pipeline_id,
        target_stores=["vector", "fts"],
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Processing Jobs CRUD
# ═══════════════════════════════════════════════════════════════════════════════


class TestProcessingJobsCRUD:

    def test_create_basic(self, db):
        pid = _ensure_project(db)
        asset_id = _ensure_asset(db, pid)
        pipeline_id = _ensure_pipeline(db)

        payload = _make_payload(asset_id, pipeline_id)
        job = create_processing_job(db, payload=payload)

        assert job.id is not None
        assert job.asset_id == asset_id
        assert job.pipeline_id == pipeline_id
        assert job.status == "queued"
        assert job.target_stores == ["vector", "fts"]
        assert job.chunks_produced == 0
        assert job.error is None
        assert job.created_at is not None

    def test_get_by_id(self, db):
        pid = _ensure_project(db)
        asset_id = _ensure_asset(db, pid)
        pipeline_id = _ensure_pipeline(db)

        created = create_processing_job(db, payload=_make_payload(asset_id, pipeline_id))
        fetched = get_processing_job(db, created.id)

        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.status == "queued"

    def test_get_not_found(self, db):
        assert get_processing_job(db, uuid.uuid4()) is None

    def test_get_status(self, db):
        pid = _ensure_project(db)
        asset_id = _ensure_asset(db, pid)
        pipeline_id = _ensure_pipeline(db)

        created = create_processing_job(db, payload=_make_payload(asset_id, pipeline_id))
        status = get_processing_job_status(db, created.id)

        assert status is not None
        assert status.job_id == created.id
        assert status.status == "queued"
        assert status.asset_id == asset_id

    def test_get_status_not_found(self, db):
        assert get_processing_job_status(db, uuid.uuid4()) is None

    def test_list_all(self, db):
        pid = _ensure_project(db)
        pipeline_id = _ensure_pipeline(db)

        for i in range(3):
            asset_id = _ensure_asset(db, pid)
            create_processing_job(db, payload=_make_payload(asset_id, pipeline_id))

        items, total = list_processing_jobs(db, page=1, page_size=50)
        assert len(items) >= 3
        assert total >= 3

    def test_list_by_asset(self, db):
        pid = _ensure_project(db)
        pipeline_id = _ensure_pipeline(db)
        asset_id = _ensure_asset(db, pid)

        create_processing_job(db, payload=_make_payload(asset_id, pipeline_id))
        create_processing_job(db, payload=_make_payload(asset_id, pipeline_id))

        items, total = list_processing_jobs(db, asset_id=asset_id)
        assert len(items) == 2
        assert all(i.asset_id == asset_id for i in items)


# ═══════════════════════════════════════════════════════════════════════════════
# Status transitions
# ═══════════════════════════════════════════════════════════════════════════════


class TestStatusTransitions:

    def test_queued_to_processing(self, db):
        pid = _ensure_project(db)
        asset_id = _ensure_asset(db, pid)
        pipeline_id = _ensure_pipeline(db)

        job = create_processing_job(db, payload=_make_payload(asset_id, pipeline_id))
        assert job.status == "queued"

        advanced = advance_job_status(
            db, job_id=job.id, new_status="processing", expected_status="queued",
        )
        assert advanced.status == "processing"
        assert advanced.started_at is not None

    def test_processing_to_done(self, db):
        pid = _ensure_project(db)
        asset_id = _ensure_asset(db, pid)
        pipeline_id = _ensure_pipeline(db)

        job = create_processing_job(db, payload=_make_payload(asset_id, pipeline_id))
        advance_job_status(db, job_id=job.id, new_status="processing", expected_status="queued")

        advanced = advance_job_status(
            db, job_id=job.id, new_status="done", expected_status="processing",
            chunks_produced=42,
        )
        assert advanced.status == "done"
        assert advanced.chunks_produced == 42
        assert advanced.completed_at is not None

    def test_processing_to_failed(self, db):
        pid = _ensure_project(db)
        asset_id = _ensure_asset(db, pid)
        pipeline_id = _ensure_pipeline(db)

        job = create_processing_job(db, payload=_make_payload(asset_id, pipeline_id))
        advance_job_status(db, job_id=job.id, new_status="processing", expected_status="queued")

        advanced = advance_job_status(
            db, job_id=job.id, new_status="failed", expected_status="processing",
            error="Something broke",
        )
        assert advanced.status == "failed"
        assert advanced.error == "Something broke"
        assert advanced.completed_at is not None

    def test_invalid_transition_rejected(self, db):
        pid = _ensure_project(db)
        asset_id = _ensure_asset(db, pid)
        pipeline_id = _ensure_pipeline(db)

        job = create_processing_job(db, payload=_make_payload(asset_id, pipeline_id))
        # queued → done is invalid (must go through processing)
        with pytest.raises(ValueError, match="Cannot transition"):
            advance_job_status(
                db, job_id=job.id, new_status="done", expected_status="queued",
            )

    def test_done_is_terminal(self, db):
        pid = _ensure_project(db)
        asset_id = _ensure_asset(db, pid)
        pipeline_id = _ensure_pipeline(db)

        job = create_processing_job(db, payload=_make_payload(asset_id, pipeline_id))
        advance_job_status(db, job_id=job.id, new_status="processing", expected_status="queued")
        advance_job_status(db, job_id=job.id, new_status="done", expected_status="processing")

        # done → anything should fail
        with pytest.raises(ValueError, match="Cannot transition"):
            advance_job_status(
                db, job_id=job.id, new_status="processing", expected_status="done",
            )

    def test_concurrent_transition_fails(self, db):
        pid = _ensure_project(db)
        asset_id = _ensure_asset(db, pid)
        pipeline_id = _ensure_pipeline(db)

        job = create_processing_job(db, payload=_make_payload(asset_id, pipeline_id))
        advance_job_status(db, job_id=job.id, new_status="processing", expected_status="queued")

        # Another attempt with wrong expected_status should fail
        with pytest.raises(ValueError):
            advance_job_status(
                db, job_id=job.id, new_status="done", expected_status="queued",
            )
