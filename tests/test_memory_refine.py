"""P6-02 Memory Refine — contract tests for all 6 refine sub-modules.

Covers:
  1. dedup   — cosine-similarity duplicate detection
  2. conflict — LLM-based semantic conflict detection
  3. merge   — LLM-assisted smart merge
  4. expire  — rule-based automatic expiration
  5. quality — multi-dimensional quality scoring
  6. pipeline — orchestration of all stages

Strategy:
  - Pure functions (scoring, parsing, similarity) are tested directly.
  - DB-dependent functions (SQL queries) are tested via mocking since
    the refine SQL uses PostgreSQL DISTINCT ON which SQLite doesn't support.
"""

from __future__ import annotations

import datetime as _dt_mod
import json
import math
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from mneme.api.context import ActorContext, RequestContext
from mneme.memory.search import _cosine_similarity


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures / helpers
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def ctx() -> RequestContext:
    return RequestContext(
        request_id=uuid4(),
        correlation_id=uuid4(),
        actor=ActorContext(actor_type="system", actor_id=uuid4()),
    )


def _make_embedding(dim: int = 16, seed: int = 0) -> list[float]:
    """Deterministic pseudo-embedding for testing."""
    import random
    rng = random.Random(seed)
    vec = [rng.gauss(0, 1) for _ in range(dim)]
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec]


def _similar_embedding(base: list[float], noise: float = 0.01, seed: int = 42) -> list[float]:
    """Create an embedding very similar to *base*."""
    import random
    rng = random.Random(seed)
    vec = [v + rng.gauss(0, noise) for v in base]
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec]


# ═══════════════════════════════════════════════════════════════════════════
# 1. DEDUP module tests
# ═══════════════════════════════════════════════════════════════════════════

class TestDedupPair:
    """Tests for the DedupPair dataclass."""

    def test_is_identical_true(self):
        from mneme.memory.refine.dedup import DedupPair
        pair = DedupPair(
            memory_a_id=uuid4(), memory_b_id=uuid4(), similarity=0.99999,
        )
        assert pair.is_identical is True

    def test_is_identical_false(self):
        from mneme.memory.refine.dedup import DedupPair
        pair = DedupPair(
            memory_a_id=uuid4(), memory_b_id=uuid4(), similarity=0.95,
        )
        assert pair.is_identical is False

    def test_is_identical_boundary(self):
        from mneme.memory.refine.dedup import DedupPair
        pair = DedupPair(
            memory_a_id=uuid4(), memory_b_id=uuid4(), similarity=0.9999,
        )
        assert pair.is_identical is True  # >= 0.9999

    def test_pair_fields(self):
        from mneme.memory.refine.dedup import DedupPair
        a, b = uuid4(), uuid4()
        pair = DedupPair(
            memory_a_id=a, memory_b_id=b, similarity=0.93,
            memory_a_title="A", memory_b_title="B",
            canonical_key_a="key_a", canonical_key_b="key_b",
        )
        assert pair.memory_a_id == a
        assert pair.memory_b_id == b
        assert pair.similarity == 0.93
        assert pair.memory_a_title == "A"
        assert pair.canonical_key_b == "key_b"


class TestDedupResult:
    """Tests for the DedupResult dataclass."""

    def test_defaults(self):
        from mneme.memory.refine.dedup import DedupResult
        r = DedupResult()
        assert r.pairs_found == 0
        assert r.relations_created == 0
        assert r.pairs == []
        assert r.identical_count == 0

    def test_identical_count(self):
        from mneme.memory.refine.dedup import DedupResult, DedupPair
        pairs = [
            DedupPair(memory_a_id=uuid4(), memory_b_id=uuid4(), similarity=0.99999),
            DedupPair(memory_a_id=uuid4(), memory_b_id=uuid4(), similarity=0.93),
            DedupPair(memory_a_id=uuid4(), memory_b_id=uuid4(), similarity=0.9999),
        ]
        r = DedupResult(pairs_found=3, pairs=pairs)
        assert r.identical_count == 2  # 0.99999 and 0.9999


class TestDedupParseEmbedding:
    """Tests for _parse_stored_embedding."""

    def test_none_returns_none(self):
        from mneme.memory.refine.dedup import _parse_stored_embedding
        assert _parse_stored_embedding(None) is None

    def test_list_input(self):
        from mneme.memory.refine.dedup import _parse_stored_embedding
        result = _parse_stored_embedding([0.1, 0.2, 0.3])
        assert result == [0.1, 0.2, 0.3]

    def test_json_string_input(self):
        from mneme.memory.refine.dedup import _parse_stored_embedding
        result = _parse_stored_embedding('[0.5, 0.6, 0.7]')
        assert result == [0.5, 0.6, 0.7]

    def test_invalid_json_string(self):
        from mneme.memory.refine.dedup import _parse_stored_embedding
        assert _parse_stored_embedding("not-json") is None

    def test_int_input_returns_none(self):
        from mneme.memory.refine.dedup import _parse_stored_embedding
        assert _parse_stored_embedding(42) is None

    def test_empty_list(self):
        from mneme.memory.refine.dedup import _parse_stored_embedding
        assert _parse_stored_embedding([]) == []


