"""Contract tests for ``mneme.db.pg_arrays`` — PG text[] serialization.

Covers:
* ``to_pg_array`` — Python list → PG array literal
* ``parse_pg_array`` — PG array literal / JSON / Python list → Python list
* Round-trip fidelity for edge-case characters
* Backward-compat JSON parsing (legacy data)
"""

from __future__ import annotations

import pytest

from mneme.db.pg_arrays import parse_pg_array, to_pg_array


# ═══════════════════════════════════════════════════════════════════════════════
# to_pg_array
# ═══════════════════════════════════════════════════════════════════════════════


class TestToPgArray:
    """Python list → PG array literal serialization."""

    def test_empty_list(self):
        assert to_pg_array([]) == "{}"

    def test_none(self):
        assert to_pg_array(None) == "{}"

    def test_single_element(self):
        assert to_pg_array(["text/plain"]) == '{"text/plain"}'

    def test_multiple_elements(self):
        result = to_pg_array(["text/plain", "text/markdown", "application/pdf"])
        assert result == '{"text/plain","text/markdown","application/pdf"}'

    def test_elements_with_spaces(self):
        result = to_pg_array(["image caption", "code block"])
        assert result == '{"image caption","code block"}'

    def test_elements_with_backslash(self):
        # Backslash must be escaped as \\\\ (to produce literal \\ in PG)
        result = to_pg_array([r"path\to\file", r"normal"])
        assert result == r'{"path\\to\\file","normal"}'

    def test_elements_with_double_quote(self):
        result = to_pg_array(['he said "hello"', "plain"])
        assert result == r'{"he said \"hello\"","plain"}'

    def test_elements_with_comma(self):
        result = to_pg_array(["a,b", "c"])
        assert result == '{"a,b","c"}'

    def test_elements_with_curly_braces(self):
        result = to_pg_array(["{key: val}", "plain"])
        assert result == '{"{key: val}","plain"}'

    def test_mixed_special_characters(self):
        result = to_pg_array([
            'back\\slash',
            'quote"mark',
            'comma,here',
            '{brace}',
            "normal",
        ])
        # Verify round-trip
        parsed = parse_pg_array(result)
        assert parsed == [
            'back\\slash',
            'quote"mark',
            'comma,here',
            '{brace}',
            "normal",
        ]

    def test_chinese_characters(self):
        result = to_pg_array(["知识库", "记忆库", "向量库"])
        assert result == '{"知识库","记忆库","向量库"}'

    def test_numbers_as_strings(self):
        # All elements are stringified
        result = to_pg_array([1, 2.5, "three"])
        assert result == '{"1","2.5","three"}'


# ═══════════════════════════════════════════════════════════════════════════════
# parse_pg_array
# ═══════════════════════════════════════════════════════════════════════════════


class TestParsePgArray:
    """PG array literal / JSON / Python list → Python list deserialization."""

    # ── None / empty ───────────────────────────────────────────────────────

    def test_none(self):
        assert parse_pg_array(None) == []

    def test_empty_string(self):
        assert parse_pg_array("") == []

    def test_whitespace_only(self):
        assert parse_pg_array("   ") == []

    # ── Python list passthrough (psycopg2 normal path) ─────────────────────

    def test_python_list_passthrough(self):
        assert parse_pg_array(["a", "b", "c"]) == ["a", "b", "c"]

    def test_python_list_empty(self):
        assert parse_pg_array([]) == []

    def test_python_list_single(self):
        assert parse_pg_array(["only"]) == ["only"]

    # ── PG array literal ───────────────────────────────────────────────────

    def test_empty_pg_array(self):
        assert parse_pg_array("{}") == []

    def test_single_element_pg(self):
        assert parse_pg_array("{text/plain}") == ["text/plain"]

    def test_multiple_elements_pg(self):
        result = parse_pg_array('{"text/plain","text/markdown","application/pdf"}')
        assert result == ["text/plain", "text/markdown", "application/pdf"]

    def test_elements_with_spaces_pg(self):
        result = parse_pg_array('{"image caption","code block"}')
        assert result == ["image caption", "code block"]

    def test_elements_with_escaped_quote_pg(self):
        result = parse_pg_array(r'{"he said \"hello\"","plain"}')
        assert result == ['he said "hello"', "plain"]

    def test_elements_with_escaped_backslash_pg(self):
        result = parse_pg_array(r'{"path\\to\\file","normal"}')
        assert result == [r"path\to\file", "normal"]

    def test_elements_with_comma_inside_quotes_pg(self):
        result = parse_pg_array('{"a,b","c"}')
        assert result == ["a,b", "c"]

    def test_unquoted_simple_elements(self):
        # PG allows unquoted elements when they don't contain special chars
        result = parse_pg_array("{simple,clean,easy}")
        assert result == ["simple", "clean", "easy"]

    def test_mixed_quoted_unquoted(self):
        result = parse_pg_array('{"has space",plain,"has,comma",simple}')
        assert result == ["has space", "plain", "has,comma", "simple"]

    def test_chinese_pg(self):
        result = parse_pg_array('{"知识库","记忆库","向量库"}')
        assert result == ["知识库", "记忆库", "向量库"]

    # ── JSON array (legacy backward-compat) ────────────────────────────────

    def test_empty_json_array(self):
        assert parse_pg_array("[]") == []

    def test_single_element_json(self):
        assert parse_pg_array('["text/plain"]') == ["text/plain"]

    def test_multiple_elements_json(self):
        result = parse_pg_array('["text/plain","text/markdown","application/pdf"]')
        assert result == ["text/plain", "text/markdown", "application/pdf"]

    def test_json_with_numbers(self):
        result = parse_pg_array('[1, 2, 3]')
        assert result == ["1", "2", "3"]

    def test_json_with_special_chars(self):
        result = parse_pg_array('["he said \\"hello\\"","path\\\\to\\\\file"]')
        assert result == ['he said "hello"', r"path\to\file"]

    # ── Fallback ───────────────────────────────────────────────────────────

    def test_plain_string_fallback(self):
        # A bare string that's neither PG array nor JSON
        result = parse_pg_array("just-a-string")
        assert result == ["just-a-string"]

    def test_non_string_non_list(self):
        # Non-string, non-list → empty
        assert parse_pg_array(42) == []


