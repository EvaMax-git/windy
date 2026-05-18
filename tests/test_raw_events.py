"""P4-03 Raw Events — contract tests (DB layer).

Covers P4-03 completion criteria:
1. Create raw event → payload_hash auto-computed, text_preview auto-extracted,
   idempotency_key auto-derived, retention_until = event_time + 365d, pii_flags = [].
2. Get raw event by ID.
3. List raw events with filters (conversation_id, event_source_id) and pagination.
4. Idempotency: duplicate idempotency_key returns existing event.
5. UNIQUE(event_source_id, payload_hash, event_time) dedup.
6. Error cases: not-found.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from mneme.api.context import ActorContext, RequestContext
from mneme.db.raw_events import (
    compute_payload_hash,
    create_raw_event,
    derive_idempotency_key,
    extract_text_preview,
    get_raw_event,
    list_raw_events,
)
from mneme.schemas.common import SensitivityLevel
from mneme.schemas.conversations import RawEventCreate, RawEventType


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def test_context() -> RequestContext:
    """Build a minimal RequestContext with a user actor."""
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
def test_project(db) -> UUID:
    """Create a project and return its ID."""
    project_id = uuid4()
    code = f"RAW-{uuid4().hex[:8].upper()}"
    db.execute(
        text(
            "INSERT INTO projects (project_id, project_code, name, status, sensitivity_default) "
            "VALUES (:pid, :code, :name, 'active', 'normal')"
        ),
        {"pid": project_id.hex, "code": code, "name": "P4-03 Test Project"},
    )
    db.flush()
    return project_id


@pytest.fixture(autouse=True)
def ensure_raw_events_table(db):
    """Ensure the raw_events table exists for SQLite."""
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS raw_events (
            raw_event_id TEXT PRIMARY KEY NOT NULL,
            project_id TEXT,
            event_source_id TEXT,
            conversation_id TEXT,
            message_id TEXT,
            raw_event_type TEXT NOT NULL,
            source_platform TEXT NOT NULL,
            external_event_id TEXT,
            event_time TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            text_preview TEXT,
            sensitivity_level TEXT NOT NULL DEFAULT 'private',
            pii_flags TEXT NOT NULL DEFAULT '[]',
            retention_until TEXT,
            import_run_id TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))
    db.flush()
    yield


# ═══════════════════════════════════════════════════════════════════════════
# Unit Tests — Helpers
# ═══════════════════════════════════════════════════════════════════════════


class TestHelpers:
    """Unit tests for helper functions."""

    def test_compute_payload_hash_deterministic(self):
        """Same payload produces same hash."""
        payload = {"a": 1, "b": 2}
        h1 = compute_payload_hash(payload)
        h2 = compute_payload_hash(payload)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex digest

    def test_compute_payload_hash_key_order_independent(self):
        """Hash is independent of key order (sorted keys)."""
        h1 = compute_payload_hash({"a": 1, "b": 2})
        h2 = compute_payload_hash({"b": 2, "a": 1})
        assert h1 == h2

    def test_compute_payload_hash_different_content(self):
        """Different content produces different hash."""
        h1 = compute_payload_hash({"text": "hello"})
        h2 = compute_payload_hash({"text": "world"})
        assert h1 != h2

    def test_extract_text_preview_from_text_key(self):
        """Extracts preview from 'text' key."""
        preview = extract_text_preview({"text": "Hello " * 100})
        assert preview is not None
        assert len(preview) <= 500
        assert preview.startswith("Hello ")

    def test_extract_text_preview_from_content_key(self):
        """Extracts preview from 'content' key."""
        preview = extract_text_preview({"content": "World content here"})
        assert preview == "World content here"

    def test_extract_text_preview_from_content_text_key(self):
        """Extracts preview from 'content_text' key."""
        preview = extract_text_preview({"content_text": "Some content"})
        assert preview == "Some content"

    def test_extract_text_preview_from_message_key(self):
        """Extracts preview from 'message' key."""
        preview = extract_text_preview({"message": "A message"})
        assert preview == "A message"

    def test_extract_text_preview_falls_back_to_json(self):
        """When no text key exists, falls back to JSON representation (first 500 chars)."""
        preview = extract_text_preview({"key": "value", "num": 42})
        assert preview is not None
        assert len(preview) <= 500

    def test_extract_text_preview_empty_payload(self):
        """Empty payload falls back to JSON representation."""
        preview = extract_text_preview({})
        # Falls back to json.dumps({}) which is "{}"
        assert preview == "{}"

    def test_extract_text_preview_truncation(self):
        """Long content is truncated to 500 chars."""
        long_text = "A" * 600
        preview = extract_text_preview({"text": long_text})
        assert preview is not None
        assert len(preview) == 500

    def test_derive_idempotency_key_explicit(self):
        """Uses explicit idempotency_key when provided."""
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        payload = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="mneme_api",
            payload_json={"text": "hello"},
            event_time=event_time,
            idempotency_key="my-explicit-key",
        )
        key = derive_idempotency_key(payload, "abc123")
        assert key == "my-explicit-key"

    def test_derive_idempotency_key_from_platform(self):
        """Derives from source_platform:external_event_id when no explicit key."""
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        payload = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="slack",
            payload_json={"text": "hello"},
            event_time=event_time,
            external_event_id="evt-12345",
        )
        key = derive_idempotency_key(payload, "abc123")
        assert key == "slack:evt-12345"

    def test_derive_idempotency_key_from_hash_and_time(self):
        """Derives from payload_hash[:16]:event_time.isoformat when no explicit key or platform+ext_id."""
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        payload = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="mneme_api",
            payload_json={"text": "hello"},
            event_time=event_time,
        )
        payload_hash = compute_payload_hash(payload.payload_json)
        key = derive_idempotency_key(payload, payload_hash)
        assert key == f"{payload_hash[:16]}:{event_time.isoformat()}"


# ═══════════════════════════════════════════════════════════════════════════
# 1. Create Raw Event
# ═══════════════════════════════════════════════════════════════════════════


class TestRawEventCreate:
    """POST /api/v4/raw-events — create a raw event."""

    def test_create_minimal(self, db, test_context, test_project):
        """Creating a raw event with only required fields succeeds."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        payload = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="mneme_api",
            payload_json={"text": "Hello world"},
            event_time=event_time,
            project_id=test_project,
        )
        event = create_raw_event(db, ctx, payload=payload)

        assert event.raw_event_id is not None
        assert event.raw_event_type == "message"
        assert event.source_platform == "mneme_api"
        assert event.payload_json == {"text": "Hello world"}
        assert event.payload_hash is not None
        assert len(event.payload_hash) == 64
        assert event.sensitivity_level == "private"
        assert event.pii_flags == []
        assert event.created_at is not None

    def test_create_auto_computes_payload_hash(self, db, test_context, test_project):
        """payload_hash is auto-computed as SHA-256 of payload_json."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        payload_json = {"text": "test message"}
        payload = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="mneme_api",
            payload_json=payload_json,
            event_time=event_time,
            project_id=test_project,
        )
        event = create_raw_event(db, ctx, payload=payload)

        expected_hash = compute_payload_hash(payload_json)
        assert event.payload_hash == expected_hash

    def test_create_auto_computes_text_preview(self, db, test_context, test_project):
        """text_preview is auto-extracted from payload_json."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        payload = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="mneme_api",
            payload_json={"text": "This is a preview text"},
            event_time=event_time,
            project_id=test_project,
        )
        event = create_raw_event(db, ctx, payload=payload)

        assert event.text_preview == "This is a preview text"

    def test_create_auto_computes_retention_until(self, db, test_context, test_project):
        """retention_until defaults to event_time + 365 days."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        payload = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="mneme_api",
            payload_json={"text": "test"},
            event_time=event_time,
            project_id=test_project,
        )
        event = create_raw_event(db, ctx, payload=payload)

        expected_retention = event_time + timedelta(days=365)
        assert event.retention_until is not None
        if event.retention_until:
            diff = abs((event.retention_until - expected_retention).total_seconds())
            assert diff < 10  # within 10 seconds

    def test_create_pii_flags_empty_array(self, db, test_context, test_project):
        """pii_flags defaults to empty array [] (Phase 4)."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        payload = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="mneme_api",
            payload_json={"text": "test"},
            event_time=event_time,
            project_id=test_project,
        )
        event = create_raw_event(db, ctx, payload=payload)

        assert event.pii_flags == []

    def test_create_auto_derives_idempotency_key(self, db, test_context, test_project):
        """idempotency_key is auto-derived when not provided."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        payload = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="mneme_api",
            payload_json={"text": "test"},
            event_time=event_time,
            project_id=test_project,
        )
        event = create_raw_event(db, ctx, payload=payload)

        assert event.idempotency_key is not None
        assert len(event.idempotency_key) >= 16

    def test_create_with_all_optional_fields(self, db, test_context, test_project):
        """Creating a raw event with all optional fields populated."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        payload = RawEventCreate(
            raw_event_type=RawEventType.tool_call,
            source_platform="slack",
            payload_json={"tool": "search", "args": {"query": "test"}},
            event_time=event_time,
            project_id=test_project,
            conversation_id=uuid4(),
            event_source_id=uuid4(),
            message_id=uuid4(),
            external_event_id="ext-999",
            sensitivity_level=SensitivityLevel.sensitive,
            import_run_id=uuid4(),
            idempotency_key="my-custom-key-001",
        )
        event = create_raw_event(db, ctx, payload=payload)

        assert event.raw_event_type == "tool_call"
        assert event.source_platform == "slack"
        assert event.sensitivity_level == "sensitive"
        assert event.external_event_id == "ext-999"
        assert event.idempotency_key == "my-custom-key-001"
        assert event.payload_hash is not None

    def test_create_different_event_types(self, db, test_context, test_project):
        """All raw_event_type enum values can be stored."""
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        for event_type in RawEventType:
            ctx = RequestContext(
                request_id=test_context.request_id,
                correlation_id=test_context.correlation_id,
                actor=test_context.actor,
                idempotency_key=str(uuid4()),
            )
            payload = RawEventCreate(
                raw_event_type=event_type,
                source_platform="mneme_api",
                payload_json={"text": f"Event type: {event_type.value}"},
                event_time=event_time,
                project_id=test_project,
            )
            event = create_raw_event(db, ctx, payload=payload)
            assert event.raw_event_type == event_type.value