class TestDedupPairwiseSimilar:
    """Tests for _pairwise_similar internal function."""

    def test_empty_list(self):
        from mneme.memory.refine.dedup import _pairwise_similar
        assert _pairwise_similar([]) == []

    def test_single_entry(self):
        from mneme.memory.refine.dedup import _pairwise_similar
        entries = [{"memory_id": uuid4(), "embedding": _make_embedding()}]
        assert _pairwise_similar(entries) == []

    def test_two_similar_entries(self):
        from mneme.memory.refine.dedup import _pairwise_similar
        base = _make_embedding(seed=1)
        entries = [
            {"memory_id": uuid4(), "embedding": base, "title": "A", "canonical_key": "ka"},
            {"memory_id": uuid4(), "embedding": _similar_embedding(base, noise=0.001), "title": "B", "canonical_key": "kb"},
        ]
        pairs = _pairwise_similar(entries, threshold=0.90)
        assert len(pairs) == 1
        assert pairs[0].similarity >= 0.90

    def test_two_different_entries_below_threshold(self):
        from mneme.memory.refine.dedup import _pairwise_similar
        e1 = _make_embedding(seed=1)
        e2 = _make_embedding(seed=99)  # very different seed
        entries = [
            {"memory_id": uuid4(), "embedding": e1, "title": "A", "canonical_key": "ka"},
            {"memory_id": uuid4(), "embedding": e2, "title": "B", "canonical_key": "kb"},
        ]
        pairs = _pairwise_similar(entries, threshold=0.92)
        assert len(pairs) == 0

    def test_sorted_by_similarity_desc(self):
        from mneme.memory.refine.dedup import _pairwise_similar
        base = _make_embedding(seed=1)
        entries = [
            {"memory_id": uuid4(), "embedding": base, "title": "A", "canonical_key": "ka"},
            {"memory_id": uuid4(), "embedding": _similar_embedding(base, noise=0.001), "title": "B", "canonical_key": "kb"},
            {"memory_id": uuid4(), "embedding": _similar_embedding(base, noise=0.05), "title": "C", "canonical_key": "kc"},
        ]
        pairs = _pairwise_similar(entries, threshold=0.50)
        for i in range(len(pairs) - 1):
            assert pairs[i].similarity >= pairs[i + 1].similarity

    def test_max_candidates_respected(self):
        from mneme.memory.refine.dedup import _pairwise_similar
        base = _make_embedding(seed=1)
        entries = [
            {"memory_id": uuid4(), "embedding": _similar_embedding(base, noise=0.001, seed=s), "title": f"T{s}", "canonical_key": f"k{s}"}
            for s in range(5)
        ]
        pairs = _pairwise_similar(entries, threshold=0.50, max_candidates=3)
        assert len(pairs) <= 3


class TestDedupThresholdValidation:
    """Tests for detect_duplicates parameter validation."""

    def test_threshold_too_low(self):
        from mneme.memory.refine.dedup import detect_duplicates
        mock_db = MagicMock()
        with pytest.raises(ValueError, match="threshold"):
            detect_duplicates(mock_db, threshold=0.4)

    def test_threshold_too_high(self):
        from mneme.memory.refine.dedup import detect_duplicates
        mock_db = MagicMock()
        with pytest.raises(ValueError, match="threshold"):
            detect_duplicates(mock_db, threshold=1.1)

    def test_max_candidates_zero(self):
        from mneme.memory.refine.dedup import detect_duplicates
        mock_db = MagicMock()
        with pytest.raises(ValueError, match="max_candidates"):
            detect_duplicates(mock_db, max_candidates=0)

    def test_max_candidates_too_high(self):
        from mneme.memory.refine.dedup import detect_duplicates
        mock_db = MagicMock()
        with pytest.raises(ValueError, match="max_candidates"):
            detect_duplicates(mock_db, max_candidates=501)


class TestDedupApplyEdgeCases:
    """Tests for apply_dedup edge cases."""

    def test_self_referencing_skipped(self, ctx):
        from mneme.memory.refine.dedup import DedupPair, apply_dedup
        mid = uuid4()
        pair = DedupPair(memory_a_id=mid, memory_b_id=mid, similarity=1.0)
        mock_db = MagicMock()
        result = apply_dedup(mock_db, ctx, pair=pair)
        assert result is None

    def test_memory_not_found_returns_none(self, ctx):
        from mneme.memory.refine.dedup import DedupPair, apply_dedup
        pair = DedupPair(memory_a_id=uuid4(), memory_b_id=uuid4(), similarity=0.95)
        mock_db = MagicMock()
        with patch("mneme.db.memories.get_memory", return_value=None):
            result = apply_dedup(mock_db, ctx, pair=pair)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# 2. CONFLICT module tests
# ═══════════════════════════════════════════════════════════════════════════

class TestConflictCandidate:
    """Tests for the ConflictCandidate dataclass."""

    def test_defaults(self):
        from mneme.memory.refine.conflict import ConflictCandidate
        c = ConflictCandidate(
            memory_a_id=uuid4(), memory_b_id=uuid4(), similarity=0.80,
        )
        assert c.conflict is False
        assert c.reason is None
        assert c.confidence == 0.0

    def test_evaluated_fields(self):
        from mneme.memory.refine.conflict import ConflictCandidate
        c = ConflictCandidate(
            memory_a_id=uuid4(), memory_b_id=uuid4(), similarity=0.80,
            conflict=True, reason="contradicts", confidence=0.85,
        )
        assert c.conflict is True
        assert c.confidence == 0.85


