"""Contract tests for Pipeline DB layer (P3-04).

Tests the ``mneme.db.pipelines`` module covering:

* PipelineDef CRUD: create / get / get_by_code / list with filters + pagination
* PipelineRun CRUD: create / get / list with filters + pagination
* Idempotency: duplicate create returns same result
* State machine: advance_run_status with valid/invalid transitions
* Optimistic concurrency: expected_status mismatch detection
"""

from __future__ import annotations

import uuid

import pytest

from mneme.api.context import ActorContext, RequestContext
from mneme.db.pipelines import (
    _can_transition_run,
    advance_run_status,
    create_pipeline_def,
    create_pipeline_run,
    get_pipeline_def,
    get_pipeline_def_by_code,
    get_pipeline_run,
    list_pipeline_defs,
    list_pipeline_runs,
)
from mneme.schemas.pipelines import PipelineDefStatus, PipelineRunStatus
from tests.conftest import TEST_USER_ID


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_context(idem_key: str | None = None) -> RequestContext:
    """Build a minimal RequestContext for tests."""
    req_id = uuid.uuid4()
    if idem_key is None:
        idem_key = str(uuid.uuid4())
    return RequestContext(
        request_id=req_id,
        correlation_id=req_id,
        actor=ActorContext(actor_type="user", actor_id=TEST_USER_ID),
        idempotency_key=idem_key,
    )


def _create_test_def(db, *, code: str | None = None, **kwargs):
    """Create a pipeline definition with unique code and return it."""
    if code is None:
        code = f"p{uuid.uuid4().hex[:12]}"
    ctx = _make_context()
    return create_pipeline_def(
        db,
        ctx,
        pipeline_code=code,
        pipeline_type=kwargs.pop("pipeline_type", "asset_import"),
        name=kwargs.pop("name", f"Pipeline {code[:8]}"),
        **kwargs,
    )