# ═══════════════════════════════════════════════════════════════════════════
# 2. Get Raw Event
# ═══════════════════════════════════════════════════════════════════════════


class TestRawEventGet:
    """GET /api/v4/raw-events/{id} — get raw event detail."""

    def test_get_existing(self, db, test_context, test_project):
        """Get a raw event that exists returns it."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        payload = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="mneme_api",
            payload_json={"text": "My event"},
            event_time=event_time,
            project_id=test_project,
        )
        created = create_raw_event(db, ctx, payload=payload)

        fetched = get_raw_event(db, created.raw_event_id)
        assert fetched is not None
        assert fetched.raw_event_id == created.raw_event_id
        assert fetched.raw_event_type == "message"
        assert fetched.payload_json == {"text": "My event"}
        assert fetched.payload_hash == created.payload_hash

    def test_get_nonexistent(self, db):
        """Get a non-existent raw event returns None."""
        result = get_raw_event(db, UUID("99999999-9999-9999-9999-999999999999"))
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# 3. List Raw Events
# ═══════════════════════════════════════════════════════════════════════════


class TestRawEventList:
    """GET /api/v4/raw-events — list with filters and pagination."""

    def test_list_empty(self, db, test_project):
        """Listing raw events with no matching events returns empty."""
        # Use a unique conversation_id that has no events
        unique_conv_id = uuid4()
        items, total = list_raw_events(db, conversation_id=unique_conv_id)
        assert items == []
        assert total == 0

    def test_list_with_items(self, db, test_context, test_project):
        """List returns created raw events (scoped by conversation_id)."""
        conv_id = uuid4()
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(3):
            ctx = RequestContext(
                request_id=test_context.request_id,
                correlation_id=test_context.correlation_id,
                actor=test_context.actor,
                idempotency_key=str(uuid4()),
            )
            payload = RawEventCreate(
                raw_event_type=RawEventType.message,
                source_platform="mneme_api",
                payload_json={"text": f"Event {i}"},
                event_time=event_time,
                project_id=test_project,
                conversation_id=conv_id,
            )
            create_raw_event(db, ctx, payload=payload)

        items, total = list_raw_events(db, conversation_id=conv_id)
        assert len(items) == 3
        assert total == 3

    def test_list_filter_by_conversation_id(self, db, test_context, test_project):
        """List filtered by conversation_id returns only matching events."""
        conv_id = uuid4()

        ctx1 = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        payload1 = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="mneme_api",
            payload_json={"text": "In conversation"},
            event_time=event_time,
            project_id=test_project,
            conversation_id=conv_id,
        )
        create_raw_event(db, ctx1, payload=payload1)

        ctx2 = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload2 = RawEventCreate(
            raw_event_type=RawEventType.tool_call,
            source_platform="mneme_api",
            payload_json={"text": "Not in conversation"},
            event_time=event_time,
            project_id=test_project,
        )
        create_raw_event(db, ctx2, payload=payload2)

        items, total = list_raw_events(db, conversation_id=conv_id)
        assert len(items) == 1
        assert total == 1
        assert items[0].payload_json == {"text": "In conversation"}

    def test_list_filter_by_event_source_id(self, db, test_context, test_project):
        """List filtered by event_source_id returns only matching events."""
        es_id = uuid4()

        ctx1 = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        payload1 = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="mneme_api",
            payload_json={"text": "From source"},
            event_time=event_time,
            project_id=test_project,
            event_source_id=es_id,
        )
        create_raw_event(db, ctx1, payload=payload1)

        ctx2 = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload2 = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="mneme_api",
            payload_json={"text": "Other source"},
            event_time=event_time,
            project_id=test_project,
        )
        create_raw_event(db, ctx2, payload=payload2)

        items, total = list_raw_events(db, event_source_id=es_id)
        assert len(items) == 1
        assert total == 1
        assert items[0].payload_json == {"text": "From source"}

    def test_list_pagination(self, db, test_context, test_project):
        """List respects page and page_size (scoped by conversation_id)."""
        conv_id = uuid4()
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(5):
            ctx = RequestContext(
                request_id=test_context.request_id,
                correlation_id=test_context.correlation_id,
                actor=test_context.actor,
                idempotency_key=str(uuid4()),
            )
            payload = RawEventCreate(
                raw_event_type=RawEventType.message,
                source_platform="mneme_api",
                payload_json={"text": f"Page Test {i}"},
                event_time=event_time,
                project_id=test_project,
                conversation_id=conv_id,
            )
            create_raw_event(db, ctx, payload=payload)

        items_p1, total = list_raw_events(db, conversation_id=conv_id, page=1, page_size=3)
        assert len(items_p1) == 3
        assert total == 5

        items_p2, total2 = list_raw_events(db, conversation_id=conv_id, page=2, page_size=3)
        assert len(items_p2) == 2
        assert total2 == 5

    def test_list_ordered_by_event_time_desc(self, db, test_context, test_project):
        """List returns events ordered by event_time DESC (scoped by conversation_id)."""
        conv_id = uuid4()
        t1 = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc)
        t3 = datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)

        for t in [t1, t2, t3]:
            ctx = RequestContext(
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
            payload = RawEventCreate(
                raw_event_type=RawEventType.message,
                source_platform="mneme_api",
                payload_json={"text": f"Time {t.isoformat()}"},
                event_time=t,
                project_id=test_project,
                conversation_id=conv_id,
            )
            create_raw_event(db, ctx, payload=payload)

        items, total = list_raw_events(db, conversation_id=conv_id)
        assert total == 3
        # Should be ordered by event_time DESC
        assert items[0].event_time >= items[1].event_time
        assert items[1].event_time >= items[2].event_time


# ═══════════════════════════════════════════════════════════════════════════
# 4. Idempotency
# ═══════════════════════════════════════════════════════════════════════════


class TestRawEventIdempotency:
    """Idempotency key guarantees for raw_events."""

    def test_create_idempotent_same_ikey(self, db, test_context, test_project):
        """Same idempotency_key twice returns the same raw event."""
        key = str(uuid4())
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=key,
        )
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        payload = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="mneme_api",
            payload_json={"text": "Idempotent event"},
            event_time=event_time,
            project_id=test_project,
            idempotency_key="dedup-key-001",
        )

        event1 = create_raw_event(db, ctx, payload=payload)
        event2 = create_raw_event(db, ctx, payload=payload)

        assert event1.raw_event_id == event2.raw_event_id
        assert event1.payload_hash == event2.payload_hash
        assert event1.created_at == event2.created_at

    def test_create_idempotent_different_payload_same_ikey(self, db, test_context, test_project):
        """Same idempotency_key with different payload returns the original (first write wins)."""
        key = str(uuid4())
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=key,
        )
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)

        payload1 = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="mneme_api",
            payload_json={"text": "First write"},
            event_time=event_time,
            project_id=test_project,
            idempotency_key="first-wins-key",
        )
        event1 = create_raw_event(db, ctx, payload=payload1)

        payload2 = RawEventCreate(
            raw_event_type=RawEventType.tool_call,
            source_platform="slack",
            payload_json={"text": "Second write attempt"},
            event_time=event_time,
            project_id=test_project,
            idempotency_key="first-wins-key",
        )
        event2 = create_raw_event(db, ctx, payload=payload2)

        # Should return the first event, not the second
        assert event1.raw_event_id == event2.raw_event_id
        assert event2.payload_json == {"text": "First write"}
        assert event2.raw_event_type == "message"

    def test_create_idempotent_auto_derived_key(self, db, test_context, test_project):
        """Auto-derived idempotency_key also provides idempotency."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        payload = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="mneme_api",
            payload_json={"text": "Auto key test"},
            event_time=event_time,
            project_id=test_project,
        )
        event1 = create_raw_event(db, ctx, payload=payload)
        event2 = create_raw_event(db, ctx, payload=payload)

        assert event1.raw_event_id == event2.raw_event_id

    def test_create_with_explicit_idempotency_key_via_header(self, db, test_context, test_project):
        """Idempotency works when key is passed explicitly vs auto-derived."""
        explicit_key = "my-explicit-idempotency-key-42"
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)

        payload = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="mneme_api",
            payload_json={"text": "Explicit key"},
            event_time=event_time,
            project_id=test_project,
            idempotency_key=explicit_key,
        )
        event1 = create_raw_event(db, ctx, payload=payload)
        event2 = create_raw_event(db, ctx, payload=payload)
        assert event1.raw_event_id == event2.raw_event_id