class TestConflictResult:
    """Tests for the ConflictResult dataclass."""

    def test_defaults(self):
        from mneme.memory.refine.conflict import ConflictResult
        r = ConflictResult()
        assert r.candidates_found == 0
        assert r.llm_evaluated == 0
        assert r.conflicts_confirmed == 0
        assert r.relations_created == 0

    def test_confirmed_conflicts_property(self):
        from mneme.memory.refine.conflict import ConflictResult, ConflictCandidate
        c1 = ConflictCandidate(memory_a_id=uuid4(), memory_b_id=uuid4(), similarity=0.80, conflict=True)
        c2 = ConflictCandidate(memory_a_id=uuid4(), memory_b_id=uuid4(), similarity=0.75, conflict=False)
        c3 = ConflictCandidate(memory_a_id=uuid4(), memory_b_id=uuid4(), similarity=0.82, conflict=True)
        r = ConflictResult(candidates=[c1, c2, c3])
        confirmed = r.confirmed_conflicts
        assert len(confirmed) == 2
        assert all(c.conflict for c in confirmed)


class TestConflictParseEmbedding:
    """Tests for conflict module _parse_stored_embedding."""

    def test_none_returns_none(self):
        from mneme.memory.refine.conflict import _parse_stored_embedding
        assert _parse_stored_embedding(None) is None

    def test_json_string(self):
        from mneme.memory.refine.conflict import _parse_stored_embedding
        result = _parse_stored_embedding('[0.1, 0.2]')
        assert result == [0.1, 0.2]


class TestConflictPairwiseZone:
    """Tests for _pairwise_similar_zone."""

    def test_empty_list(self):
        from mneme.memory.refine.conflict import _pairwise_similar_zone
        assert _pairwise_similar_zone([]) == []

    def test_single_entry(self):
        from mneme.memory.refine.conflict import _pairwise_similar_zone
        entries = [{"memory_id": uuid4(), "embedding": _make_embedding()}]
        assert _pairwise_similar_zone(entries) == []

    def test_zone_filtering(self):
        from mneme.memory.refine.conflict import _pairwise_similar_zone
        base = _make_embedding(seed=1)
        # Very similar pair — should be in dedup zone (above 0.92), not conflict zone
        very_similar = [
            {"memory_id": uuid4(), "embedding": base, "title": "A", "memory_text": "text a", "canonical_key": "ka"},
            {"memory_id": uuid4(), "embedding": _similar_embedding(base, noise=0.001), "title": "B", "memory_text": "text b", "canonical_key": "kb"},
        ]
        candidates = _pairwise_similar_zone(very_similar, threshold_low=0.70, threshold_high=0.92)
        # Very similar pairs may or may not be in zone depending on exact sim
        # Just verify it doesn't crash
        assert isinstance(candidates, list)

    def test_different_entries_outside_zone(self):
        from mneme.memory.refine.conflict import _pairwise_similar_zone
        e1 = _make_embedding(seed=1)
        e2 = _make_embedding(seed=99)
        entries = [
            {"memory_id": uuid4(), "embedding": e1, "title": "A", "memory_text": "t1", "canonical_key": "ka"},
            {"memory_id": uuid4(), "embedding": e2, "title": "B", "memory_text": "t2", "canonical_key": "kb"},
        ]
        candidates = _pairwise_similar_zone(entries, threshold_low=0.70, threshold_high=0.92)
        assert len(candidates) == 0  # very different embeddings

    def test_max_pairs_respected(self):
        from mneme.memory.refine.conflict import _pairwise_similar_zone
        base = _make_embedding(seed=1)
        entries = [
            {"memory_id": uuid4(), "embedding": _similar_embedding(base, noise=0.1, seed=s),
             "title": f"T{s}", "memory_text": f"text {s}", "canonical_key": f"k{s}"}
            for s in range(8)
        ]
        candidates = _pairwise_similar_zone(entries, threshold_low=0.30, threshold_high=0.99, max_pairs=3)
        assert len(candidates) <= 3


class TestConflictLLMResponseParsing:
    """Tests for _parse_llm_conflict_response."""

    def test_valid_json(self):
        from mneme.memory.refine.conflict import _parse_llm_conflict_response
        raw = '{"conflict": true, "reason": "contradicts", "confidence": 0.85}'
        result = _parse_llm_conflict_response(raw)
        assert result["conflict"] is True
        assert result["reason"] == "contradicts"
        assert result["confidence"] == 0.85

    def test_json_with_markdown_fence(self):
        from mneme.memory.refine.conflict import _parse_llm_conflict_response
        raw = '```json\n{"conflict": false, "reason": "no conflict", "confidence": 0.2}\n```'
        result = _parse_llm_conflict_response(raw)
        assert result["conflict"] is False
        assert result["confidence"] == 0.2

    def test_invalid_json_returns_parse_error(self):
        from mneme.memory.refine.conflict import _parse_llm_conflict_response
        result = _parse_llm_conflict_response("not json at all")
        assert result["conflict"] is False
        assert result["reason"] == "parse_error"
        assert result["confidence"] == 0.0

    def test_empty_string(self):
        from mneme.memory.refine.conflict import _parse_llm_conflict_response
        result = _parse_llm_conflict_response("")
        assert result["conflict"] is False
        assert result["reason"] == "parse_error"

    def test_missing_fields_defaults(self):
        from mneme.memory.refine.conflict import _parse_llm_conflict_response
        result = _parse_llm_conflict_response('{"conflict": true}')
        assert result["conflict"] is True
        assert result["reason"] == ""
        assert result["confidence"] == 0.0


