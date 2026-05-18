"""P4-01 Conversation API — contract tests (DB layer).

Covers P4-01 completion criteria:
1. Create conversation → DB has complete row, conversation_type and source_platform correct.
2. Get conversation by ID.
3. List conversations with filters and pagination.
4. Update conversation mutable fields.
5. Archive conversation → status='archived', ended_at set.
6. Soft-delete conversation → status='deleted'.
7. Idempotency: duplicate create with same key returns existing.
8. Error cases: not-found, inactive update/archive.

Total: 20+ contract tests.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from mneme.api.context import ActorContext, RequestContext
from mneme.db.conversations import (
    archive_conversation,
    create_conversation,
    delete_conversation,
    get_conversation,
    list_conversations,
    update_conversation,
)
from mneme.schemas.common import SensitivityLevel
from mneme.schemas.conversations import (
    ConversationCreateRequest,
    ConversationType,
    ConversationUpdateRequest,
)


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
    code = f"TEST-{uuid4().hex[:8].upper()}"
    db.execute(
        text(
            "INSERT INTO projects (project_id, project_code, name, status, sensitivity_default) "
            "VALUES (:pid, :code, :name, 'active', 'normal')"
        ),
        {"pid": project_id.hex, "code": code, "name": "P4-01 Test Project"},
    )
    db.flush()
    return project_id


@pytest.fixture(autouse=True)
def ensure_conversations_table(db):
    """Ensure the conversations table exists (not in SQLite schema yet)."""
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS conversations (
            conversation_id TEXT PRIMARY KEY NOT NULL,
            project_id TEXT,
            owner_user_id TEXT,
            conversation_type TEXT NOT NULL DEFAULT 'chat',
            title TEXT,
            source_platform TEXT NOT NULL,
            sensitivity_level TEXT NOT NULL DEFAULT 'private',
            retention_days INTEGER,
            conversation_status TEXT NOT NULL DEFAULT 'active',
            started_at TEXT,
            ended_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """))
    db.flush()
    yield


# ═══════════════════════════════════════════════════════════════════════════
# 1. Create Conversation
# ═══════════════════════════════════════════════════════════════════════════


class TestConversationCreate:
    """POST /api/v4/conversations — create a conversation."""

    def test_create_minimal(self, db, test_context, test_project):
        """Creating a conversation with only required fields succeeds."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload = ConversationCreateRequest(
            project_id=test_project,
            source_platform="mneme_api",
        )
        conv = create_conversation(db, ctx, payload=payload)

        assert conv.conversation_id is not None
        assert conv.conversation_type == "chat"
        assert conv.source_platform == "mneme_api"
        assert conv.conversation_status == "active"
        assert conv.sensitivity_level == "private"
        assert conv.project_id == test_project
        assert conv.owner_user_id == UUID("00000000-0000-0000-0000-000000000001")

    def test_create_with_all_fields(self, db, test_context, test_project):
        """Creating a conversation with all optional fields."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload = ConversationCreateRequest(
            project_id=test_project,
            conversation_type=ConversationType.meeting,
            source_platform="mneme_web",
            title="Weekly Sync",
            sensitivity_level=SensitivityLevel.sensitive,
            retention_days=90,
        )
        conv = create_conversation(db, ctx, payload=payload)

        assert conv.conversation_type == "meeting"
        assert conv.source_platform == "mneme_web"
        assert conv.title == "Weekly Sync"
        assert conv.sensitivity_level == "sensitive"
        assert conv.retention_days == 90
        assert conv.conversation_status == "active"
        assert conv.started_at is None
        assert conv.ended_at is None

    def test_create_with_started_at(self, db, test_context, test_project):
        """Creating a conversation with explicit started_at."""
        from datetime import datetime, timezone

        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        started = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
        payload = ConversationCreateRequest(
            project_id=test_project,
            source_platform="mneme_api",
            started_at=started,
        )
        conv = create_conversation(db, ctx, payload=payload)

        assert conv.started_at is not None
        assert conv.conversation_status == "active"

    def test_create_defaults_to_chat_type(self, db, test_context, test_project):
        """conversation_type defaults to 'chat' when not specified."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload = ConversationCreateRequest(
            project_id=test_project,
            source_platform="mneme_api",
        )
        conv = create_conversation(db, ctx, payload=payload)
        assert conv.conversation_type == "chat"

    def test_create_returns_created_at(self, db, test_context, test_project):
        """created_at and updated_at are populated automatically."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload = ConversationCreateRequest(
            project_id=test_project,
            source_platform="mneme_api",
        )
        conv = create_conversation(db, ctx, payload=payload)

        assert conv.created_at is not None
        assert conv.updated_at is not None


