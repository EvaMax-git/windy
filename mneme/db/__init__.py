"""Database layer — SQLAlchemy models, DAO functions, and Alembic migrations.

Submodules provide CRUD operations for each domain entity:
* ``agents``              — Agent + agent token management
* ``memories``            — Memory lifecycle (create/activate/expire/restore/delete)
* ``memory_candidates``   — LLM-extracted memory candidates
* ``memory_index_entries`` — FTS/vector index entry management
* ``memory_sources``      — Source attribution for activated memories
* ``conversations``       — Conversations and messages
* ``projects``            — Project scoping
* ``review_items``        — Human review queue management
* ``audit``               — Audit and outbox event records
* ``context_packs``       — Compiled context pack storage
* ``jobs``                — Pipeline job tracking
* ``pipelines``           — Pipeline run tracking
* ``inbox``               — Data import inbox
* ``assets``              — Asset metadata storage
* ``auth``                — User session and authentication records
* ``budget``              — API call budget tracking
* ``vault``               — Encrypted credential storage
* ``knowledge``           — Knowledge document + chunk storage
* ``transactions``        — Transaction helpers
* ``api_call_logs``       — API call telemetry
"""

# Re-export commonly used DAO utilities for convenience
from mneme.db.transactions import transaction

__all__ = [
    "transaction",
]