def _create_test_run(db, *, pipeline_def_id, idem_key=None, **kwargs):
    """Create a pipeline run and return it."""
    ctx = _make_context(idem_key=idem_key)
    return create_pipeline_run(
        db,
        ctx,
        pipeline_def_id=pipeline_def_id,
        **kwargs,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# State machine unit tests (no DB required)
# ═══════════════════════════════════════════════════════════════════════════════


class TestStateMachineTransitions:

    def test_pending_to_running(self):
        assert _can_transition_run("pending", "running") is True

    def test_pending_to_cancelled(self):
        assert _can_transition_run("pending", "cancelled") is True

    def test_running_to_succeeded(self):
        assert _can_transition_run("running", "succeeded") is True

    def test_running_to_failed(self):
        assert _can_transition_run("running", "failed") is True

    def test_running_to_cancelled(self):
        assert _can_transition_run("running", "cancelled") is True

    def test_failed_to_pending_retry(self):
        assert _can_transition_run("failed", "pending") is True

    def test_succeeded_terminal(self):
        assert _can_transition_run("succeeded", "running") is False
        assert _can_transition_run("succeeded", "failed") is False
        assert _can_transition_run("succeeded", "cancelled") is False

    def test_cancelled_terminal(self):
        assert _can_transition_run("cancelled", "running") is False
        assert _can_transition_run("cancelled", "pending") is False

    def test_superseded_terminal(self):
        assert _can_transition_run("superseded", "running") is False
        assert _can_transition_run("superseded", "pending") is False

    def test_invalid_reverse_transitions(self):
        assert _can_transition_run("running", "pending") is False
        assert _can_transition_run("succeeded", "pending") is False
        assert _can_transition_run("failed", "running") is False

    def test_unknown_transition(self):
        assert _can_transition_run("nonexistent", "running") is False
        assert _can_transition_run("pending", "nonexistent") is False


# ═══════════════════════════════════════════════════════════════════════════════
# PipelineDef CRUD
# ═══════════════════════════════════════════════════════════════════════════════


class TestPipelineDefCRUD:

    def test_create_basic(self, db):
        result = _create_test_def(db, code="import-v1")
        assert result.pipeline_def_id is not None
        assert result.pipeline_code == "import-v1"
        assert result.pipeline_type.value == "asset_import"
        assert result.version == 1
        assert result.status.value == "active"
        assert result.created_at is not None
        assert result.updated_at is not None

    def test_create_default_config_for_asset_import(self, db):
        result = _create_test_def(db, code="auto-cfg", pipeline_type="asset_import")
        steps = result.config_json.get("steps", [])
        step_codes = [s["step_code"] for s in steps]
        assert len(steps) == 5
        assert "validate_hash" in step_codes
        assert "extract_metadata" in step_codes
        assert "write_metadata" in step_codes
        assert "update_ingest_state" in step_codes
        assert "trigger_knowledge_index" in step_codes

    def test_create_custom_config(self, db):
        custom = {"steps": [{"step_code": "my_step", "handler": "custom", "timeout_seconds": 10}]}
        result = _create_test_def(db, code="custom-cfg", config_json=custom)
        assert result.config_json["steps"][0]["step_code"] == "my_step"

    def test_create_duplicate_code_version(self, db):
        """Same (project_id, pipeline_code, version) triggers IntegrityError."""
        pid = uuid.uuid4()
        code = f"dup-{uuid.uuid4().hex[:8]}"
        ctx = _make_context()
        create_pipeline_def(db, ctx, pipeline_code=code, pipeline_type="backup",
                            name="Dup", project_id=pid)
        ctx2 = _make_context()
        with pytest.raises(Exception):  # IntegrityError from UNIQUE constraint
            create_pipeline_def(db, ctx2, pipeline_code=code, pipeline_type="backup",
                                name="Dup2", project_id=pid)

    def test_get_by_id(self, db):
        created = _create_test_def(db, code="get-me")
        fetched = get_pipeline_def(db, created.pipeline_def_id)
        assert fetched is not None
        assert fetched.pipeline_def_id == created.pipeline_def_id
        assert fetched.pipeline_code == "get-me"

    def test_get_not_found(self, db):
        assert get_pipeline_def(db, uuid.uuid4()) is None

    def test_get_by_code(self, db):
        _create_test_def(db, code="by-code-1")
        _create_test_def(db, code="by-code-1")
        fetched = get_pipeline_def_by_code(db, pipeline_code="by-code-1")
        assert fetched is not None
        assert fetched.pipeline_code == "by-code-1"
        assert fetched.version >= 1

    def test_get_by_code_not_found(self, db):
        assert get_pipeline_def_by_code(db, pipeline_code="no-such") is None

    def test_list_all(self, db):
        _create_test_def(db, code="list-a")
        _create_test_def(db, code="list-b")
        _create_test_def(db, code="list-c")
        items, total = list_pipeline_defs(db)
        assert total >= 3
        assert len(items) >= 3

    def test_list_filter_by_type(self, db):
        _create_test_def(db, code="type-imp", pipeline_type="asset_import")
        _create_test_def(db, code="type-bkp", pipeline_type="backup")
        items, total = list_pipeline_defs(db, pipeline_type="backup")
        assert total >= 1
        for item in items:
            assert item.pipeline_type.value == "backup"

    def test_list_filter_by_status(self, db):
        ctx = _make_context()
        create_pipeline_def(db, ctx, pipeline_code="st-draft", pipeline_type="backup",
                            name="Draft Def", status="draft")
        create_pipeline_def(db, ctx, pipeline_code="st-active", pipeline_type="backup",
                            name="Active Def", status="active")
        items, total = list_pipeline_defs(db, status="draft")
        assert total >= 1
        for item in items:
            assert item.status.value == "draft"

    def test_list_pagination(self, db):
        for i in range(5):
            _create_test_def(db, code=f"page-{i}")
        items, total = list_pipeline_defs(db, page=1, page_size=2)
        assert len(items) <= 2
        assert total >= 5

    def test_create_idempotent(self, db):
        idem_key = f"idem-{uuid.uuid4().hex}"
        code = f"idem-code-{uuid.uuid4().hex[:8]}"

        ctx1 = _make_context(idem_key=idem_key)
        result1 = create_pipeline_def(db, ctx1, pipeline_code=code,
                                      pipeline_type="backup", name="Idem Def")

        ctx2 = _make_context(idem_key=idem_key)
        result2 = create_pipeline_def(db, ctx2, pipeline_code="different-code",
                                      pipeline_type="importer", name="Ignored")

        assert result2.pipeline_def_id == result1.pipeline_def_id
        assert result2.pipeline_code == code
        assert result2.name == "Idem Def"

    def test_create_with_project(self, db):
        project_id = uuid.uuid4()
        result = _create_test_def(db, code="proj-def", project_id=project_id)
        assert result.project_id == project_id

    def test_list_filter_by_project(self, db):
        pid_a = uuid.uuid4()
        pid_b = uuid.uuid4()
        _create_test_def(db, code="proj-a-def", project_id=pid_a)
        _create_test_def(db, code="proj-b-def", project_id=pid_b)
        items, _ = list_pipeline_defs(db, project_id=pid_a)
        for item in items:
            assert item.project_id == pid_a


# ═══════════════════════════════════════════════════════════════════════════════
# PipelineRun CRUD
# ═══════════════════════════════════════════════════════════════════════════════


class TestPipelineRunCRUD:

    def test_create_basic(self, db):
        pdef = _create_test_def(db, code="run-def")
        run = _create_test_run(db, pipeline_def_id=pdef.pipeline_def_id)
        assert run.pipeline_run_id is not None
        assert run.pipeline_def_id == pdef.pipeline_def_id
        assert run.status.value == "pending"
        assert run.trigger_type.value == "manual"

    def test_create_with_target(self, db):
        pdef = _create_test_def(db, code="target-def")
        target_id = uuid.uuid4()
        run = _create_test_run(
            db, pipeline_def_id=pdef.pipeline_def_id,
            target_type="asset", target_id=target_id, target_version=3,
            input_json={"asset_id": str(target_id), "strategy": "fast"},
        )
        assert run.target_type.value == "asset"
        assert run.target_id == target_id
        assert run.target_version == 3
        assert run.input_json.get("strategy") == "fast"

    def test_create_with_nonexistent_def(self, db):
        ctx = _make_context()
        with pytest.raises(ValueError, match="not found"):
            create_pipeline_run(db, ctx, pipeline_def_id=uuid.uuid4())

    def test_create_against_disabled_def(self, db):
        ctx = _make_context()
        pdef = create_pipeline_def(
            db, ctx, pipeline_code=f"dis-{uuid.uuid4().hex[:8]}",
            pipeline_type="backup", name="Disabled", status="disabled",
        )
        ctx2 = _make_context()
        with pytest.raises(ValueError, match="disabled"):
            create_pipeline_run(db, ctx2, pipeline_def_id=pdef.pipeline_def_id)

    def test_create_against_archived_def(self, db):
        ctx = _make_context()
        pdef = create_pipeline_def(
            db, ctx, pipeline_code=f"arc-{uuid.uuid4().hex[:8]}",
            pipeline_type="backup", name="Archived", status="archived",
        )
        ctx2 = _make_context()
        with pytest.raises(ValueError):
            create_pipeline_run(db, ctx2, pipeline_def_id=pdef.pipeline_def_id)

    def test_get_by_id(self, db):
        pdef = _create_test_def(db, code="get-run-def")
        created = _create_test_run(db, pipeline_def_id=pdef.pipeline_def_id)
        fetched = get_pipeline_run(db, created.pipeline_run_id)
        assert fetched is not None
        assert fetched.pipeline_run_id == created.pipeline_run_id
        assert fetched.status.value == "pending"

    def test_get_not_found(self, db):
        assert get_pipeline_run(db, uuid.uuid4()) is None

    def test_list_all(self, db):
        pdef = _create_test_def(db, code="list-runs")
        _create_test_run(db, pipeline_def_id=pdef.pipeline_def_id)
        _create_test_run(db, pipeline_def_id=pdef.pipeline_def_id)
        items, total = list_pipeline_runs(db)
        assert total >= 2

    def test_list_filter_by_status(self, db):
        pdef = _create_test_def(db, code="fs-def")
        _create_test_run(db, pipeline_def_id=pdef.pipeline_def_id)
        items, total = list_pipeline_runs(db, status="pending")
        assert total >= 1
        for item in items:
            assert item.status.value == "pending"

    def test_list_filter_by_trigger(self, db):
        pdef = _create_test_def(db, code="trig-def")
        _create_test_run(db, pipeline_def_id=pdef.pipeline_def_id)
        items, total = list_pipeline_runs(db, trigger_type="manual")
        assert total >= 1

    def test_list_filter_by_def(self, db):
        pdef_a = _create_test_def(db, code="def-a")
        pdef_b = _create_test_def(db, code="def-b")
        _create_test_run(db, pipeline_def_id=pdef_a.pipeline_def_id)
        _create_test_run(db, pipeline_def_id=pdef_b.pipeline_def_id)
        items, _ = list_pipeline_runs(db, pipeline_def_id=pdef_a.pipeline_def_id)
        for item in items:
            assert item.pipeline_def_id == pdef_a.pipeline_def_id

    def test_list_pagination(self, db):
        pdef = _create_test_def(db, code="pg-runs")
        for _ in range(5):
            _create_test_run(db, pipeline_def_id=pdef.pipeline_def_id)
        items, total = list_pipeline_runs(db, page=1, page_size=2)
        assert len(items) <= 2
        assert total >= 5

    def test_create_idempotent(self, db):
        pdef = _create_test_def(db, code="idem-run-def")
        idem_key = f"run-idem-{uuid.uuid4().hex}"

        ctx1 = _make_context(idem_key=idem_key)
        run1 = create_pipeline_run(db, ctx1, pipeline_def_id=pdef.pipeline_def_id,
                                   input_json={"note": "original"})

        ctx2 = _make_context(idem_key=idem_key)
        run2 = create_pipeline_run(db, ctx2, pipeline_def_id=pdef.pipeline_def_id,
                                   input_json={"note": "ignored"})

        assert run2.pipeline_run_id == run1.pipeline_run_id
        assert run2.input_json.get("note") == "original"


# ═══════════════════════════════════════════════════════════════════════════════
# State machine integration tests (DB required)
# ═══════════════════════════════════════════════════════════════════════════════


class TestAdvanceRunStatus:

    def test_pending_to_running(self, db):
        pdef = _create_test_def(db, code="sm-p2r")
        run = _create_test_run(db, pipeline_def_id=pdef.pipeline_def_id)
        ctx = _make_context()
        updated = advance_run_status(db, ctx, pipeline_run_id=run.pipeline_run_id,
                                     new_status="running", expected_status="pending")
        assert updated.status.value == "running"
        assert updated.started_at is not None

    def test_running_to_succeeded(self, db):
        pdef = _create_test_def(db, code="sm-r2s")
        run = _create_test_run(db, pipeline_def_id=pdef.pipeline_def_id)
        advance_run_status(db, _make_context(), pipeline_run_id=run.pipeline_run_id,
                           new_status="running", expected_status="pending")
        output = {"steps_completed": 5, "asset_id": str(uuid.uuid4())}
        updated = advance_run_status(db, _make_context(),
                                     pipeline_run_id=run.pipeline_run_id,
                                     new_status="succeeded",
                                     expected_status="running",
                                     output_json=output)
        assert updated.status.value == "succeeded"
        assert updated.finished_at is not None
        assert updated.output_json.get("steps_completed") == 5

    def test_running_to_failed(self, db):
        pdef = _create_test_def(db, code="sm-r2f")
        run = _create_test_run(db, pipeline_def_id=pdef.pipeline_def_id)
        advance_run_status(db, _make_context(), pipeline_run_id=run.pipeline_run_id,
                           new_status="running", expected_status="pending")
        error = {"error_code": "STEP_FAILED", "message": "Disk full"}
        updated = advance_run_status(db, _make_context(),
                                     pipeline_run_id=run.pipeline_run_id,
                                     new_status="failed", expected_status="running",
                                     error_json=error)
        assert updated.status.value == "failed"
        assert updated.finished_at is not None
        assert updated.error_json.get("error_code") == "STEP_FAILED"

    def test_pending_to_cancelled(self, db):
        pdef = _create_test_def(db, code="sm-p2c")
        run = _create_test_run(db, pipeline_def_id=pdef.pipeline_def_id)
        updated = advance_run_status(db, _make_context(),
                                     pipeline_run_id=run.pipeline_run_id,
                                     new_status="cancelled", expected_status="pending")
        assert updated.status.value == "cancelled"
        assert updated.finished_at is not None

    def test_failed_to_pending_retry(self, db):
        pdef = _create_test_def(db, code="sm-f2p")
        run = _create_test_run(db, pipeline_def_id=pdef.pipeline_def_id)
        advance_run_status(db, _make_context(), pipeline_run_id=run.pipeline_run_id,
                           new_status="running", expected_status="pending")
        advance_run_status(db, _make_context(), pipeline_run_id=run.pipeline_run_id,
                           new_status="failed", expected_status="running")
        updated = advance_run_status(db, _make_context(),
                                     pipeline_run_id=run.pipeline_run_id,
                                     new_status="pending", expected_status="failed")
        assert updated.status.value == "pending"

    def test_succeeded_is_terminal(self, db):
        pdef = _create_test_def(db, code="sm-term")
        run = _create_test_run(db, pipeline_def_id=pdef.pipeline_def_id)
        advance_run_status(db, _make_context(), pipeline_run_id=run.pipeline_run_id,
                           new_status="running", expected_status="pending")
        advance_run_status(db, _make_context(), pipeline_run_id=run.pipeline_run_id,
                           new_status="succeeded", expected_status="running")
        with pytest.raises(ValueError, match="Invalid run status transition"):
            advance_run_status(db, _make_context(),
                               pipeline_run_id=run.pipeline_run_id,
                               new_status="running", expected_status="succeeded")

    def test_expected_status_mismatch(self, db):
        pdef = _create_test_def(db, code="sm-opt")
        run = _create_test_run(db, pipeline_def_id=pdef.pipeline_def_id)
        with pytest.raises(ValueError, match="Expected status"):
            advance_run_status(db, _make_context(),
                               pipeline_run_id=run.pipeline_run_id,
                               new_status="running",
                               expected_status="running")  # actual is pending

    def test_nonexistent_run(self, db):
        with pytest.raises(ValueError, match="not found"):
            advance_run_status(db, _make_context(),
                               pipeline_run_id=uuid.uuid4(),
                               new_status="running", expected_status="pending")

    def test_full_retry_cycle(self, db):
        """failed → pending → running → succeeded (full retry)."""
        pdef = _create_test_def(db, code="sm-cycle")
        run = _create_test_run(db, pipeline_def_id=pdef.pipeline_def_id)

        # pending → running → failed
        advance_run_status(db, _make_context(), pipeline_run_id=run.pipeline_run_id,
                           new_status="running", expected_status="pending")
        advance_run_status(db, _make_context(), pipeline_run_id=run.pipeline_run_id,
                           new_status="failed", expected_status="running")
        assert get_pipeline_run(db, run.pipeline_run_id).status.value == "failed"

        # failed → pending (retry)
        advance_run_status(db, _make_context(), pipeline_run_id=run.pipeline_run_id,
                           new_status="pending", expected_status="failed")
        assert get_pipeline_run(db, run.pipeline_run_id).status.value == "pending"

        # pending → running → succeeded
        advance_run_status(db, _make_context(), pipeline_run_id=run.pipeline_run_id,
                           new_status="running", expected_status="pending")
        updated = advance_run_status(db, _make_context(),
                                     pipeline_run_id=run.pipeline_run_id,
                                     new_status="succeeded", expected_status="running",
                                     output_json={"retried": True})
        assert updated.status.value == "succeeded"
        assert updated.output_json.get("retried") is True
