"""PostgreSQL text[] ↔ Python list conversion utilities.

Provides canonical serialisation/deserialisation for ``text[]`` columns
so that every data-access layer writes the same PG array-literal format
(``{elem1,elem2}``) regardless of the database backend (PostgreSQL or
SQLite).

Rationale
---------
* ``sqlite3.register_adapter(list, …)`` and ``psycopg2`` adapters in
  ``tests/conftest.py`` auto-convert Python lists to JSON.  For
  ``text[]`` columns the correct wire-format is the **PG array literal**
  (``{elem}``) — **not** a JSON array (``["elem"]``).
* Passing PG array-literal *strings* as bound parameters bypasses the
  adapters entirely and produces the same stored representation on
  every backend.
"""

from __future__ import annotations

import json


# ── Serialisation: Python list → PG array literal ───────────────────────────

def to_pg_array(values: list[str] | None) -> str:
    """Convert a Python list of strings to a PostgreSQL array literal.

    **Format**: ``{elem1,elem2}`` with proper escaping for special
    characters (backslash, double-quote, comma, curly-brace).

    Returns ``{}`` for empty / ``None`` input.

    >>> to_pg_array(["text/plain", "text/md"])
    '{"text/plain","text/md"}'

    >>> to_pg_array([])
    '{}'

    >>> to_pg_array(None)
    '{}'
    """
    if not values:
        return "{}"

    escaped_parts: list[str] = []
    for v in values:
        s = str(v)
        s = s.replace("\\", "\\\\")
        s = s.replace('"', '\\"')
        escaped_parts.append(f'"{s}"')

    return "{" + ",".join(escaped_parts) + "}"


# ── Deserialisation: PG array literal / JSON → Python list ──────────────────

def parse_pg_array(value: object) -> list[str]:
    """Parse a value that originated from a PostgreSQL ``text[]`` column.

    Handles three representations:

    1. **Python list** — e.g. ``psycopg2`` returns ``text[]`` columns as
       native Python lists.
    2. **PG array literal string** — ``{"text/plain","text/md"}`` (the
       format produced by :func:`to_pg_array`).
    3. **JSON array string** — ``["text/plain","text/md"]`` (legacy
       format; may exist in databases populated before this fix).

    Returns an empty list for ``None`` or un-parseable input.

    >>> parse_pg_array(None)
    []

    >>> parse_pg_array(["a", "b"])
    ['a', 'b']

    >>> parse_pg_array('{"text/plain","text/md"}')
    ['text/plain', 'text/md']

    >>> parse_pg_array('["text/plain","text/md"]')
    ['text/plain', 'text/md']

    >>> parse_pg_array('{}')
    []

    >>> parse_pg_array('{only}')
    ['only']

    >>> parse_pg_array('{"has\\\\bs","has\\"quote"}')
    ['has\\\\bs', 'has"quote']
    """
    if value is None:
        return []

    # ── Already a Python list (psycopg2 normal path) ──────────────────────
    if isinstance(value, list):
        return [str(v) for v in value]

    if not isinstance(value, str):
        return []

    s = value.strip()

    if not s:
        return []

    # ── JSON array (legacy format) ────────────────────────────────────────
    if s.startswith("["):
        try:
            result = json.loads(s)
            if isinstance(result, list):
                return [str(v) for v in result]
        except (json.JSONDecodeError, TypeError):
            pass

    # ── PG array literal ──────────────────────────────────────────────────
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1]  # strip outer braces
        if not inner.strip():
            return []

        elements: list[str] = []
        pos = 0
        while pos < len(inner):
            # Skip whitespace / commas between elements
            while pos < len(inner) and inner[pos] in (" ", "\t", "\n", ","):
                pos += 1
            if pos >= len(inner):
                break

            if inner[pos] == '"':
                # Quoted element — find the closing un-escaped quote
                pos += 1  # skip opening quote
                buf: list[str] = []
                while pos < len(inner):
                    ch = inner[pos]
                    if ch == "\\" and pos + 1 < len(inner):
                        next_ch = inner[pos + 1]
                        buf.append(next_ch)
                        pos += 2
                    elif ch == '"':
                        pos += 1  # skip closing quote
                        break
                    else:
                        buf.append(ch)
                        pos += 1
                elements.append("".join(buf))
            else:
                # Unquoted element — read until comma or end
                start = pos
                while pos < len(inner) and inner[pos] not in (",", "}"):
                    pos += 1
                elements.append(inner[start:pos].strip())

        return elements

    # ── Fallback: single value ───────────────────────────────────────────
    return [s]