# ═══════════════════════════════════════════════════════════════════════════════
# Round-trip integration
# ═══════════════════════════════════════════════════════════════════════════════


class TestRoundTrip:
    """to_pg_array → parse_pg_array round-trip fidelity."""

    CASES = [
        [],
        ["text/plain"],
        ["text/plain", "text/markdown", "application/pdf"],
        ["image/png", "image/jpeg"],
        ["hello world", "foo bar"],
        ["path\\to\\file", "normal"],
        ['quote"here', "plain"],
        ["has,comma", "normal"],
        ["{curly}", "flat"],
        ["知识库", "记忆库"],
    ]

    @pytest.mark.parametrize("original", CASES)
    def test_round_trip(self, original):
        pg_str = to_pg_array(original)
        parsed = parse_pg_array(pg_str)
        assert parsed == original, f"Round-trip failed: {original} → {pg_str} → {parsed}"

    def test_round_trip_via_list_passthrough(self):
        """When psycopg2 returns a list directly, parse_pg_array passes through."""
        original = ["a", "b", "c"]
        assert parse_pg_array(original) == original

    def test_full_write_read_cycle(self):
        """Simulate a full DB write→read cycle across both backends."""
        original = [
            "text/plain",
            "text/markdown",
            "application/pdf",
            "image/png",
        ]

        # Simulate write: convert to PG array literal string
        pg_literal = to_pg_array(original)

        # Simulate PostgreSQL read: psycopg2 returns Python list
        assert parse_pg_array(original) == original  # list passthrough

        # Simulate SQLite read: returns the stored PG literal string
        assert parse_pg_array(pg_literal) == original  # string → parsed

        # Simulate legacy JSON data read
        import json
        json_data = json.dumps(original)  # ["text/plain", ...]
        assert parse_pg_array(json_data) == original  # JSON → parsed


# ═══════════════════════════════════════════════════════════════════════════════
# Integration with DB (requires db fixture)
# ═══════════════════════════════════════════════════════════════════════════════


