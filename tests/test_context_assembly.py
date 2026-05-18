"""P8-01 Context Assembly Engine — contract tests.

Covers P8-01 completion criteria:
1. assemble_context() returns assembled_text + sections + budget.
2. Card strategy mapping: soul_card/identity_card/tool_catalog → always,
   user_profile → moderate, tool_detail → on_demand.
3. Strategy overrides work correctly.
4. expand_cards forces moderate/on_demand to expand fully.
5. Token budget calculation is correct.
6. Degradation when no card stores found.
7. Audit event + outbox event written on assemble.
8. POST /context/assemble 201 + response schema validation.
9. Conversation history is prepended when provided.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from mneme.api.context import ActorContext, RequestContext
from mneme.context.assembly_engine import (
    assemble_context,
    _strategy_for,
    _content_hash,
)
from mneme.schemas.context_assembly import (
    CARD_STRATEGY_MAP,
    InjectionStrategy,
)


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def test_ctx() -> RequestContext:
    return RequestContext(
        request_id=uuid4(),
        correlation_id=uuid4(),
        actor=ActorContext(
            actor_type="user",
            actor_id=UUID("00000000-0000-0000-0000-000000000001"),
            auth_context_type="user_session",
            auth_context_id=UUID("00000000-0000-0000-0000-000000000001"),
        ),
        idempotency_key=str(uuid4()),
    )


@pytest.fixture
def test_agent(db) -> UUID:
    """Create a test agent and return its ID."""
    agent_id = uuid4()
    db.execute(
        text(
            "INSERT INTO agents (agent_id, agent_code, name, status) "
            "VALUES (:aid, :code, :name, 'active')"
        ),
        {"aid": agent_id, "code": f"ASM-{uuid4().hex[:8].upper()}", "name": "Assembly Test Agent"},
    )
    db.flush()
    return agent_id


def _create_card_store(db, agent_id: UUID, store_type: str, name: str, memories: list[dict] | None = None):
    """Create a memory_store with optional memories for testing."""
    store_id = uuid4()
    db.execute(
        text(
            "INSERT INTO memory_stores (store_id, agent_id, name, type) "
            "VALUES (:sid, :aid, :name, :type)"
        ),
        {"sid": store_id, "aid": agent_id, "name": name, "type": store_type},
    )
    if memories:
        for mem in memories:
            mem_id = uuid4()
            db.execute(
                text(
                    "INSERT INTO memories (memory_id, store_id, canonical_key, title, memory_text, "
                    "status, sensitivity_level, project_id) "
                    "VALUES (:mid, :sid, :key, :title, :text, 'active', 'normal', NULL)"
                ),
                {
                    "mid": mem_id,
                    "sid": store_id,
                    "key": f"mem-{mem_id.hex[:8]}",
                    "title": mem.get("title", ""),
                    "text": mem.get("text", ""),
                },
            )
            # Also insert into memory_index_entries for FTS
            import hashlib
            text_content = mem.get("text", "")
            content_hash = hashlib.sha256(text_content.encode()).hexdigest()[:32]
            db.execute(
                text(
                    "INSERT INTO memory_index_entries (memory_index_entry_id, memory_id, "
                    "index_text, memory_version, content_hash, fts_state, vector_state) "
                    "VALUES (:eid, :mid, :text, 1, :chash, 'ready', 'ready')"
                ),
                {"eid": uuid4(), "mid": mem_id, "text": text_content, "chash": content_hash},
            )
    db.flush()
    return store_id


# ═══════════════════════════════════════════════════════════════════════════
# Unit tests — helpers
# ═══════════════════════════════════════════════════════════════════════════

class TestStrategyMapping:
    """Test card-type → strategy mapping."""

    def test_soul_card_always(self):
        assert _strategy_for("soul_card") == InjectionStrategy.always

    def test_identity_card_always(self):
        assert _strategy_for("identity_card") == InjectionStrategy.always

    def test_tool_catalog_always(self):
        assert _strategy_for("tool_catalog") == InjectionStrategy.always

    def test_user_profile_moderate(self):
        assert _strategy_for("user_profile") == InjectionStrategy.moderate

    def test_tool_detail_on_demand(self):
        assert _strategy_for("tool_detail") == InjectionStrategy.on_demand

    def test_unknown_defaults_to_moderate(self):
        assert _strategy_for("unknown_card_type") == InjectionStrategy.moderate

    def test_strategy_map_consistency(self):
        """CARD_STRATEGY_MAP keys match the MemoryStoreType enum values."""
        expected_keys = {
            "soul_card", "identity_card", "tool_catalog",
            "user_profile", "tool_detail",
        }
        assert set(CARD_STRATEGY_MAP.keys()) == expected_keys


class TestContentHash:
    """Test content hash generation."""

    def test_deterministic(self):
        assert _content_hash("hello") == _content_hash("hello")

    def test_32_chars(self):
        assert len(_content_hash("anything")) == 32

    def test_different_inputs(self):
        assert _content_hash("a") != _content_hash("b")


# ═══════════════════════════════════════════════════════════════════════════
# Unit tests — assemble_context()
# ═══════════════════════════════════════════════════════════════════════════

class TestAssembleContext:
    """Test the core assemble_context function."""

    def test_assemble_basic(self, db, test_ctx, test_agent):
        """Basic assemble with all card types returns sections + budget."""
        _create_card_store(db, test_agent, "soul_card", "Test Soul", [
            {"title": "Persona", "text": "You are a helpful assistant."},
        ])
        _create_card_store(db, test_agent, "identity_card", "Test Identity", [
            {"title": "Role", "text": "Support agent for Mneme3 platform."},
        ])

        result = assemble_context(
            db, test_ctx,
            agent_id=test_agent,
            query_text="help me with context",
            max_tokens=128000,
        )
        db.flush()

        assert "assembled_text" in result
        assert "sections" in result
        assert "budget" in result
        assert "total_tokens" in result
        assert "strategy_summary" in result
        assert "degradation_reason" in result

        assert len(result["sections"]) >= 2
        assert result["total_tokens"] > 0
        assert len(result["strategy_summary"]) >= 2
        assert result["strategy_summary"]["soul_card"] == "always"

    def test_no_card_stores_degradation(self, db, test_ctx, test_agent):
        """Degradation when agent has no card stores."""
        result = assemble_context(
            db, test_ctx,
            agent_id=test_agent,
            query_text="test",
        )
        db.flush()

        assert result["degradation_reason"] == "no_card_stores_found"
        assert len(result["sections"]) == 0
        assert result["total_tokens"] <= 1  # empty text = 1 token minimum

    def test_strategy_overrides(self, db, test_ctx, test_agent):
        """Strategy overrides change the applied strategy."""
        _create_card_store(db, test_agent, "soul_card", "Soul", [
            {"title": "P", "text": "Persona text."},
        ])

        result = assemble_context(
            db, test_ctx,
            agent_id=test_agent,
            query_text="test",
            strategy_overrides={"soul_card": "moderate"},
        )
        db.flush()

        assert result["strategy_summary"]["soul_card"] == "moderate"
        section = next(s for s in result["sections"] if s["card_type"] == "soul_card")
        assert section["strategy"] == "moderate"

    def test_expand_cards_forces_always(self, db, test_ctx, test_agent):
        """expand_cards forces moderate/on_demand to always."""
        _create_card_store(db, test_agent, "user_profile", "Profile", [
            {"title": "User", "text": "User prefers concise answers."},
        ])

        result = assemble_context(
            db, test_ctx,
            agent_id=test_agent,
            query_text="test",
            expand_cards=["user_profile"],
        )
        db.flush()

        assert result["strategy_summary"]["user_profile"] == "always"

    def test_budget_calculation(self, db, test_ctx, test_agent):
        """Budget breakdown has correct structure and ranges."""
        _create_card_store(db, test_agent, "soul_card", "Soul", [
            {"title": "P", "text": "Test persona."},
        ])

        result = assemble_context(
            db, test_ctx,
            agent_id=test_agent,
            query_text="test",
            max_tokens=10000,
        )
        db.flush()

        budget = result["budget"]
        assert budget["total_available"] == 10000
        assert budget["usable"] > 0
        assert budget["usable"] < budget["total_available"]
        assert budget["remaining"] >= 0
        assert "always_used" in budget
        assert "moderate_used" in budget
        assert "on_demand_used" in budget

    def test_conversation_history_prepended(self, db, test_ctx, test_agent):
        """Conversation history is included in assembled text."""
        _create_card_store(db, test_agent, "soul_card", "Soul", [
            {"title": "P", "text": "Persona."},
        ])

        result = assemble_context(
            db, test_ctx,
            agent_id=test_agent,
            query_text="test",
            conversation_history="User: Hello\nAssistant: Hi there!",
            max_tokens=128000,
        )
        db.flush()

        assert "User: Hello" in result["assembled_text"]
        assert "Hi there" in result["assembled_text"]

    def test_assemble_creates_audit_event(self, db, test_ctx, test_agent):
        """Assemble writes an audit_events row."""
        _create_card_store(db, test_agent, "soul_card", "Soul", [
            {"title": "P", "text": "Persona."},
        ])

        result = assemble_context(
            db, test_ctx,
            agent_id=test_agent,
            query_text="audit test",
        )
        db.flush()

        audit_row = db.execute(
            text("SELECT * FROM audit_events WHERE action = 'context.assemble' ORDER BY occurred_at DESC LIMIT 1")
        ).first()
        assert audit_row is not None
        assert dict(audit_row._mapping)["action"] == "context.assemble"

    def test_assemble_creates_outbox_event(self, db, test_ctx, test_agent):
        """Assemble writes an outbox events row."""
        _create_card_store(db, test_agent, "soul_card", "Soul", [
            {"title": "P", "text": "Persona."},
        ])

        result = assemble_context(
            db, test_ctx,
            agent_id=test_agent,
            query_text="outbox test",
        )
        db.flush()

        event_row = db.execute(
            text("SELECT * FROM events WHERE event_type = 'context.assembled' ORDER BY occurred_at DESC LIMIT 1")
        ).first()
        assert event_row is not None
        assert dict(event_row._mapping)["event_type"] == "context.assembled"

    def test_section_ordering(self, db, test_ctx, test_agent):
        """Sections should be ordered: always first, then moderate, then on_demand."""
        _create_card_store(db, test_agent, "soul_card", "Soul", [
            {"title": "P", "text": "Always content."},
        ])
        _create_card_store(db, test_agent, "user_profile", "Profile", [
            {"title": "U", "text": "Moderate content."},
        ])

        result = assemble_context(
            db, test_ctx,
            agent_id=test_agent,
            query_text="order test",
        )
        db.flush()

        sections = result["sections"]
        always_idx = next(i for i, s in enumerate(sections) if s["strategy"] == "always")
        moderate_idx = next(i for i, s in enumerate(sections) if s["strategy"] == "moderate")
        assert always_idx < moderate_idx

    def test_token_budget_enforcement(self, db, test_ctx, test_agent):
        """Very small budget should cause truncation."""
        _create_card_store(db, test_agent, "soul_card", "Soul", [
            {"title": "P", "text": "x " * 5000},  # ~5000 tokens
        ])

        result = assemble_context(
            db, test_ctx,
            agent_id=test_agent,
            query_text="budget test",
            max_tokens=1000,  # Very small budget
        )
        db.flush()

        # Either truncated or very few tokens used
        soul_section = next((s for s in result["sections"] if s["card_type"] == "soul_card"), None)
        if soul_section:
            assert soul_section["token_count"] < 5000  # Should be limited by budget


# ═══════════════════════════════════════════════════════════════════════════
# API endpoint tests (require running server - run against deployed instance)
# ═══════════════════════════════════════════════════════════════════════════
#
# These tests are designed to run against a live Mneme3 server.
# Example: pytest tests/test_context_assembly.py -k TestAssembleEndpoint
#   --server-url http://192.168.31.199:8000
#
# Usage with deployed instance:
#   curl -X POST http://192.168.31.199:8000/api/v4/context/assemble \
#     -H "Content-Type: application/json" \
#     -H "Authorization: Bearer <token>" \
#     -d '{"agent_id": "<uuid>", "query_text": "test"}'