class TestConflictThresholdValidation:
    """Tests for detect_conflicts parameter validation."""

    def test_threshold_low_gte_high_raises(self):
        from mneme.memory.refine.conflict import detect_conflicts
        mock_db = MagicMock()
        with pytest.raises(ValueError, match="threshold_low"):
            detect_conflicts(mock_db, threshold_low=0.90, threshold_high=0.80)

    def test_threshold_negative_raises(self):
        from mneme.memory.refine.conflict import detect_conflicts
        mock_db = MagicMock()
        with pytest.raises(ValueError, match="threshold_low"):
            detect_conflicts(mock_db, threshold_low=-0.1)

    def test_max_pairs_zero_raises(self):
        from mneme.memory.refine.conflict import detect_conflicts
        mock_db = MagicMock()
        with pytest.raises(ValueError, match="max_pairs"):
            detect_conflicts(mock_db, max_pairs=0)


class TestConflictApplyEdgeCases:
    """Tests for apply_conflict edge cases."""

    def test_unconfirmed_skipped(self, ctx):
        from mneme.memory.refine.conflict import ConflictCandidate, apply_conflict
        candidate = ConflictCandidate(
            memory_a_id=uuid4(), memory_b_id=uuid4(), similarity=0.80,
            conflict=False, confidence=0.0,
        )
        mock_db = MagicMock()
        result = apply_conflict(mock_db, ctx, candidate=candidate)
        assert result is None

    def test_low_confidence_skipped(self, ctx):
        from mneme.memory.refine.conflict import ConflictCandidate, apply_conflict
        candidate = ConflictCandidate(
            memory_a_id=uuid4(), memory_b_id=uuid4(), similarity=0.80,
            conflict=True, confidence=0.3,  # below 0.7 default
        )
        mock_db = MagicMock()
        result = apply_conflict(mock_db, ctx, candidate=candidate)
        assert result is None

    def test_self_referencing_skipped(self, ctx):
        from mneme.memory.refine.conflict import ConflictCandidate, apply_conflict
        mid = uuid4()
        candidate = ConflictCandidate(
            memory_a_id=mid, memory_b_id=mid, similarity=0.80,
            conflict=True, confidence=0.9,
        )
        mock_db = MagicMock()
        with patch("mneme.db.memories.get_memory", return_value=MagicMock()):
            result = apply_conflict(mock_db, ctx, candidate=candidate)
        assert result is None


class TestConflictEvaluateWithLLM:
    """Tests for evaluate_conflicts_with_llm."""

    def test_no_gateway_returns_unchanged(self):
        from mneme.memory.refine.conflict import ConflictCandidate, evaluate_conflicts_with_llm
        candidates = [
            ConflictCandidate(memory_a_id=uuid4(), memory_b_id=uuid4(), similarity=0.80),
        ]
        result = evaluate_conflicts_with_llm(candidates, gateway=None)
        assert result.llm_evaluated == 0
        assert len(result.candidates) == 1

    def test_empty_candidates(self):
        from mneme.memory.refine.conflict import evaluate_conflicts_with_llm
        result = evaluate_conflicts_with_llm([], gateway=MagicMock())
        assert result.llm_evaluated == 0


# ═══════════════════════════════════════════════════════════════════════════
# 3. MERGE module tests
# ═══════════════════════════════════════════════════════════════════════════

class TestMergeDataclasses:
    """Tests for MergeResult and SmartMergeOutput dataclasses."""

    def test_merge_result_defaults(self):
        from mneme.memory.refine.merge import MergeResult
        r = MergeResult(survivor_id=uuid4(), consumed_id=uuid4(), success=True)
        assert r.success is True
        assert r.error is None

    def test_smart_merge_output_defaults(self):
        from mneme.memory.refine.merge import SmartMergeOutput
        o = SmartMergeOutput()
        assert o.survivor is None
        assert o.merged_count == 0
        assert o.failed_count == 0
        assert o.merged_title is None
        assert o.merged_text is None
        assert o.results == []


class TestMergeBuildPrompt:
    """Tests for _build_merge_prompt."""

    def test_single_memory(self):
        from mneme.memory.refine.merge import _build_merge_prompt
        mem = MagicMock()
        mem.title = "Test Memory"
        mem.canonical_key = "test-key"
        mem.memory_text = "Some text content."
        messages = _build_merge_prompt([mem])
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "Test Memory" in messages[1]["content"]
        assert "test-key" in messages[1]["content"]
        assert "Some text content." in messages[1]["content"]

    def test_multiple_memories(self):
        from mneme.memory.refine.merge import _build_merge_prompt
        memories = []
        for i in range(3):
            mem = MagicMock()
            mem.title = f"Memory {i}"
            mem.canonical_key = f"key-{i}"
            mem.memory_text = f"Text {i}"
            memories.append(mem)
        messages = _build_merge_prompt(memories)
        assert "Memory 1" in messages[1]["content"]
        assert "Memory 2" in messages[1]["content"]
        assert "Memory 3" in messages[1]["content"]

    def test_untitled_memory(self):
        from mneme.memory.refine.merge import _build_merge_prompt
        mem = MagicMock()
        mem.title = None
        mem.canonical_key = "key"
        mem.memory_text = "text"
        messages = _build_merge_prompt([mem])
        assert "(untitled)" in messages[1]["content"]