class TestDbIntegration:
    """Verify that text[] columns round-trip correctly through the DAL."""

    def test_pipeline_registry_round_trip(self, db):
        """Create → read: all text[] arrays survive the round-trip."""
        from mneme.db.pipeline_registry import (
            create_pipeline_registry,
            get_pipeline_registry,
        )
        from mneme.schemas.pipeline_registry import PipelineRegistryCreateRequest

        payload = PipelineRegistryCreateRequest(
            name="Round-trip Test Pipeline",
            input_formats=["text/plain", "text/markdown", "application/pdf"],
            processor_module="rt_test_module",
            accept_chunk_types=["text", "code", "table"],
            target_stores=["vector_store", "fulltext_store", "graph_store"],
        )

        created = create_pipeline_registry(db, payload)

        # Verify write: values should be non-empty lists
        assert created.input_formats == ["text/plain", "text/markdown", "application/pdf"]
        assert created.accept_chunk_types == ["text", "code", "table"]
        assert created.target_stores == ["vector_store", "fulltext_store", "graph_store"]

        # Re-read from DB
        fetched = get_pipeline_registry(db, created.id)
        assert fetched is not None
        assert fetched.input_formats == created.input_formats
        assert fetched.accept_chunk_types == created.accept_chunk_types
        assert fetched.target_stores == created.target_stores

    def test_pipeline_registry_empty_arrays(self, db):
        """Empty arrays should round-trip as empty lists."""
        from mneme.db.pipeline_registry import (
            create_pipeline_registry,
            get_pipeline_registry,
        )
        from mneme.schemas.pipeline_registry import PipelineRegistryCreateRequest

        payload = PipelineRegistryCreateRequest(
            name="Empty Arrays Test",
            input_formats=[],
            processor_module="empty_arrays_module",
            accept_chunk_types=[],
            target_stores=[],
        )

        created = create_pipeline_registry(db, payload)
        assert created.input_formats == []
        assert created.accept_chunk_types == []
        assert created.target_stores == []

        fetched = get_pipeline_registry(db, created.id)
        assert fetched is not None
        assert fetched.input_formats == []
        assert fetched.accept_chunk_types == []
        assert fetched.target_stores == []

    def test_pipeline_registry_special_characters(self, db):
        """Special characters in array elements survive round-trip."""
        from mneme.db.pipeline_registry import (
            create_pipeline_registry,
            get_pipeline_registry,
        )
        from mneme.schemas.pipeline_registry import PipelineRegistryCreateRequest

        payload = PipelineRegistryCreateRequest(
            name="Special Chars Test",
            input_formats=["text/plain; charset=utf-8", "image/svg+xml"],
            processor_module="special_chars_module",
            accept_chunk_types=["text with spaces", "code,block"],
            target_stores=["知识库", "记忆库"],
        )

        created = create_pipeline_registry(db, payload)
        assert "text/plain; charset=utf-8" in created.input_formats
        assert "text with spaces" in created.accept_chunk_types
        assert "知识库" in created.target_stores

        fetched = get_pipeline_registry(db, created.id)
        assert fetched is not None
        assert fetched.input_formats == created.input_formats
        assert fetched.accept_chunk_types == created.accept_chunk_types
        assert fetched.target_stores == created.target_stores

    def test_processing_jobs_round_trip(self, db):
        """Create → read: target_stores array survives the round-trip."""
        from mneme.db.pipeline_registry import create_pipeline_registry
        from mneme.db.processing_jobs import create_processing_job, get_processing_job
        from mneme.schemas.pipeline_registry import PipelineRegistryCreateRequest
        from mneme.schemas.processing_jobs import ProcessingJobCreateRequest
        from mneme.db.assets import lookup_asset_by_hash  # not needed, use raw UUID

        # Create a pipeline first
        pipeline_payload = PipelineRegistryCreateRequest(
            name="Job Test Pipeline",
            input_formats=["text/plain"],
            processor_module="job_test_module",
            accept_chunk_types=["text"],
            target_stores=["test_store"],
        )
        pipeline = create_pipeline_registry(db, pipeline_payload)

        import uuid as _uuid
        job_payload = ProcessingJobCreateRequest(
            asset_id=_uuid.uuid4(),
            pipeline_id=pipeline.id,
            target_stores=["vector", "graph", "fulltext"],
        )

        created = create_processing_job(db, payload=job_payload)
        assert created.target_stores == ["vector", "graph", "fulltext"]

        fetched = get_processing_job(db, created.id)
        assert fetched is not None
        assert fetched.target_stores == ["vector", "graph", "fulltext"]

    def test_seed_pipelines_have_valid_arrays(self, db):
        """Seeded pipelines should have correct text[] arrays after round-trip."""
        from mneme.db.pipeline_registry import (
            get_pipeline_registry_by_module,
            list_pipeline_registries,
            seed_pipeline_registries,
            delete_pipeline_registry,
        )

        # Clean up existing
        for item in list_pipeline_registries(db):
            delete_pipeline_registry(db, item.id)

        seed_pipeline_registries(db)

        chunker = get_pipeline_registry_by_module(db, "chunker_standard")
        assert chunker is not None
        assert isinstance(chunker.input_formats, list)
        assert "md" in chunker.input_formats
        assert "txt" in chunker.input_formats
        assert "office" in chunker.input_formats
        assert isinstance(chunker.accept_chunk_types, list)
        assert "text" in chunker.accept_chunk_types
        assert "code" in chunker.accept_chunk_types
        assert isinstance(chunker.target_stores, list)
        assert "vector" in chunker.target_stores
        assert "fulltext" in chunker.target_stores

        ocr = get_pipeline_registry_by_module(db, "ocr_parser")
        assert ocr is not None
        assert "pdf" in ocr.input_formats
        assert "image" in ocr.input_formats
        assert "text" in ocr.accept_chunk_types
        assert "image_caption" in ocr.accept_chunk_types
        assert "vector" in ocr.target_stores
        assert "graph" in ocr.target_stores

        conv = get_pipeline_registry_by_module(db, "conversation_parser")
        assert conv is not None
        assert "chat" in conv.input_formats
        assert "json" in conv.input_formats
        assert "text" in conv.accept_chunk_types
        assert "graph" in conv.target_stores
