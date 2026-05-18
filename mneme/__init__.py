"""Mneme backend package (P5-04 modular architecture).

Subsystems
----------
* ``mneme.memory``     — FTS search, embedding, extract pipeline
* ``mneme.context``    — Context compiler for agent queries
* ``mneme.security``   — Auth, policy engine, review routing, audit
* ``mneme.migration``  — SQLite → PostgreSQL migration engine
* ``mneme.core``       — Module registry + cross-cutting infrastructure
* ``mneme.db``         — Database models and DAO layer
* ``mneme.api``        — FastAPI v4 route definitions
* ``mneme.schemas``    — Pydantic request/response schemas
* ``mneme.gateway``    — LLM provider routing and unified call entry
* ``mneme.importer``   — Mneme2 → v4.1 data import framework
* ``mneme.knowledge``  — Knowledge chunking, FTS, citation
* ``mneme.observability`` — Logging, health checks, metrics
* ``mneme.storage``    — File-system storage backend
* ``mneme.vault``      — Credential encryption and access logging
* ``mneme.worker``     — Outbox poller and event consumers
* ``mneme.backup``     — pg_dump + manifest + integrity verification
* ``mneme.restore``    — Restore preview and convenience wrappers
* ``mneme.domain``     — Object registry, versioning helpers
"""

from mneme.core import ModuleRegistry, register

__all__ = [
    "ModuleRegistry",
    "register",
]
