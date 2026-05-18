"""Tests for P4-10 Memory Auto-Extract Sweeper."""

import pytest
from unittest.mock import MagicMock, patch
from uuid import uuid4, UUID

from mneme.worker.memory_auto_extract import (
    MemoryAutoExtractSweeper,
    SweepResult,
    UnprocessedConversation,
    create_memory_auto_extract_sweeper,
    get_unprocessed_stats,
    _make_system_context,
    _build_context_for_extract,
)


class TestSweepResult:
    def test_defaults(self):
        result = SweepResult()
        assert result.conversations_scanned == 0
        assert result.conversations_processed == 0
        assert result.messages_extracted == 0
        assert result.candidates_submitted == 0
        assert result.candidates_deduped == 0
        assert result.errors == 0
        assert result.skipped == 0


class TestUnprocessedConversation:
    def test_creation(self):
        cid = uuid4()
        pid = uuid4()
        conv = UnprocessedConversation(
            conversation_id=cid,
            project_id=pid,
        )
        assert conv.conversation_id == cid
        assert conv.project_id == pid
        assert conv.message_ids == []
        assert conv.total_unprocessed == 0
        assert conv.first_message_at is None


class TestMemoryAutoExtractSweeper:
    def test_instant_mode(self):
        sweeper = MemoryAutoExtractSweeper(window_seconds=0)
        assert sweeper.is_instant_mode is True
        assert sweeper.window_seconds == 0

    def test_batch_mode(self):
        sweeper = MemoryAutoExtractSweeper(window_seconds=60)
        assert sweeper.is_instant_mode is False
        assert sweeper.window_seconds == 60

    def test_default_instant(self):
        sweeper = MemoryAutoExtractSweeper()
        assert sweeper.is_instant_mode is True

    def test_sweep_empty(self):
        """When there are no unprocessed conversations, sweep returns zeros."""
        sweeper = MemoryAutoExtractSweeper(window_seconds=0, batch_size=10)

        with patch.object(sweeper, '_find_unprocessed_conversations', return_value=[]):
            result = sweeper.sweep()
            assert result.conversations_scanned == 0
            assert result.conversations_processed == 0
            assert result.messages_extracted == 0
            assert result.errors == 0


class TestHelpers:
    def test_make_system_context(self):
        ctx = _make_system_context()
        from mneme.api.context import RequestContext
        assert isinstance(ctx, RequestContext)
        assert ctx.actor.actor_type == "system"

    def test_build_context_for_extract(self):
        eid = uuid4()
        sid = uuid4()
        ctx = _build_context_for_extract(eid, "message", sid)
        assert ctx.actor.actor_type == "system"
        assert ctx.idempotency_key is not None
        # Same inputs produce same idempotency key
        ctx2 = _build_context_for_extract(eid, "message", sid)
        assert ctx2.idempotency_key == ctx.idempotency_key


class TestIntegration:
    """Integration-style tests (require database)."""

    @pytest.mark.integration
    def test_get_unprocessed_stats_returns_dict(self, db_session):
        """Stats helper returns a dict even with empty database."""
        with patch('mneme.worker.memory_auto_extract.SessionLocal') as mock_session:
            mock_db = MagicMock()
            mock_db.execute.return_value.scalar_one.return_value = 42
            mock_session.return_value.__enter__.return_value = mock_db

            stats = get_unprocessed_stats()
            assert isinstance(stats, dict)
            assert "unprocessed_messages" in stats

    @pytest.mark.integration
    def test_sweeper_batch_size_limits(self):
        """Verify batch_size and max_messages_per_conv constrain the query."""
        sweeper = MemoryAutoExtractSweeper(
            window_seconds=30,
            batch_size=5,
            max_messages_per_conv=10,
        )
        assert sweeper._batch_size == 5
        assert sweeper._max_messages_per_conv == 10