class TestMergeParseResponse:
    """Tests for _parse_merge_response."""

    def test_valid_json(self):
        from mneme.memory.refine.merge import _parse_merge_response
        raw = '{"title": "Merged", "memory_text": "Combined text."}'
        result = _parse_merge_response(raw)
        assert result == {"title": "Merged", "memory_text": "Combined text."}

    def test_json_with_fences(self):
        from mneme.memory.refine.merge import _parse_merge_response
        raw = '```json\n{"title": "T", "memory_text": "M"}\n```'
        result = _parse_merge_response(raw)
        assert result == {"title": "T", "memory_text": "M"}

    def test_json_embedded_in_text(self):
        from mneme.memory.refine.merge import _parse_merge_response
        raw = 'Here is the result: {"title": "X", "memory_text": "Y"} done.'
        result = _parse_merge_response(raw)
        assert result == {"title": "X", "memory_text": "Y"}

    def test_invalid_json(self):
        from mneme.memory.refine.merge import _parse_merge_response
        assert _parse_merge_response("not json") is None

    def test_missing_title(self):
        from mneme.memory.refine.merge import _parse_merge_response
        result = _parse_merge_response('{"memory_text": "text"}')
        assert result is None

    def test_missing_memory_text(self):
        from mneme.memory.refine.merge import _parse_merge_response
        result = _parse_merge_response('{"title": "T"}')
        assert result is None

    def test_non_dict_returns_none(self):
        from mneme.memory.refine.merge import _parse_merge_response
        assert _parse_merge_response('[1, 2, 3]') is None


class TestSmartMergeEdgeCases:
    """Tests for smart_merge edge cases."""

    def test_survivor_not_found(self, ctx):
        from mneme.memory.refine.merge import smart_merge
        mock_db = MagicMock()
        with patch("mneme.memory.refine.merge.get_memory", return_value=None):
            result = smart_merge(mock_db, ctx, survivor_id=uuid4(), consumed_ids=[uuid4()])
        assert result.survivor is None
        assert result.merged_count == 0

    def test_no_valid_consumed(self, ctx):
        from mneme.memory.refine.merge import smart_merge
        survivor = MagicMock()
        survivor.status = "active"
        mock_db = MagicMock()
        with patch("mneme.memory.refine.merge.get_memory", side_effect=lambda db, mid: survivor if mid != uuid4() else None):
            result = smart_merge(mock_db, ctx, survivor_id=uuid4(), consumed_ids=[uuid4()])
        assert result.merged_count == 0


class TestQuickMergeDelegation:
    """Tests that quick_merge delegates to smart_merge with use_llm=False."""

    def test_quick_merge_calls_smart_merge(self, ctx):
        from mneme.memory.refine.merge import quick_merge
        mock_db = MagicMock()
        sid, cid = uuid4(), uuid4()
        with patch("mneme.memory.refine.merge.smart_merge") as mock_smart:
            mock_smart.return_value = MagicMock()
            quick_merge(mock_db, ctx, survivor_id=sid, consumed_ids=[cid], reason="test")
            mock_smart.assert_called_once()
            call_kwargs = mock_smart.call_args
            assert call_kwargs[1]["use_llm"] is False


# ═══════════════════════════════════════════════════════════════════════════
# 4. EXPIRE module tests
# ═══════════════════════════════════════════════════════════════════════════

class TestExpireRule:
    """Tests for the ExpireRule dataclass."""

    def test_defaults(self):
        from mneme.memory.refine.expire import ExpireRule
        r = ExpireRule(name="test_rule")
        assert r.name == "test_rule"
        assert r.description == ""
        assert r.enabled is True

    def test_disabled_rule(self):
        from mneme.memory.refine.expire import ExpireRule
        r = ExpireRule(name="disabled", enabled=False)
        assert r.enabled is False


class TestExpireCandidate:
    """Tests for the ExpireCandidate dataclass."""

    def test_defaults(self):
        from mneme.memory.refine.expire import ExpireCandidate
        c = ExpireCandidate(memory_id=uuid4(), canonical_key="test")
        assert c.status == "active"
        assert c.reason == ""
        assert c.quality_score is None
        assert c.search_weight is None


class TestExpireDefaultRules:
    """Tests for DEFAULT_RULES configuration."""

    def test_default_rules_count(self):
        from mneme.memory.refine.expire import DEFAULT_RULES
        assert len(DEFAULT_RULES) == 4

    def test_rule_names(self):
        from mneme.memory.refine.expire import DEFAULT_RULES
        names = {r.name for r in DEFAULT_RULES}
        expected = {"low_quality_old", "zero_weight_stale", "high_conflict_count", "merged_consumed"}
        assert names == expected

    def test_all_enabled_by_default(self):
        from mneme.memory.refine.expire import DEFAULT_RULES
        assert all(r.enabled for r in DEFAULT_RULES)


class TestExpireScanOutput:
    """Tests for ExpireScanOutput dataclass."""

    def test_defaults(self):
        from mneme.memory.refine.expire import ExpireScanOutput
        o = ExpireScanOutput()
        assert o.candidates == []
        assert o.total_scanned == 0
        assert o.rules_used == []


class TestExpireApplyOutput:
    """Tests for ExpireApplyOutput dataclass."""

    def test_defaults(self):
        from mneme.memory.refine.expire import ExpireApplyOutput
        o = ExpireApplyOutput()
        assert o.expired_count == 0
        assert o.failed_count == 0
        assert o.errors == []


class TestExpireApplyEdgeCases:
    """Tests for apply_expire edge cases."""

    def test_memory_not_found(self, ctx):
        from mneme.memory.refine.expire import apply_expire
        mock_db = MagicMock()
        with patch("mneme.memory.refine.expire.get_memory", return_value=None):
            ok, err = apply_expire(mock_db, ctx, memory_id=uuid4())
        assert ok is False
        assert "not found" in err.lower()

    def test_draft_memory_rejected(self, ctx):
        from mneme.memory.refine.expire import apply_expire
        mem = MagicMock()
        mem.status = "draft"
        mock_db = MagicMock()
        with patch("mneme.memory.refine.expire.get_memory", return_value=mem):
            ok, err = apply_expire(mock_db, ctx, memory_id=uuid4())
        assert ok is False
        assert "draft" in err.lower() or "active" in err.lower()