# ═══════════════════════════════════════════════════════════════════════════
# 5. Edge Cases & Constraints
# ═══════════════════════════════════════════════════════════════════════════


class TestRawEventEdgeCases:
    """Edge cases and constraint tests."""

    def test_valid_sensitivity_levels(self, db, test_context, test_project):
        """All standard sensitivity levels are accepted."""
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        for level in SensitivityLevel:
            ctx = RequestContext(
                request_id=test_context.request_id,
                correlation_id=test_context.correlation_id,
                actor=test_context.actor,
                idempotency_key=str(uuid4()),
            )
            payload = RawEventCreate(
                raw_event_type=RawEventType.message,
                source_platform="mneme_api",
                payload_json={"text": f"Level {level.value}"},
                event_time=event_time,
                project_id=test_project,
                sensitivity_level=level,
            )
            event = create_raw_event(db, ctx, payload=payload)
            assert event.sensitivity_level == level.value

    def test_large_payload_json(self, db, test_context, test_project):
        """Large payload_json is handled correctly."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        large_payload = {
            "text": "Large payload",
            "items": [{"id": i, "value": f"item-{i}"} for i in range(100)],
        }
        payload = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="mneme_api",
            payload_json=large_payload,
            event_time=event_time,
            project_id=test_project,
        )
        event = create_raw_event(db, ctx, payload=payload)

        assert event.payload_json == large_payload
        assert event.payload_hash is not None
        assert event.text_preview is not None

    def test_idempotency_key_stored_correctly(self, db, test_context, test_project):
        """The idempotency_key is correctly persisted in the DB."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        payload = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="mneme_api",
            payload_json={"text": "Key test"},
            event_time=event_time,
            project_id=test_project,
            idempotency_key="persisted-key-123",
        )
        event = create_raw_event(db, ctx, payload=payload)

        fetched = get_raw_event(db, event.raw_event_id)
        assert fetched is not None
        assert fetched.idempotency_key == "persisted-key-123"

    def test_none_fields_returned_as_none(self, db, test_context, test_project):
        """Optional fields that were not set return None."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        payload = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="mneme_api",
            payload_json={"text": "Minimal"},
            event_time=event_time,
            project_id=test_project,
        )
        event = create_raw_event(db, ctx, payload=payload)

        assert event.message_id is None
        assert event.external_event_id is None
        assert event.import_run_id is None

    def test_list_page_size_limit(self, db, test_context, test_project):
        """List with page_size greater than total returns all items (scoped by conversation_id)."""
        conv_id = uuid4()
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(2):
            ctx = RequestContext(
                request_id=test_context.request_id,
                correlation_id=test_context.correlation_id,
                actor=test_context.actor,
                idempotency_key=str(uuid4()),
            )
            payload = RawEventCreate(
                raw_event_type=RawEventType.message,
                source_platform="mneme_api",
                payload_json={"text": f"PageSizeLimit Event {i}"},
                event_time=event_time,
                project_id=test_project,
                conversation_id=conv_id,
            )
            create_raw_event(db, ctx, payload=payload)

        items, total = list_raw_events(db, conversation_id=conv_id, page=1, page_size=100)
        assert len(items) == 2
        assert total == 2

    def test_unicode_in_payload_json(self, db, test_context, test_project):
        """Unicode characters in payload_json are handled correctly."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        payload = RawEventCreate(
            raw_event_type=RawEventType.message,
            source_platform="mneme_api",
            payload_json={"text": "你好世界 \U0001f30d 日本語テスト"},
            event_time=event_time,
            project_id=test_project,
        )
        event = create_raw_event(db, ctx, payload=payload)

        assert event.payload_hash is not None
        assert len(event.payload_hash) == 64

    def test_create_multiple_events_different_types(self, db, test_context, test_project):
        """Multiple events of different types can coexist (scoped by conversation_id)."""
        conv_id = uuid4()
        event_time = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        types = [
            RawEventType.message,
            RawEventType.tool_call,
            RawEventType.tool_result,
            RawEventType.agent_thought,
            RawEventType.system_event,
            RawEventType.custom,
        ]
        for event_type in types:
            ctx = RequestContext(
                request_id=test_context.request_id,
                correlation_id=test_context.correlation_id,
                actor=test_context.actor,
                idempotency_key=str(uuid4()),
            )
            payload = RawEventCreate(
                raw_event_type=event_type,
                source_platform="mneme_api",
                payload_json={"text": f"Type: {event_type.value}"},
                event_time=event_time,
                project_id=test_project,
                conversation_id=conv_id,
            )
            event = create_raw_event(db, ctx, payload=payload)
            assert event.raw_event_type == event_type.value

        items, total = list_raw_events(db, conversation_id=conv_id)
        assert total == len(types)