# ═══════════════════════════════════════════════════════════════════════════
# 2. Get Conversation
# ═══════════════════════════════════════════════════════════════════════════


class TestConversationGet:
    """GET /api/v4/conversations/{id} — get conversation detail."""

    def test_get_existing(self, db, test_context, test_project):
        """Get a conversation that exists returns it."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload = ConversationCreateRequest(
            project_id=test_project,
            source_platform="mneme_api",
            title="My Conversation",
        )
        created = create_conversation(db, ctx, payload=payload)

        fetched = get_conversation(db, created.conversation_id)
        assert fetched is not None
        assert fetched.conversation_id == created.conversation_id
        assert fetched.title == "My Conversation"
        assert fetched.source_platform == "mneme_api"
        assert fetched.conversation_status == "active"

    def test_get_nonexistent(self, db):
        """Get a non-existent conversation returns None."""
        result = get_conversation(db, UUID("99999999-9999-9999-9999-999999999999"))
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# 3. List Conversations
# ═══════════════════════════════════════════════════════════════════════════


class TestConversationList:
    """GET /api/v4/conversations — list with filters and pagination."""

    def test_list_empty(self, db, test_project):
        """Listing conversations by project with none created returns empty."""
        items, total = list_conversations(db, project_id=test_project)
        assert items == []
        assert total == 0

    def test_list_with_items(self, db, test_context, test_project):
        """List returns created conversations (filtered by project)."""
        for i in range(3):
            ctx = RequestContext(
                request_id=test_context.request_id,
                correlation_id=test_context.correlation_id,
                actor=test_context.actor,
                idempotency_key=str(uuid4()),
            )
            payload = ConversationCreateRequest(
                project_id=test_project,
                source_platform="mneme_api",
                title=f"Conv {i}",
            )
            create_conversation(db, ctx, payload=payload)

        items, total = list_conversations(db, project_id=test_project)
        assert len(items) == 3
        assert total == 3

    def test_list_filter_by_project(self, db, test_context, test_project):
        """List filtered by project returns only matching conversations."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload = ConversationCreateRequest(
            project_id=test_project,
            source_platform="mneme_api",
            title="Project A Conv",
        )
        create_conversation(db, ctx, payload=payload)

        # Create another project
        project_b = uuid4()
        db.execute(
            text(
                "INSERT INTO projects (project_id, project_code, name, status, sensitivity_default) "
                "VALUES (:pid, :code, :name, 'active', 'normal')"
            ),
            {"pid": project_b.hex, "code": f"TST-{uuid4().hex[:8].upper()}", "name": "Project B"},
        )
        db.flush()

        ctx2 = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload2 = ConversationCreateRequest(
            project_id=project_b,
            source_platform="mneme_web",
            title="Project B Conv",
        )
        create_conversation(db, ctx2, payload=payload2)

        items, total = list_conversations(db, project_id=test_project)
        assert len(items) == 1
        assert total == 1
        assert items[0].title == "Project A Conv"

    def test_list_filter_by_type(self, db, test_context, test_project):
        """List filtered by conversation_type + project."""
        ctx1 = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload1 = ConversationCreateRequest(
            project_id=test_project,
            source_platform="mneme_api",
            conversation_type=ConversationType.chat,
            title="Chat Conv",
        )
        create_conversation(db, ctx1, payload=payload1)

        ctx2 = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload2 = ConversationCreateRequest(
            project_id=test_project,
            source_platform="mneme_api",
            conversation_type=ConversationType.meeting,
            title="Meeting Conv",
        )
        create_conversation(db, ctx2, payload=payload2)

        items, total = list_conversations(db, project_id=test_project, conversation_type="meeting")
        assert len(items) == 1
        assert total == 1
        assert items[0].title == "Meeting Conv"

    def test_list_filter_by_status(self, db, test_context, test_project):
        """List filtered by conversation_status + project."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload = ConversationCreateRequest(
            project_id=test_project,
            source_platform="mneme_api",
            title="To Archive",
        )
        created = create_conversation(db, ctx, payload=payload)

        ctx_archive = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        archive_conversation(db, ctx_archive, conversation_id=created.conversation_id)

        ctx2 = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload2 = ConversationCreateRequest(
            project_id=test_project,
            source_platform="mneme_api",
            title="Active Conv",
        )
        create_conversation(db, ctx2, payload=payload2)

        items, total = list_conversations(db, project_id=test_project, conversation_status="archived")
        assert len(items) == 1
        assert total == 1
        assert items[0].title == "To Archive"

        items2, total2 = list_conversations(db, project_id=test_project, conversation_status="active")
        assert len(items2) == 1
        assert total2 == 1
        assert items2[0].title == "Active Conv"

    def test_list_pagination(self, db, test_context, test_project):
        """List respects page and page_size."""
        for i in range(5):
            ctx = RequestContext(
                request_id=test_context.request_id,
                correlation_id=test_context.correlation_id,
                actor=test_context.actor,
                idempotency_key=str(uuid4()),
            )
            payload = ConversationCreateRequest(
                project_id=test_project,
                source_platform="mneme_api",
                title=f"Page Test {i}",
            )
            create_conversation(db, ctx, payload=payload)

        items_p1, total = list_conversations(db, project_id=test_project, page=1, page_size=3)
        assert len(items_p1) == 3
        assert total == 5

        items_p2, total2 = list_conversations(db, project_id=test_project, page=2, page_size=3)
        assert len(items_p2) == 2
        assert total2 == 5


# ═══════════════════════════════════════════════════════════════════════════
# 4. Update Conversation
# ═══════════════════════════════════════════════════════════════════════════


class TestConversationUpdate:
    """PATCH /api/v4/conversations/{id} — update mutable fields."""

    def test_update_title(self, db, test_context, test_project):
        """Update the title of a conversation."""
        ctx_create = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload = ConversationCreateRequest(
            project_id=test_project,
            source_platform="mneme_api",
            title="Original Title",
        )
        created = create_conversation(db, ctx_create, payload=payload)

        ctx_update = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        update_payload = ConversationUpdateRequest(title="Updated Title")
        updated = update_conversation(
            db, ctx_update,
            conversation_id=created.conversation_id,
            payload=update_payload,
        )
        assert updated.title == "Updated Title"
        assert updated.conversation_status == "active"

    def test_update_sensitivity(self, db, test_context, test_project):
        """Update sensitivity_level."""
        ctx_create = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload = ConversationCreateRequest(
            project_id=test_project,
            source_platform="mneme_api",
            sensitivity_level=SensitivityLevel.private,
        )
        created = create_conversation(db, ctx_create, payload=payload)

        ctx_update = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        update_payload = ConversationUpdateRequest(
            sensitivity_level=SensitivityLevel.sensitive,
        )
        updated = update_conversation(
            db, ctx_update,
            conversation_id=created.conversation_id,
            payload=update_payload,
        )
        assert updated.sensitivity_level == "sensitive"

    def test_update_retention_days(self, db, test_context, test_project):
        """Update retention_days."""
        ctx_create = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload = ConversationCreateRequest(
            project_id=test_project,
            source_platform="mneme_api",
            retention_days=30,
        )
        created = create_conversation(db, ctx_create, payload=payload)

        ctx_update = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        update_payload = ConversationUpdateRequest(retention_days=60)
        updated = update_conversation(
            db, ctx_update,
            conversation_id=created.conversation_id,
            payload=update_payload,
        )
        assert updated.retention_days == 60

    def test_update_nonexistent_raises(self, db, test_context):
        """Updating a non-existent conversation raises ValueError."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        update_payload = ConversationUpdateRequest(title="Won't Work")
        with pytest.raises(ValueError, match="not found"):
            update_conversation(
                db, ctx,
                conversation_id=UUID("99999999-9999-9999-9999-999999999999"),
                payload=update_payload,
            )

    def test_update_archived_raises(self, db, test_context, test_project):
        """Updating an archived conversation raises ValueError."""
        ctx_create = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload = ConversationCreateRequest(
            project_id=test_project,
            source_platform="mneme_api",
        )
        created = create_conversation(db, ctx_create, payload=payload)

        ctx_archive = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        archive_conversation(db, ctx_archive, conversation_id=created.conversation_id)

        ctx_update = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        update_payload = ConversationUpdateRequest(title="Should Fail")
        with pytest.raises(ValueError, match="not active"):
            update_conversation(
                db, ctx_update,
                conversation_id=created.conversation_id,
                payload=update_payload,
            )