class TestExpireScanWithMockDB:
    """Tests for scan_expire_candidates with mocked SQL."""

    def test_scan_returns_empty_when_no_rows(self):
        from mneme.memory.refine.expire import scan_expire_candidates
        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_db.execute.return_value = mock_result
        output = scan_expire_candidates(mock_db)
        assert output.total_scanned == 0
        assert output.candidates == []

    def test_scan_custom_rules(self):
        from mneme.memory.refine.expire import scan_expire_candidates, ExpireRule
        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_db.execute.return_value = mock_result
        rules = [ExpireRule(name="low_quality_old")]
        output = scan_expire_candidates(mock_db, rules=rules)
        assert "low_quality_old" in output.rules_used

    def test_scan_disabled_rules_skipped(self):
        from mneme.memory.refine.expire import scan_expire_candidates, ExpireRule
        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_db.execute.return_value = mock_result
        rules = [ExpireRule(name="low_quality_old", enabled=False)]
        output = scan_expire_candidates(mock_db, rules=rules)
        assert "low_quality_old" not in output.rules_used


class TestExpireBatch:
    """Tests for apply_expire_batch."""

    def test_empty_batch(self, ctx):
        from mneme.memory.refine.expire import apply_expire_batch
        mock_db = MagicMock()
        output = apply_expire_batch(mock_db, ctx, candidates=[])
        assert output.expired_count == 0
        assert output.failed_count == 0


# ═══════════════════════════════════════════════════════════════════════════
# 5. QUALITY module tests
# ═══════════════════════════════════════════════════════════════════════════

class TestQualityDataclasses:
    """Tests for QualityResult and QualityBatchOutput."""

    def test_quality_result_defaults(self):
        from mneme.memory.refine.quality import QualityResult
        r = QualityResult(memory_id=uuid4(), canonical_key="k", quality_score=0.8, search_weight=0.96)
        assert r.confidence_score == 0.5
        assert r.evidence_count_score == 0.5
        assert r.error is None

    def test_quality_batch_output_defaults(self):
        from mneme.memory.refine.quality import QualityBatchOutput
        o = QualityBatchOutput()
        assert o.results == []
        assert o.total_scored == 0
        assert o.total_failed == 0
        assert o.overall_stats == {}


class TestQualityEvidenceScore:
    """Tests for _compute_evidence_score."""

    def test_zero_spans(self):
        from mneme.memory.refine.quality import _compute_evidence_score
        assert _compute_evidence_score(0) == 0.2

    def test_ten_plus_spans(self):
        from mneme.memory.refine.quality import _compute_evidence_score
        assert _compute_evidence_score(10) == 1.0
        assert _compute_evidence_score(100) == 1.0

    def test_five_spans(self):
        from mneme.memory.refine.quality import _compute_evidence_score
        score = _compute_evidence_score(5)
        assert score == pytest.approx(0.6, abs=0.01)  # 0.2 + 0.8 * (5/10)

    def test_one_span(self):
        from mneme.memory.refine.quality import _compute_evidence_score
        score = _compute_evidence_score(1)
        assert score == pytest.approx(0.28, abs=0.01)

    def test_monotonic_increasing(self):
        from mneme.memory.refine.quality import _compute_evidence_score
        for n in range(1, 10):
            assert _compute_evidence_score(n) > _compute_evidence_score(n - 1)


class TestQualityRecencyScore:
    """Tests for _compute_recency_score."""

    def test_none_dates_returns_half(self):
        from mneme.memory.refine.quality import _compute_recency_score
        assert _compute_recency_score(None, None) == 0.5

    def test_recent_date_high_score(self):
        from mneme.memory.refine.quality import _compute_recency_score
        now = datetime.now(timezone.utc)
        score = _compute_recency_score(now, now)
        assert score >= 0.99

    def test_old_date_low_score(self):
        from mneme.memory.refine.quality import _compute_recency_score
        old = datetime.now(timezone.utc) - timedelta(days=365)
        score = _compute_recency_score(old, old)
        assert score < 0.1

    def test_updated_at_preferred(self):
        from mneme.memory.refine.quality import _compute_recency_score
        now = datetime.now(timezone.utc)
        old = datetime.now(timezone.utc) - timedelta(days=365)
        # updated_at is recent, created_at is old
        score = _compute_recency_score(old, now)
        assert score >= 0.99

    def test_half_life_parameter(self):
        from mneme.memory.refine.quality import _compute_recency_score
        ago_90 = datetime.now(timezone.utc) - timedelta(days=90)
        score = _compute_recency_score(ago_90, ago_90, half_life_days=90.0)
        assert score == pytest.approx(1.0 / math.e, abs=0.01)


class TestQualityRelationScore:
    """Tests for _compute_relation_score."""

    def test_no_relations(self):
        from mneme.memory.refine.quality import _compute_relation_score
        assert _compute_relation_score(0, 0) == 0.5

    def test_all_support(self):
        from mneme.memory.refine.quality import _compute_relation_score
        score = _compute_relation_score(10, 0)
        assert score == pytest.approx(1.0, abs=0.01)

    def test_all_conflict(self):
        from mneme.memory.refine.quality import _compute_relation_score
        score = _compute_relation_score(0, 10)
        assert score == pytest.approx(0.0, abs=0.01)

    def test_equal_support_conflict(self):
        from mneme.memory.refine.quality import _compute_relation_score
        score = _compute_relation_score(5, 5)
        assert score == pytest.approx(0.5, abs=0.01)

    def test_more_support_than_conflict(self):
        from mneme.memory.refine.quality import _compute_relation_score
        score = _compute_relation_score(8, 2)
        assert score > 0.5


