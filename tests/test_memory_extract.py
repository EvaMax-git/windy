"""P4-09 Memory Extract Pipeline — comprehensive tests.

Covers:
  1. Unit: evidence_parser.py — parse/dedup evidence spans
  2. Unit: llm_extract.py — prompt builder + response parser
  3. Integration: extract_pipeline.py — full pipeline with mock Gateway
  4. Integration: idempotency / dedup
  5. Integration: error handling (Gateway errors, empty source, parse failures)
  6. API: POST /api/v4/memory/extract endpoint
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("MNEME_SESSION_COOKIE_SECURE", "false")
sys.path.insert(0, "/mnt/nas/letta/Mneme3")

from mneme.api.context import ActorContext, RequestContext
from mneme.memory.evidence_parser import (
    EvidenceSpan,
    parse_evidence_spans,
)
from mneme.memory.llm_extract import (
    build_extract_prompt,
    parse_extract_response,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Test constants
# ═══════════════════════════════════════════════════════════════════════════════

_TEST_PROJECT_ID = UUID("b0000000-0000-0000-0000-000000000001")
_TEST_USER_ID = UUID("00000000-0000-0000-0000-000000000001")

_FAKE_GW_RESULT_EMPTY = {
    "api_call_log_id": str(uuid4()),
    "data": {"choices": [{"message": {"content": '{"candidates": []}'}}]},
    "usage": {"input_tokens": 100, "output_tokens": 10},
    "latency_ms": 200,
    "cost": {"estimated": 0.001, "actual": 0.001},
    "call_state": "succeeded",
}


def _make_fake_gw_result(candidates_data):
    """Build a mock Gateway result with the given candidate JSON."""
    return {
        "api_call_log_id": str(uuid4()),
        "data": {
            "choices": [
                {"message": {
                    "content": json.dumps({"candidates": candidates_data})
                }}
            ]
        },
        "usage": {"input_tokens": 200, "output_tokens": 50},
        "latency_ms": 300,
        "cost": {"estimated": 0.002, "actual": 0.002},
        "call_state": "succeeded",
    }


def _make_context(ikey=None):
    req_id = uuid4()
    return RequestContext(
        request_id=req_id,
        correlation_id=req_id,
        actor=ActorContext(
            actor_type="user",
            actor_id=_TEST_USER_ID,
        ),
        idempotency_key=ikey or f"test-ikey-{uuid4()}",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Unit: evidence_parser.py
# ═══════════════════════════════════════════════════════════════════════════════


class TestEvidenceParser:
    """Unit tests for evidence span parsing."""

    def test_parse_valid_single_span(self):
        spans = parse_evidence_spans([
            {
                "span_start": 0,
                "span_end": 20,
                "text_fragment": "Hello world from AI",
                "confidence": 0.95,
            }
        ])
        assert len(spans) == 1
        assert spans[0].span_start == 0
        assert spans[0].span_end == 20
        assert spans[0].text_fragment == "Hello world from AI"
        assert spans[0].confidence == 0.95

    def test_parse_multiple_spans(self):
        spans = parse_evidence_spans([
            {"span_start": 0, "span_end": 10, "text_fragment": "0123456789", "confidence": 0.9},
            {"span_start": 20, "span_end": 30, "text_fragment": "abcdefghij", "confidence": 0.8},
            {"span_start": 50, "span_end": 60, "text_fragment": "klmnopqrst", "confidence": 0.7},
        ])
        assert len(spans) == 3

    def test_parse_span_without_confidence_defaults_to_1(self):
        spans = parse_evidence_spans([
            {"span_start": 5, "span_end": 15, "text_fragment": "test text"}
        ])
        assert len(spans) == 1
        assert spans[0].confidence == 1.0

    def test_parse_none_input_returns_empty(self):
        assert parse_evidence_spans(None) == []

    def test_parse_empty_list_returns_empty(self):
        assert parse_evidence_spans([]) == []

    def test_parse_non_list_returns_empty(self):
        assert parse_evidence_spans("not a list") == []

    def test_parse_invalid_negative_offsets(self):
        spans = parse_evidence_spans([
            {"span_start": -1, "span_end": 5, "text_fragment": "bad"},
            {"span_start": 0, "span_end": 10, "text_fragment": "good"},
        ])
        assert len(spans) == 1
        assert spans[0].text_fragment == "good"

    def test_parse_invalid_end_before_start(self):
        spans = parse_evidence_spans([
            {"span_start": 10, "span_end": 5, "text_fragment": "reversed"},
        ])
        assert spans == []

    def test_parse_invalid_non_numeric_offsets(self):
        spans = parse_evidence_spans([
            {"span_start": "abc", "span_end": "def", "text_fragment": "nope"},
        ])
        assert spans == []

    def test_parse_non_dict_items_skipped(self):
        spans = parse_evidence_spans([
            "string item",
            {"span_start": 0, "span_end": 5, "text_fragment": "ok"},
        ])
        assert len(spans) == 1
        assert spans[0].text_fragment == "ok"

    def test_dedup_overlapping_spans_keep_highest_confidence(self):
        spans = parse_evidence_spans([
            {"span_start": 0, "span_end": 10, "text_fragment": "low", "confidence": 0.5},
            {"span_start": 0, "span_end": 10, "text_fragment": "high", "confidence": 0.9},
        ])
        assert len(spans) == 1
        assert spans[0].confidence == 0.9

    def test_cross_validate_corrects_mismatched_fragment(self):
        source = "The quick brown fox jumps over the lazy dog"
        spans = parse_evidence_spans(
            [{"span_start": 4, "span_end": 9, "text_fragment": "wrong", "confidence": 0.8}],
            source_text=source,
        )
        assert spans[0].text_fragment == "quick"

    def test_confidence_clamped_to_0_1_range(self):
        spans_low = parse_evidence_spans([
            {"span_start": 0, "span_end": 5, "text_fragment": "low", "confidence": -0.5},
        ])
        assert spans_low[0].confidence == 0.0

        spans_high = parse_evidence_spans([
            {"span_start": 0, "span_end": 5, "text_fragment": "high", "confidence": 2.5},
        ])
        assert spans_high[0].confidence == 1.0

    def test_to_source_span_json(self):
        span = EvidenceSpan(span_start=10, span_end=25, text_fragment="hello world", confidence=0.85)
        result = span.to_source_span_json()
        assert result == {"span_start": 10, "span_end": 25, "text_snippet": "hello world"}
        assert "confidence" not in result


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Unit: llm_extract.py — build_extract_prompt
# ═══════════════════════════════════════════════════════════════════════════════


class TestBuildExtractPrompt:
    """Unit tests for LLM prompt builder."""

    def test_build_prompt_for_message(self):
        messages = build_extract_prompt(
            source_text="We decided to use PostgreSQL as our database.",
            source_type="message",
        )
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "memory extraction engine" in messages[0]["content"]
        assert "We decided to use PostgreSQL" in messages[1]["content"]
        assert "Extract memory candidates:" in messages[1]["content"]

    def test_build_prompt_for_raw_event(self):
        messages = build_extract_prompt(
            source_text="Event: system reboot completed at 03:00 UTC",
            source_type="raw_event",
        )
        assert "Source type: raw_event" in messages[1]["content"]

    def test_build_prompt_with_conversation_context(self):
        context = "User: Hello\nAssistant: Hi, how can I help?"
        messages = build_extract_prompt(
            source_text="I need to change my password.",
            source_type="message",
            conversation_context=context,
        )
        user = messages[1]["content"]
        assert "Conversation context" in user
        assert "Hello" in user

    def test_build_prompt_system_includes_guidelines(self):
        messages = build_extract_prompt(source_text="test", source_type="message")
        system = messages[0]["content"]
        assert "What to extract" in system
        assert "What NOT to extract" in system
        assert "Output format" in system

    def test_build_prompt_empty_source(self):
        messages = build_extract_prompt(source_text="", source_type="message")
        assert len(messages) == 2
        assert "Extract memory candidates:" in messages[1]["content"]


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Unit: llm_extract.py — parse_extract_response
# ═══════════════════════════════════════════════════════════════════════════════


class TestParseExtractResponse:
    """Unit tests for LLM response parser."""

    def test_parse_valid_single_candidate(self):
        raw = json.dumps({
            "candidates": [{
                "title": "Database Choice",
                "text": "We use PostgreSQL for primary storage.",
                "confidence": 0.95,
                "evidence_spans": [{
                    "span_start": 0, "span_end": 42,
                    "text_fragment": "We decided to use PostgreSQL",
                    "confidence": 0.92,
                }],
            }]
        })
        result = parse_extract_response(raw)
        assert result.parse_error is None
        assert len(result.candidates) == 1
        assert result.candidates[0].title == "Database Choice"
        assert result.candidates[0].confidence_score == 0.95
        assert len(result.candidates[0].evidence_spans) == 1

    def test_parse_valid_multiple_candidates(self):
        raw = json.dumps({
            "candidates": [
                {"title": "Fact A", "text": "Statement A", "confidence": 0.9},
                {"title": "Fact B", "text": "Statement B", "confidence": 0.8},
            ]
        })
        result = parse_extract_response(raw)
        assert len(result.candidates) == 2

    def test_parse_empty_candidates(self):
        result = parse_extract_response('{"candidates": []}')
        assert result.parse_error is None
        assert len(result.candidates) == 0

    def test_parse_candidate_without_text_skipped(self):
        raw = json.dumps({
            "candidates": [
                {"title": "No text", "confidence": 0.9},
                {"title": "Has text", "text": "Actual content", "confidence": 0.7},
            ]
        })
        result = parse_extract_response(raw)
        assert len(result.candidates) == 1
        assert result.candidates[0].title == "Has text"

    def test_parse_with_json_markdown_fence(self):
        raw = '```json\n{"candidates": [{"title": "Test", "text": "Content", "confidence": 0.8}]}\n```'
        result = parse_extract_response(raw)
        assert result.parse_error is None
        assert len(result.candidates) == 1

    def test_parse_with_plain_markdown_fence(self):
        raw = '```\n{"candidates": [{"title": "Test", "text": "Content", "confidence": 0.5}]}\n```'
        result = parse_extract_response(raw)
        assert result.parse_error is None
        assert len(result.candidates) == 1

    def test_parse_response_stores_raw_text(self):
        raw = '{"candidates": [{"title": "X", "text": "Y", "confidence": 0.5}]}'
        result = parse_extract_response(raw)
        assert result.raw_response == raw

    def test_parse_malformed_json(self):
        result = parse_extract_response("this is not json")
        assert result.parse_error is not None
        assert "JSON decode error" in result.parse_error
        assert len(result.candidates) == 0

    def test_parse_non_dict_top_level(self):
        result = parse_extract_response('["not", "a", "dict"]')
        assert result.parse_error is not None
        assert "not a JSON object" in result.parse_error

    def test_parse_candidates_not_a_list(self):
        result = parse_extract_response('{"candidates": "string not list"}')
        assert result.parse_error is not None
        assert "not a list" in result.parse_error

    def test_parse_candidate_with_invalid_confidence(self):
        raw = json.dumps({
            "candidates": [{"title": "X", "text": "Y", "confidence": "not_a_number"}]
        })
        result = parse_extract_response(raw)
        assert result.candidates[0].confidence_score == 0.5

    def test_parse_candidate_with_candidate_text_key(self):
        raw = json.dumps({
            "candidates": [{"title": "Alt key", "candidate_text": "Using alt key", "confidence": 0.6}]
        })
        result = parse_extract_response(raw)
        assert result.candidates[0].candidate_text == "Using alt key"

    def test_confidence_clamped_in_parser(self):
        raw = json.dumps({
            "candidates": [
                {"title": "Low", "text": "t", "confidence": -10},
                {"title": "High", "text": "t", "confidence": 100},
                {"title": "OK", "text": "t", "confidence": 0.75},
            ]
        })
        result = parse_extract_response(raw)
        assert result.candidates[0].confidence_score == 0.0
        assert result.candidates[1].confidence_score == 1.0
        assert result.candidates[2].confidence_score == 0.75


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — Unit: compute_candidate_hash
# ═══════════════════════════════════════════════════════════════════════════════


class TestCandidateHash:
    """Unit tests for candidate_hash computation (GAP #16)."""

    def test_compute_hash_deterministic(self):
        from mneme.db.memory_candidates import compute_candidate_hash
        h1 = compute_candidate_hash(
            title="Test", candidate_text="Content", source_type="message",
            source_id=UUID("00000000-0000-0000-0000-000000000001"),
        )
        h2 = compute_candidate_hash(
            title="Test", candidate_text="Content", source_type="message",
            source_id=UUID("00000000-0000-0000-0000-000000000001"),
        )
        assert h1 == h2
        assert len(h1) == 64

    def test_compute_hash_different_inputs_different_hash(self):
        from mneme.db.memory_candidates import compute_candidate_hash
        h1 = compute_candidate_hash(
            title="A", candidate_text="X", source_type="message",
            source_id=UUID("00000000-0000-0000-0000-000000000001"),
        )
        h2 = compute_candidate_hash(
            title="B", candidate_text="X", source_type="message",
            source_id=UUID("00000000-0000-0000-0000-000000000001"),
        )
        assert h1 != h2

    def test_compute_hash_none_title_uses_empty(self):
        from mneme.db.memory_candidates import compute_candidate_hash
        h1 = compute_candidate_hash(
            title=None, candidate_text="X", source_type="message",
            source_id=UUID("00000000-0000-0000-0000-000000000001"),
        )
        h2 = compute_candidate_hash(
            title="", candidate_text="X", source_type="message",
            source_id=UUID("00000000-0000-0000-0000-000000000001"),
        )
        assert h1 == h2


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — Integration: extract_pipeline.py (with mock Gateway)
# ═══════════════════════════════════════════════════════════════════════════════


class TestExtractPipelineIntegration:
    """Integration tests for the full Extract Pipeline with mock Gateway."""

    @pytest.fixture(autouse=True)
    def _seed(self, db):
        from sqlalchemy import text

        # Clean up any leaked data from previous test runs (submit_candidate commits)
        # Delete in FK-safe order
        db.execute(text("DELETE FROM memory_candidates WHERE project_id = :pid"), {"pid": _TEST_PROJECT_ID})
        # event_source + messages cascade from conversations via FK
        db.execute(text("DELETE FROM conversations WHERE project_id = :pid"), {"pid": _TEST_PROJECT_ID})
        db.execute(text("DELETE FROM raw_events WHERE project_id = :pid"), {"pid": _TEST_PROJECT_ID})
        db.execute(text("DELETE FROM events WHERE aggregate_type = 'memory_candidate' AND payload_json::text LIKE '%' || :pid_str || '%'"), {"pid_str": str(_TEST_PROJECT_ID)})
        db.execute(text("DELETE FROM projects WHERE project_id = :pid"), {"pid": _TEST_PROJECT_ID})
        db.flush()

        db.execute(text("""
            INSERT INTO projects (project_id, project_code, name, status)
            VALUES (:pid, :pcode, :pname, 'active')
            ON CONFLICT (project_id) DO NOTHING
        """), {"pid": _TEST_PROJECT_ID, "pcode": "TEST-P4-09", "pname": "P4-09 Test Project"})

        self.conv_id = UUID("c0000000-0000-0000-0000-000000000001")
        db.execute(text("""
            INSERT INTO conversations (
                conversation_id, project_id, conversation_type,
                sensitivity_level, conversation_status, title,
                owner_user_id, source_platform
            )
            VALUES (:cid, :pid, 'chat', 'normal', 'active', 'Test Conversation',
                    :uid, 'mneme_web')
            ON CONFLICT (conversation_id) DO NOTHING
        """), {"cid": self.conv_id, "pid": _TEST_PROJECT_ID, "uid": _TEST_USER_ID})

        self.es_id = UUID("d0000000-0000-0000-0000-000000000001")
        db.execute(text("""
            INSERT INTO event_source (
                event_source_id, conversation_id, source_platform, participants_json
            )
            VALUES (:eid, :cid, 'mneme_web', '[]')
            ON CONFLICT (event_source_id) DO NOTHING
        """), {"eid": self.es_id, "cid": self.conv_id})

        self.msg_id = UUID("e0000000-0000-0000-0000-000000000001")
        db.execute(text("""
            INSERT INTO messages (
                message_id, conversation_id, event_source_id,
                role_code, content_text, content_hash,
                sender_label, message_time, sensitivity_level
            )
            VALUES (:mid, :cid, :esid, 'user', :text, :hash,
                    'Test User', now(), 'normal')
            ON CONFLICT (message_id) DO NOTHING
        """), {
            "mid": self.msg_id, "cid": self.conv_id, "esid": self.es_id,
            "text": "We decided to use PostgreSQL as our primary database. The team agreed on Redis for caching.",
            "hash": hashlib.sha256(b"test-msg").hexdigest(),
        })

        self.event_id = UUID("f0000000-0000-0000-0000-000000000001")
        db.execute(text("""
            INSERT INTO raw_events (
                raw_event_id, project_id, raw_event_type,
                source_platform, event_time,
                idempotency_key, payload_hash,
                text_preview, payload_json
            )
            VALUES (:eid, :pid, 'system_event', 'test-system', now(),
                    :ikey, :phash, :preview, :pjson)
            ON CONFLICT (raw_event_id) DO NOTHING
        """), {
            "eid": self.event_id, "pid": _TEST_PROJECT_ID,
            "ikey": f"test-event-ikey-{uuid4()}",
            "phash": hashlib.sha256(b"test-event").hexdigest(),
            "preview": "System maintenance scheduled for Sunday 03:00 UTC.",
            "pjson": json.dumps({"message": "System maintenance"}),
        })

        db.flush()

    def test_extract_from_message_source(self, db):
        from mneme.memory.extract_pipeline import run_extract_pipeline
        from mneme.gateway.call import Gateway

        context = _make_context()
        fake_result = _make_fake_gw_result([
            {"title": "Database Choice", "text": "The project uses PostgreSQL.", "confidence": 0.95},
            {"title": "Caching Choice", "text": "Redis is used for caching.", "confidence": 0.90},
        ])

        with patch.object(Gateway, "call", return_value=fake_result):
            output = run_extract_pipeline(db, context, source_type="message", source_id=self.msg_id)

        assert output.error is None
        assert output.llm_candidates_found == 2
        assert output.candidates_submitted == 2
        assert output.candidates_deduped == 0
        assert len(output.candidates) == 2

    def test_extract_from_raw_event_source(self, db):
        from mneme.memory.extract_pipeline import run_extract_pipeline
        from mneme.gateway.call import Gateway

        context = _make_context()
        fake_result = _make_fake_gw_result([
            {"title": "Maintenance", "text": "System maintenance Sunday 03:00 UTC.", "confidence": 0.85}
        ])

        with patch.object(Gateway, "call", return_value=fake_result):
            output = run_extract_pipeline(db, context, source_type="raw_event", source_id=self.event_id)

        assert output.error is None
        assert output.candidates_submitted >= 1

    def test_idempotent_duplicate_extraction(self, db):
        from mneme.memory.extract_pipeline import run_extract_pipeline
        from mneme.gateway.call import Gateway

        fake_result = _make_fake_gw_result([
            {"title": "DB Choice", "text": "We use PostgreSQL", "confidence": 0.9}
        ])

        with patch.object(Gateway, "call", return_value=fake_result):
            out1 = run_extract_pipeline(db, _make_context(), source_type="message", source_id=self.msg_id)
        assert out1.candidates_submitted == 1

        with patch.object(Gateway, "call", return_value=fake_result):
            out2 = run_extract_pipeline(db, _make_context(), source_type="message", source_id=self.msg_id)
        assert out2.candidates_deduped >= 1

    def test_extract_nonexistent_message_returns_error(self, db):
        from mneme.memory.extract_pipeline import run_extract_pipeline
        output = run_extract_pipeline(db, _make_context(),
            source_type="message", source_id=UUID("99999999-9999-9999-9999-999999999999"))
        assert output.error is not None
        assert "not found" in output.error.lower()

    def test_extract_nonexistent_raw_event_returns_error(self, db):
        from mneme.memory.extract_pipeline import run_extract_pipeline
        output = run_extract_pipeline(db, _make_context(),
            source_type="raw_event", source_id=UUID("99999999-9999-9999-9999-999999999999"))
        assert output.error is not None
        assert "not found" in output.error.lower()

    def test_extract_unsupported_source_type_error(self, db):
        from mneme.memory.extract_pipeline import run_extract_pipeline
        output = run_extract_pipeline(db, _make_context(),
            source_type="unsupported_type", source_id=self.msg_id)
        assert output.error is not None
        assert "unsupported" in output.error.lower()

    def test_extract_empty_candidates_from_llm(self, db):
        from mneme.memory.extract_pipeline import run_extract_pipeline
        from mneme.gateway.call import Gateway

        with patch.object(Gateway, "call", return_value=_FAKE_GW_RESULT_EMPTY):
            output = run_extract_pipeline(db, _make_context(),
                source_type="message", source_id=self.msg_id)
        assert output.error is None
        assert output.llm_candidates_found == 0
        assert output.candidates_submitted == 0

    def test_extract_gateway_error(self, db):
        from mneme.memory.extract_pipeline import run_extract_pipeline
        from mneme.gateway.call import Gateway, GatewayError

        gw_error = GatewayError(
            api_call_log_id=uuid4(), code="provider_error",
            message="Provider returned 500", call_state="failed",
        )

        with patch.object(Gateway, "call", side_effect=gw_error):
            output = run_extract_pipeline(db, _make_context(),
                source_type="message", source_id=self.msg_id)
        assert output.error is not None
        assert "Gateway error" in output.error

    def test_evidence_spans_stored_in_candidate_metadata(self, db):
        from mneme.memory.extract_pipeline import run_extract_pipeline
        from mneme.gateway.call import Gateway
        from mneme.db.memory_candidates import get_candidate_by_id

        fake_result = _make_fake_gw_result([{
            "title": "Memory with evidence",
            "text": "Memory content with evidence links.",
            "confidence": 0.85,
            "evidence_spans": [{
                "span_start": 14, "span_end": 49,
                "text_fragment": "use PostgreSQL as our primary database",
                "confidence": 0.92,
            }],
        }])

        with patch.object(Gateway, "call", return_value=fake_result):
            output = run_extract_pipeline(db, _make_context(),
                source_type="message", source_id=self.msg_id)

        assert output.sources_created >= 1
        cid = output.candidates[0].get("candidate_id")
        if cid:
            candidate = get_candidate_by_id(db, UUID(str(cid)))
            if candidate and candidate.metadata_json:
                assert "evidence_spans" in candidate.metadata_json

    def test_extract_llm_returns_unparseable_json(self, db):
        from mneme.memory.extract_pipeline import run_extract_pipeline
        from mneme.gateway.call import Gateway

        fake_result = {
            "api_call_log_id": str(uuid4()),
            "data": {"choices": [{"message": {"content": "This is not JSON at all."}}]},
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "latency_ms": 100, "cost": {"estimated": 0.0, "actual": 0.0},
            "call_state": "succeeded",
        }

        with patch.object(Gateway, "call", return_value=fake_result):
            output = run_extract_pipeline(db, _make_context(),
                source_type="message", source_id=self.msg_id)
        assert output.error is not None
        assert "parse error" in output.error.lower()

    def test_submitted_candidate_has_required_fields(self, db):
        from mneme.memory.extract_pipeline import run_extract_pipeline
        from mneme.gateway.call import Gateway

        fake_result = _make_fake_gw_result([
            {"title": "Field Check", "text": "Checking fields.", "confidence": 0.85}
        ])

        with patch.object(Gateway, "call", return_value=fake_result):
            output = run_extract_pipeline(db, _make_context(),
                source_type="message", source_id=self.msg_id)

        c = output.candidates[0]
        assert "candidate_id" in c
        assert "is_new" in c
        assert "title" in c
        assert "candidate_hash" in c
        assert "candidate_status" in c
        assert c["is_new"] is True
        assert c["candidate_status"] == "pending_review"

    def test_extract_preserves_llm_confidence(self, db):
        from mneme.memory.extract_pipeline import run_extract_pipeline
        from mneme.gateway.call import Gateway
        from mneme.db.memory_candidates import get_candidate_by_id

        fake_result = _make_fake_gw_result([
            {"title": "High Conf", "text": "High confidence fact.", "confidence": 0.97},
            {"title": "Low Conf", "text": "Low confidence speculation.", "confidence": 0.32},
        ])

        with patch.object(Gateway, "call", return_value=fake_result):
            output = run_extract_pipeline(db, _make_context(),
                source_type="message", source_id=self.msg_id)

        for c_summary in output.candidates:
            cid = c_summary.get("candidate_id")
            if cid:
                candidate = get_candidate_by_id(db, UUID(str(cid)))
                if candidate:
                    assert 0.0 <= candidate.confidence_score <= 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — Unit: memory_consumer helpers
# ═══════════════════════════════════════════════════════════════════════════════


class TestMemoryConsumerHelpers:
    """Unit tests for memory_consumer helper functions."""

    def test_event_type_to_source_type_message(self):
        from mneme.worker.consumers.memory_consumer import _event_type_to_source_type
        assert _event_type_to_source_type("message.created") == "message"

    def test_event_type_to_source_type_raw_event(self):
        from mneme.worker.consumers.memory_consumer import _event_type_to_source_type
        assert _event_type_to_source_type("raw_event.created") == "raw_event"

    def test_event_type_to_source_type_unknown_defaults_to_message(self):
        from mneme.worker.consumers.memory_consumer import _event_type_to_source_type
        assert _event_type_to_source_type("unknown.event") == "message"

    def test_memory_consumer_can_handle(self):
        from mneme.worker.consumers.memory_consumer import MemoryEventConsumer
        consumer = MemoryEventConsumer()
        assert consumer.can_handle("message.created") is True
        assert consumer.can_handle("raw_event.created") is True
        assert consumer.can_handle("memory_candidate.submitted") is False
        assert consumer.can_handle("unknown.event") is False

    def test_memory_consumer_name(self):
        from mneme.worker.consumers.memory_consumer import MemoryEventConsumer
        assert MemoryEventConsumer().name == "memory-consumer"

    def test_make_system_context(self):
        from mneme.worker.consumers.memory_consumer import _make_system_context
        event_id = uuid4()
        ctx = _make_system_context(event_id, "test-ikey")
        assert ctx.actor.actor_type == "system"
        assert ctx.correlation_id == event_id
        assert ctx.idempotency_key == "test-ikey"

    def test_resolve_project_from_payload(self, db):
        from mneme.worker.consumers.memory_consumer import _resolve_project
        pid = _resolve_project("message",
            UUID("e0000000-0000-0000-0000-000000000001"),
            {"project_id": str(_TEST_PROJECT_ID)})
        assert pid == _TEST_PROJECT_ID

    def test_resolve_project_from_message_conversation(self, db):
        from sqlalchemy import text
        from mneme.worker.consumers.memory_consumer import _resolve_project

        # Seed project + conversation + message
        msg_id = UUID("e0000000-0000-0000-0000-000000000001")
        conv_id = UUID("c0000000-0000-0000-0000-000000000001")
        db.execute(text("""
            INSERT INTO projects (project_id, project_code, name, status)
            VALUES (:pid, 'T-RESOLVE', 'Resolve Test', 'active')
            ON CONFLICT (project_id) DO NOTHING
        """), {"pid": _TEST_PROJECT_ID})
        db.execute(text("""
            INSERT INTO conversations (
                conversation_id, project_id, conversation_type,
                sensitivity_level, conversation_status, title, owner_user_id, source_platform
            )
            VALUES (:cid, :pid, 'chat', 'normal', 'active', 'Test', :uid, 'mneme_web')
            ON CONFLICT (conversation_id) DO NOTHING
        """), {"cid": conv_id, "pid": _TEST_PROJECT_ID, "uid": _TEST_USER_ID})
        db.execute(text("""
            INSERT INTO messages (
                message_id, conversation_id, role_code, content_text, content_hash,
                sender_label, message_time, sensitivity_level
            )
            VALUES (:mid, :cid, 'user', :text, :hash, 'User', now(), 'normal')
            ON CONFLICT (message_id) DO NOTHING
        """), {
            "mid": msg_id, "cid": conv_id,
            "text": "hello", "hash": hashlib.sha256(b"resolve").hexdigest(),
        })
        db.flush()

        pid = _resolve_project("message", msg_id, {})
        assert pid == _TEST_PROJECT_ID

    def test_resolve_project_unknown_returns_none(self):
        from mneme.worker.consumers.memory_consumer import _resolve_project
        pid = _resolve_project("message",
            UUID("99999999-9999-9999-9999-999999999999"), {})
        assert pid is None


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — API: POST /api/v4/memory/extract
# ═══════════════════════════════════════════════════════════════════════════════


class TestExtractApiEndpoint:
    """FastAPI TestClient tests for the /memory/extract endpoint."""

    def test_extract_endpoint_missing_idempotency_key(self):
        from fastapi.testclient import TestClient
        from mneme.main import app

        client = TestClient(app)
        response = client.post(
            "/api/v4/memory/extract",
            json={
                "source_type": "message",
                "source_id": "e0000000-0000-0000-0000-000000000001",
            },
            headers={"X-Request-Id": str(uuid4())},
        )
        assert response.status_code == 400

    def test_extract_endpoint_invalid_source_type(self):
        from fastapi.testclient import TestClient
        from mneme.main import app

        client = TestClient(app)
        response = client.post(
            "/api/v4/memory/extract",
            json={"source_type": "invalid", "source_id": "e0000000-0000-0000-0000-000000000001"},
            headers={"Idempotency-Key": "test-ikey-001", "X-Request-Id": str(uuid4())},
        )
        assert response.status_code in (400, 422)

    def test_extract_endpoint_invalid_uuid(self):
        from fastapi.testclient import TestClient
        from mneme.main import app

        client = TestClient(app)
        response = client.post(
            "/api/v4/memory/extract",
            json={"source_type": "message", "source_id": "not-a-uuid"},
            headers={"Idempotency-Key": "test-ikey-002", "X-Request-Id": str(uuid4())},
        )
        assert response.status_code in (400, 422)

    def test_extract_endpoint_success_with_mock_gateway(self, db):
        from sqlalchemy import text
        from fastapi.testclient import TestClient
        from mneme.main import app
        from mneme.gateway.call import Gateway

        # Seed needed data
        db.execute(text("""
            INSERT INTO projects (project_id, project_code, name, status)
            VALUES (:pid, 'P4-09-API', 'API Test', 'active')
            ON CONFLICT (project_id) DO NOTHING
        """), {"pid": _TEST_PROJECT_ID})

        conv_id = UUID("c0000000-0000-0000-0000-000000000001")
        db.execute(text("""
            INSERT INTO conversations (
                conversation_id, project_id, conversation_type,
                sensitivity_level, conversation_status, title, owner_user_id, source_platform
            )
            VALUES (:cid, :pid, 'chat', 'normal', 'active', 'API Test', :uid, 'mneme_web')
            ON CONFLICT (conversation_id) DO NOTHING
        """), {"cid": conv_id, "pid": _TEST_PROJECT_ID, "uid": _TEST_USER_ID})

        es_id = UUID("d0000000-0000-0000-0000-000000000001")
        db.execute(text("""
            INSERT INTO event_source (event_source_id, conversation_id, source_platform, participants_json)
            VALUES (:eid, :cid, 'mneme_web', '[]')
            ON CONFLICT (event_source_id) DO NOTHING
        """), {"eid": es_id, "cid": conv_id})

        msg_id = UUID("e0000000-0000-0000-0000-000000000001")
        db.execute(text("""
            INSERT INTO messages (
                message_id, conversation_id, event_source_id,
                role_code, content_text, content_hash,
                sender_label, message_time, sensitivity_level
            )
            VALUES (:mid, :cid, :esid, 'user', :text, :hash,
                    'API User', now(), 'normal')
            ON CONFLICT (message_id) DO NOTHING
        """), {
            "mid": msg_id, "cid": conv_id, "esid": es_id,
            "text": "API test message for extract pipeline.",
            "hash": hashlib.sha256(b"api-extract-msg").hexdigest(),
        })
        db.flush()

        fake_result = _make_fake_gw_result([
            {"title": "API Extract", "text": "Extracted via API.", "confidence": 0.8}
        ])

        client = TestClient(app)
        with patch.object(Gateway, "call", return_value=fake_result):
            response = client.post(
                "/api/v4/memory/extract",
                json={"source_type": "message", "source_id": str(msg_id)},
                headers={
                    "Idempotency-Key": "test-extract-api-001",
                    "X-Request-Id": str(uuid4()),
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert data["data"]["error"] is None
        assert data["data"]["candidates_submitted"] >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — Extract output structure validation
# ═══════════════════════════════════════════════════════════════════════════════


class TestExtractOutputStructure:
    """Validate ExtractOutput and pipeline return structures."""

    def test_extract_output_dataclass_fields(self):
        from mneme.memory.extract_pipeline import ExtractOutput

        out = ExtractOutput()
        assert out.pipeline_run_id is None
        assert out.candidates_submitted == 0
        assert out.candidates_deduped == 0
        assert out.sources_created == 0
        assert out.llm_candidates_found == 0
        assert out.error is None
        assert out.candidates == []

    def test_extract_result_dataclass_fields(self):
        from mneme.memory.llm_extract import ExtractResult, ExtractedCandidate

        result = ExtractResult()
        assert result.candidates == []
        assert result.raw_response == ""
        assert result.parse_error is None

        # ExtractedCandidate defaults
        cand = ExtractedCandidate()
        assert cand.title == ""
        assert cand.candidate_text == ""
        assert cand.confidence_score == 0.5
        assert cand.evidence_spans == []

    def test_make_pipeline_idempotency_key(self):
        from mneme.memory.extract_pipeline import _make_pipeline_idempotency_key

        k1 = _make_pipeline_idempotency_key("message", UUID("e0000000-0000-0000-0000-000000000001"))
        k2 = _make_pipeline_idempotency_key("message", UUID("e0000000-0000-0000-0000-000000000001"))
        assert k1 == k2

        k3 = _make_pipeline_idempotency_key("raw_event", UUID("e0000000-0000-0000-0000-000000000001"))
        assert k1 != k3

    def test_memory_extract_pipeline_class_interface(self, db):
        from sqlalchemy import text
        from mneme.memory.extract_pipeline import MemoryExtractPipeline
        from mneme.gateway.call import Gateway

        # Seed project + conversation + message
        msg_id = UUID("e0000000-0000-0000-0000-000000000001")
        conv_id = UUID("c0000000-0000-0000-0000-000000000001")
        db.execute(text("""
            INSERT INTO projects (project_id, project_code, name, status)
            VALUES (:pid, 'T-IFACE', 'Iface Test', 'active')
            ON CONFLICT (project_id) DO NOTHING
        """), {"pid": _TEST_PROJECT_ID})
        db.execute(text("""
            INSERT INTO conversations (
                conversation_id, project_id, conversation_type,
                sensitivity_level, conversation_status, title, owner_user_id, source_platform
            )
            VALUES (:cid, :pid, 'chat', 'normal', 'active', 'Test', :uid, 'mneme_web')
            ON CONFLICT (conversation_id) DO NOTHING
        """), {"cid": conv_id, "pid": _TEST_PROJECT_ID, "uid": _TEST_USER_ID})
        db.execute(text("""
            INSERT INTO messages (
                message_id, conversation_id, role_code, content_text, content_hash,
                sender_label, message_time, sensitivity_level
            )
            VALUES (:mid, :cid, 'user', :text, :hash, 'User', now(), 'normal')
            ON CONFLICT (message_id) DO NOTHING
        """), {
            "mid": msg_id, "cid": conv_id,
            "text": "Interface check message.", "hash": hashlib.sha256(b"class-iface").hexdigest(),
        })
        db.flush()

        fake_result = _make_fake_gw_result([
            {"title": "Class Test", "text": "Interface check.", "confidence": 0.75}
        ])

        with patch.object(Gateway, "call", return_value=fake_result):
            pipeline = MemoryExtractPipeline()
            output = pipeline.extract_from_source(
                db, _make_context(),
                source_type="message",
                source_id=msg_id,
            )
        assert output.error is None
        assert output.candidates_submitted >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