# ═══════════════════════════════════════════════════════════════════════════
# 5. Archive Conversation
# ═══════════════════════════════════════════════════════════════════════════


class TestConversationArchive:
    """POST /api/v4/conversations/{id}/archive — archive conversation."""

    def test_archive_active(self, db, test_context, test_project):
        """Archiving an active conversation sets status='archived' and ended_at."""
        ctx_create = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload = ConversationCreateRequest(
            project_id=test_project,
            source_platform="mneme_api",
        )
        created = create_conversation(db, ctx_create, payload=payload)
        assert created.ended_at is None
        assert created.conversation_status == "active"

        ctx_archive = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        archived = archive_conversation(db, ctx_archive, conversation_id=created.conversation_id)

        assert archived.conversation_status == "archived"
        assert archived.ended_at is not None

    def test_archive_already_archived_raises(self, db, test_context, test_project):
        """Archiving an already archived conversation raises ValueError."""
        ctx_create = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload = ConversationCreateRequest(
            project_id=test_project,
            source_platform="mneme_api",
        )
        created = create_conversation(db, ctx_create, payload=payload)

        ctx_archive = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        archive_conversation(db, ctx_archive, conversation_id=created.conversation_id)

        ctx_archive2 = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        with pytest.raises(ValueError, match="not active"):
            archive_conversation(db, ctx_archive2, conversation_id=created.conversation_id)

    def test_archive_nonexistent_raises(self, db, test_context):
        """Archiving a non-existent conversation raises ValueError."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        with pytest.raises(ValueError, match="not found"):
            archive_conversation(
                db, ctx,
                conversation_id=UUID("99999999-9999-9999-9999-999999999999"),
            )


# ═══════════════════════════════════════════════════════════════════════════
# 6. Delete (Soft) Conversation
# ═══════════════════════════════════════════════════════════════════════════


class TestConversationDelete:
    """POST /api/v4/conversations/{id}/delete — soft-delete conversation."""

    def test_delete_active(self, db, test_context, test_project):
        """Soft-deleting an active conversation sets status='deleted'."""
        ctx_create = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload = ConversationCreateRequest(
            project_id=test_project,
            source_platform="mneme_api",
        )
        created = create_conversation(db, ctx_create, payload=payload)

        ctx_del = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        deleted = delete_conversation(db, ctx_del, conversation_id=created.conversation_id)

        assert deleted.conversation_status == "deleted"

        # The conversation still exists in DB
        fetched = get_conversation(db, created.conversation_id)
        assert fetched is not None
        assert fetched.conversation_status == "deleted"

    def test_delete_nonexistent_raises(self, db, test_context):
        """Deleting a non-existent conversation raises ValueError."""
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        with pytest.raises(ValueError, match="not found"):
            delete_conversation(
                db, ctx,
                conversation_id=UUID("99999999-9999-9999-9999-999999999999"),
            )


# ═══════════════════════════════════════════════════════════════════════════
# 7. Idempotency
# ═══════════════════════════════════════════════════════════════════════════


class TestConversationIdempotency:
    """Idempotency key guarantees for conversations."""

    def test_create_idempotent(self, db, test_context, test_project):
        """Same idempotency key twice returns the same conversation."""
        key = str(uuid4())
        ctx = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=key,
        )
        payload = ConversationCreateRequest(
            project_id=test_project,
            source_platform="mneme_api",
            title="Idempotent Conv",
        )
        conv1 = create_conversation(db, ctx, payload=payload)
        conv2 = create_conversation(db, ctx, payload=payload)

        assert conv1.conversation_id == conv2.conversation_id
        assert conv1.title == conv2.title
        assert conv1.created_at == conv2.created_at

    def test_update_idempotent(self, db, test_context, test_project):
        """Same idempotency key for update returns the same result."""
        key_create = str(uuid4())
        ctx_create = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=key_create,
        )
        payload = ConversationCreateRequest(
            project_id=test_project,
            source_platform="mneme_api",
            title="Original",
        )
        created = create_conversation(db, ctx_create, payload=payload)

        key_update = str(uuid4())
        ctx_update = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=key_update,
        )
        update_payload = ConversationUpdateRequest(title="Updated Title")
        updated1 = update_conversation(
            db, ctx_update,
            conversation_id=created.conversation_id,
            payload=update_payload,
        )
        updated2 = update_conversation(
            db, ctx_update,
            conversation_id=created.conversation_id,
            payload=update_payload,
        )
        assert updated1.title == updated2.title
        assert updated1.updated_at == updated2.updated_at

    def test_archive_pre_check_blocks_idempotent_replay(self, db, test_context, test_project):
        """Archive pre-checks status — idempotent replay is blocked by design.

        The archive_conversation function validates status='active' before
        entering the idempotent wrapper.  Calling archive twice with the same
        key raises ValueError on the second call because the conversation is
        already archived.
        """
        ctx_create = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=str(uuid4()),
        )
        payload = ConversationCreateRequest(
            project_id=test_project,
            source_platform="mneme_api",
        )
        created = create_conversation(db, ctx_create, payload=payload)

        key = str(uuid4())
        ctx_archive = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=key,
        )
        archived = archive_conversation(db, ctx_archive, conversation_id=created.conversation_id)
        assert archived.conversation_status == "archived"

        # Second call with the same key should fail because status is no longer active
        with pytest.raises(ValueError, match="not active"):
            archive_conversation(db, ctx_archive, conversation_id=created.conversation_id)