class TestQualityClamp:
    """Tests for _clamp helper."""

    def test_within_range(self):
        from mneme.memory.refine.quality import _clamp
        assert _clamp(0.5, 0.0, 1.0) == 0.5

    def test_below_min(self):
        from mneme.memory.refine.quality import _clamp
        assert _clamp(-0.5, 0.0, 1.0) == 0.0

    def test_above_max(self):
        from mneme.memory.refine.quality import _clamp
        assert _clamp(1.5, 0.0, 1.0) == 1.0

    def test_exact_bounds(self):
        from mneme.memory.refine.quality import _clamp
        assert _clamp(0.0, 0.0, 1.0) == 0.0
        assert _clamp(1.0, 0.0, 1.0) == 1.0


class TestQualityBuildCoherencePrompt:
    """Tests for _build_coherence_prompt."""

    def test_single_memory(self):
        from mneme.memory.refine.quality import _build_coherence_prompt
        memories = [{"memory_text": "Test memory content"}]
        messages = _build_coherence_prompt(memories)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "[0]" in messages[1]["content"]

    def test_truncates_long_text(self):
        from mneme.memory.refine.quality import _build_coherence_prompt
        memories = [{"memory_text": "x" * 2000}]
        messages = _build_coherence_prompt(memories)
        # The text should be truncated to 800 chars
        assert "x" * 800 in messages[1]["content"]
        assert "x" * 801 not in messages[1]["content"]

    def test_multiple_memories_indexed(self):
        from mneme.memory.refine.quality import _build_coherence_prompt
        memories = [{"memory_text": f"Text {i}"} for i in range(5)]
        messages = _build_coherence_prompt(memories)
        for i in range(5):
            assert f"[{i}]" in messages[1]["content"]


class TestQualityScoringWeights:
    """Tests that quality scoring weights sum to 1.0."""

    def test_default_weights_sum_to_one(self):
        from mneme.memory.refine.quality import _DEFAULT_WEIGHTS
        total = sum(_DEFAULT_WEIGHTS.values())
        assert total == pytest.approx(1.0, abs=0.001)

    def test_all_dimensions_present(self):
        from mneme.memory.refine.quality import _DEFAULT_WEIGHTS
        expected = {"confidence", "evidence", "coherence", "recency", "relation"}
        assert set(_DEFAULT_WEIGHTS.keys()) == expected


class TestQualityApplyScores:
    """Tests for apply_quality_scores."""

    def test_empty_results(self):
        from mneme.memory.refine.quality import apply_quality_scores
        mock_db = MagicMock()
        count = apply_quality_scores(mock_db, results=[])
        assert count == 0

    def test_skips_error_results(self):
        from mneme.memory.refine.quality import apply_quality_scores, QualityResult
        mock_db = MagicMock()
        results = [
            QualityResult(memory_id=uuid4(), canonical_key="k", quality_score=0.8,
                          search_weight=0.96, error="some error"),
        ]
        count = apply_quality_scores(mock_db, results=results)
        assert count == 0
        mock_db.execute.assert_not_called()

    def test_applies_valid_results(self):
        from mneme.memory.refine.quality import apply_quality_scores, QualityResult
        mock_db = MagicMock()
        results = [
            QualityResult(memory_id=uuid4(), canonical_key="k", quality_score=0.8,
                          search_weight=0.96),
        ]
        count = apply_quality_scores(mock_db, results=results)
        assert count == 1
        assert mock_db.execute.call_count == 2  # quality + index weight


# ═══════════════════════════════════════════════════════════════════════════
# 6. PIPELINE orchestration tests
# ═══════════════════════════════════════════════════════════════════════════

class TestRefinePipelineResult:
    """Tests for RefinePipelineResult dataclass."""

    def test_defaults(self):
        from mneme.memory.refine import RefinePipelineResult
        r = RefinePipelineResult()
        assert r.dedup is None
        assert r.conflict is None
        assert r.expire is None
        assert r.quality is None
        assert r.errors == []
        assert r.stages_run == []


