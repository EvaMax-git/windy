"""Database compatibility utilities.

Provides adapters that work on both PostgreSQL and SQLite,
allowing the same SQLAlchemy code to run on either backend.
"""
from __future__ import annotations

import os
from uuid import UUID

from sqlalchemy import TypeDecorator, types


def _is_postgres() -> bool:
    """Return True if the configured DATABASE_URL points to PostgreSQL."""
    url = os.environ.get("DATABASE_URL", "")
    return url.startswith("postgresql")


class PortableUUID(TypeDecorator):
    """A UUID type that works on both PostgreSQL and SQLite.

    On PostgreSQL it delegates to the native UUID type.
    On SQLite it stores UUIDs as standard dashed strings (matching
    ``str(uuid)``), so that ``WHERE project_id = :pid`` works regardless
    of whether the column was populated via ``str(pid)`` or via a UUID object.
    """

    impl = types.TypeEngine
    cache_ok = True

    def __init__(self, as_uuid: bool = True):
        super().__init__()
        self._as_uuid = as_uuid
        if _is_postgres():
            from sqlalchemy.dialects.postgresql import UUID as PG_UUID
            self._impl = PG_UUID(as_uuid=as_uuid)
        else:
            self._impl = types.String()

    def get_col_spec(self, **kw):
        return self._impl.get_col_spec(**kw)

    def bind_processor(self, dialect):
        """On SQLite, convert UUID → standard dashed string.

        Uses ``str(uuid)`` format (with hyphens) so that
        ``WHERE project_id = :pid`` can find rows inserted via ``str(pid)``.
        """
        if _is_postgres():
            return self._impl.bind_processor(dialect)
        # SQLite: UUID → standard dashed string
        def process(value):
            if value is None:
                return None
            return str(value)
        return process

    def result_processor(self, dialect, coltype):
        """On SQLite, convert string → UUID if ``as_uuid`` is set."""
        if _is_postgres():
            return self._impl.result_processor(dialect, coltype)
        if self._as_uuid:
            def process(value):
                if value is None:
                    return None
                if isinstance(value, UUID):
                    return value
                return UUID(value)
            return process
        return None

    def coerce_compared_value(self, op, value):
        return self._impl.coerce_compared_value(op, value)

    def python_type(self):
        return UUID if self._as_uuid else str


# Singleton for use in bindparam type_= statements
PG_UUID_COMPAT = PortableUUID(as_uuid=True)