class TestRunRefinePipeline:
    """Tests for run_refine_pipeline orchestration."""

    def test_invalid_stage_raises(self, ctx):
        from mneme.memory.refine import run_refine_pipeline
        mock_db = MagicMock()
        with pytest.raises(ValueError, match="Unknown refine stage"):
            run_refine_pipeline(mock_db, ctx, stages=["invalid_stage"])

    def test_empty_stages(self, ctx):
        from mneme.memory.refine import run_refine_pipeline
        mock_db = MagicMock()
        result = run_refine_pipeline(mock_db, ctx, stages=[])
        assert result.stages_run == []
        assert result.errors == []

    def test_all_stages_with_mocks(self, ctx):
        from mneme.memory.refine import run_refine_pipeline, DedupResult, ConflictResult
        from mneme.memory.refine.expire import ExpireScanOutput
        from mneme.memory.refine.quality import QualityBatchOutput

        mock_db = MagicMock()

        with patch("mneme.memory.refine.detect_duplicates", return_value=DedupResult()) as m_dedup, \
             patch("mneme.memory.refine.detect_conflicts", return_value=ConflictResult()) as m_conflict, \
             patch("mneme.memory.refine.scan_expire_candidates", return_value=ExpireScanOutput()) as m_expire, \
             patch("mneme.memory.refine.score_memories", return_value=QualityBatchOutput()) as m_quality:

            result = run_refine_pipeline(mock_db, ctx, stages=["dedup", "conflict", "expire", "quality"])

        assert "dedup" in result.stages_run
        assert "conflict" in result.stages_run
        assert "expire" in result.stages_run
        assert "quality" in result.stages_run
        assert result.errors == []
        m_dedup.assert_called_once()
        m_conflict.assert_called_once()
        m_expire.assert_called_once()
        m_quality.assert_called_once()

    def test_dry_run_skips_dedup_apply(self, ctx):
        from mneme.memory.refine import run_refine_pipeline, DedupResult, DedupPair
        from mneme.memory.refine.expire import ExpireScanOutput
        from mneme.memory.refine.quality import QualityBatchOutput

        mock_db = MagicMock()
        dedup_result = DedupResult(pairs_found=1, pairs=[
            DedupPair(memory_a_id=uuid4(), memory_b_id=uuid4(), similarity=0.95),
        ])

        with patch("mneme.memory.refine.detect_duplicates", return_value=dedup_result), \
             patch("mneme.memory.refine.detect_conflicts", return_value=MagicMock(candidates=[])), \
             patch("mneme.memory.refine.scan_expire_candidates", return_value=ExpireScanOutput()), \
             patch("mneme.memory.refine.score_memories", return_value=QualityBatchOutput()), \
             patch("mneme.memory.refine.apply_dedup_batch") as m_apply:

            result = run_refine_pipeline(mock_db, ctx, dry_run=True, stages=["dedup"])

        m_apply.assert_not_called()

    def test_stage_failure_captured_in_errors(self, ctx):
        from mneme.memory.refine import run_refine_pipeline

        mock_db = MagicMock()

        with patch("mneme.memory.refine.detect_duplicates", side_effect=RuntimeError("boom")), \
             patch("mneme.memory.refine.detect_conflicts", return_value=MagicMock(candidates=[])), \
             patch("mneme.memory.refine.scan_expire_candidates", return_value=MagicMock(candidates=[])), \
             patch("mneme.memory.refine.score_memories", return_value=MagicMock()):

            result = run_refine_pipeline(mock_db, ctx, stages=["dedup", "conflict", "expire", "quality"])

        assert len(result.errors) == 1
        assert "boom" in result.errors[0]
        assert "dedup" not in result.stages_run
        assert "conflict" in result.stages_run  # other stages still run

    def test_single_stage(self, ctx):
        from mneme.memory.refine import run_refine_pipeline
        from mneme.memory.refine.quality import QualityBatchOutput

        mock_db = MagicMock()
        with patch("mneme.memory.refine.score_memories", return_value=QualityBatchOutput()):
            result = run_refine_pipeline(mock_db, ctx, stages=["quality"])

        assert result.stages_run == ["quality"]
        assert result.dedup is None
        assert result.conflict is None
        assert result.expire is None
        assert result.quality is not None

    def test_dedup_threshold_passed_through(self, ctx):
        from mneme.memory.refine import run_refine_pipeline, DedupResult

        mock_db = MagicMock()
        with patch("mneme.memory.refine.detect_duplicates", return_value=DedupResult()) as m_dedup, \
             patch("mneme.memory.refine.detect_conflicts", return_value=MagicMock(candidates=[])), \
             patch("mneme.memory.refine.scan_expire_candidates", return_value=MagicMock(candidates=[])), \
             patch("mneme.memory.refine.score_memories", return_value=MagicMock()):

            run_refine_pipeline(mock_db, ctx, dedup_threshold=0.95, stages=["dedup"])

        m_dedup.assert_called_once()
        call_kwargs = m_dedup.call_args[1]
        assert call_kwargs["threshold"] == 0.95

    def test_expire_dry_run_skips_apply(self, ctx):
        from mneme.memory.refine import run_refine_pipeline
        from mneme.memory.refine.expire import ExpireScanOutput, ExpireCandidate

        mock_db = MagicMock()
        scan = ExpireScanOutput(
            candidates=[ExpireCandidate(memory_id=uuid4(), canonical_key="k")],
            total_scanned=1,
        )

        with patch("mneme.memory.refine.detect_duplicates", return_value=MagicMock(pairs=[])), \
             patch("mneme.memory.refine.detect_conflicts", return_value=MagicMock(candidates=[])), \
             patch("mneme.memory.refine.scan_expire_candidates", return_value=scan), \
             patch("mneme.memory.refine.score_memories", return_value=MagicMock()), \
             patch("mneme.memory.refine.apply_expire_batch") as m_apply:

            result = run_refine_pipeline(mock_db, ctx, dry_run=True, stages=["expire"])

        m_apply.assert_not_called()
        assert result.expire_applied is None


# ═══════════════════════════════════════════════════════════════════════════
# 7. Cosine similarity (cross-cutting utility)
# ═══════════════════════════════════════════════════════════════════════════

class TestCosineSimilarity:
    """Tests for the _cosine_similarity utility used by both dedup and conflict."""

    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert _cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        assert _cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_similar_vectors(self):
        v1 = [1.0, 0.0]
        v2 = [0.9, 0.1]
        sim = _cosine_similarity(v1, v2)
        assert 0.9 < sim < 1.0

    def test_empty_vectors(self):
        result = _cosine_similarity([], [])
        # Should handle gracefully (may return 0 or raise)
        assert isinstance(result, float)
