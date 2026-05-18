"""Memory-store isolation access control.

Ensures AgentA cannot read or modify AgentB's memory_store.  The access
control chain is::

    Agent → memory_stores (via agent_id) → memories (via store_id)

Two layers of protection are provided:

1. **FastAPI dependency** — ``check_store_access_middleware`` is a router-level
   dependency that inspects ``store_id`` / ``memory_id`` from URL path parameters
   and denies access if the authenticated agent does not own the store.

2. **ASGI middleware** — ``StoreAccessMiddleware`` sits in the HTTP stack and
   intercepts all requests to ``/api/v4/memory*`` paths, performing the same
   check before the request reaches a route handler.

Both layers only activate for agent-authenticated requests (Bearer token).
User-session requests and unauthenticated requests pass through unchanged.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from mneme.api.auth import extract_agent_token
from mneme.api.errors import ApiError
from mneme.api.schemas import error_envelope
from mneme.db.agents import AuthenticatedAgent, authenticate_agent_token
from mneme.db.base import SessionLocal, get_db
from mneme.db.memory_stores import get_store
from mneme.db.memories import get_memory

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _resolve_agent(request: Request, db: Session) -> AuthenticatedAgent | None:
    """Try to authenticate an agent from the request Bearer token.

    Returns None if no agent token is present (user session or anonymous).
    """
    token = extract_agent_token(request)
    if token is None:
        return None
    return authenticate_agent_token(db, token)


def _get_agent_id(agent: AuthenticatedAgent) -> UUID:
    """Extract the agent_id from an AuthenticatedAgent."""
    return agent.agent.agent_id


def _deny_store_access(
    store_id: UUID,
    agent: AuthenticatedAgent,
    reason: str = "",
) -> None:
    """Raise a 403 permission-denied error for store access."""
    msg = (
        f"Agent '{agent.agent.agent_code}' does not have access "
        f"to memory_store '{store_id}'"
    )
    if reason:
        msg = f"{msg}: {reason}"
    raise ApiError(403, "permission_denied", msg)


def _deny_memory_access(
    memory_id: UUID,
    agent: AuthenticatedAgent,
    reason: str = "",
) -> None:
    """Raise a 403 permission-denied error for memory access."""
    msg = (
        f"Agent '{agent.agent.agent_code}' does not have access "
        f"to memory '{memory_id}'"
    )
    if reason:
        msg = f"{msg}: {reason}"
    raise ApiError(403, "permission_denied", msg)


# ═══════════════════════════════════════════════════════════════════════════════
# Core access checks
# ═══════════════════════════════════════════════════════════════════════════════


def check_store_ownership(
    store_id: UUID,
    agent: AuthenticatedAgent,
    db: Session,
) -> None:
    """Verify *agent* owns *store_id*.  Raises ``ApiError(403)`` if not.

    A store is owned by an agent when ``memory_stores.agent_id == agent.agent_id``.
    Unbound stores (``agent_id IS NULL``) are accessible by any agent.
    """
    store = get_store(db, store_id)
    if store is None:
        raise ApiError(404, "not_found", f"MemoryStore '{store_id}' not found")

    if store.agent_id is None:
        # Unbound store — accessible by all agents
        logger.debug(
            "Store %s is unbound, allowing access for agent %s",
            store_id, agent.agent.agent_code,
        )
        return

    if store.agent_id != _get_agent_id(agent):
        logger.warning(
            "Agent '%s' attempted to access store '%s' owned by agent '%s'",
            agent.agent.agent_code, store_id, store.agent_id,
        )
        _deny_store_access(store_id, agent)

    logger.debug(
        "Store access OK: agent=%s store=%s",
        agent.agent.agent_code, store_id,
    )


def check_memory_store_ownership(
    memory_id: UUID,
    agent: AuthenticatedAgent,
    db: Session,
) -> None:
    """Verify *agent* owns the store that *memory_id* belongs to.

    If the memory has no ``store_id`` (unbound), access is allowed.
    Otherwise delegates to :func:`check_store_ownership`.
    """
    memory = get_memory(db, memory_id)
    if memory is None:
        raise ApiError(404, "not_found", f"Memory '{memory_id}' not found")

    if memory.store_id is None:
        # Memory not bound to any store — accessible by all
        logger.debug(
            "Memory %s is unbound (no store_id), allowing access for agent %s",
            memory_id, agent.agent.agent_code,
        )
        return

    check_store_ownership(memory.store_id, agent, db)


# ═══════════════════════════════════════════════════════════════════════════════
# Router-level FastAPI dependency  (acts as middleware for a group of routes)
# ═══════════════════════════════════════════════════════════════════════════════


async def check_store_access_middleware(
    request: Request,
    db: Session = Depends(get_db),
) -> None:
    """Router-level dependency — enforce store isolation on every request.

    Add this to an ``APIRouter``'s ``dependencies`` list so it runs before
    every route handler in that router.

    Only activates for **agent-authenticated** requests (Bearer token).
    User-session requests skip the check entirely.

    Inspects ``request.path_params`` for ``store_id`` or ``memory_id``:
    * If ``store_id`` is present → verifies agent owns the store.
    * If ``memory_id`` is present → resolves memory → store → verifies ownership.
    * If neither is present → allows through (list/create endpoints must
      add their own filtering).
    """
    # ── Only check agent-authenticated requests ──────────────────────────
    agent = _resolve_agent(request, db)
    if agent is None:
        return  # user session or anonymous — no isolation needed

    # ── Inspect path parameters for store or memory IDs ──────────────────
    store_id: UUID | None = request.path_params.get("store_id")
    memory_id: UUID | None = request.path_params.get("memory_id")

    if store_id is not None:
        check_store_ownership(store_id, agent, db)
        return

    if memory_id is not None:
        check_memory_store_ownership(memory_id, agent, db)
        return

    # No identifiable resource in path — allow through.
    # List / create endpoints are responsible for filtering results
    # or validating body parameters themselves.
    logger.debug(
        "No store_id/memory_id in path params for agent=%s, skipping middleware check",
        agent.agent.agent_code,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ASGI middleware  (global protection, sits before route handlers)
# ═══════════════════════════════════════════════════════════════════════════════

# Path prefixes that are subject to store-isolation checks
_PROTECTED_PATH_PREFIXES = (
    "/api/v4/memory-stores",
    "/api/v4/memory",
    "/api/v4/inbox",
    "/api/v4/graph",
    "/api/v4/refine",
    "/api/v4/review",
)

# Regex groups for extracting resource IDs from URL patterns
import re

# Matches paths like /api/v4/memory-stores/{store_id}/...
_STORE_ID_PATTERN = re.compile(
    r"^/api/v4/memory-stores/(?P<store_id>[0-9a-fA-F-]{36})"
)

# Matches paths like /api/v4/memory/{memory_id}/...
# but NOT /api/v4/memory-stores/... or /api/v4/memory/index/...
_MEMORY_ID_PATTERN = re.compile(
    r"^/api/v4/memory/(?P<memory_id>[0-9a-fA-F-]{36})(?:/|$)"
)


class StoreAccessMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that enforces memory-store isolation.

    Intercepts all requests to protected paths (``/api/v4/memory*`` etc.)
    and verifies that agent-authenticated requests can only access stores
    and memories that belong to them.

    User-session requests and unauthenticated requests pass through unchanged.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # ── Skip non-protected paths ────────────────────────────────────
        if not any(path.startswith(p) for p in _PROTECTED_PATH_PREFIXES):
            return await call_next(request)

        # ── Only check agent-authenticated requests ─────────────────────
        token = extract_agent_token(request)
        if token is None:
            # User session or anonymous — pass through
            return await call_next(request)

        # ── Authenticate the agent with a short-lived DB session ────────
        db = SessionLocal()
        try:
            agent = authenticate_agent_token(db, token)
            if agent is None:
                # Invalid/expired token — let the route handler return 401
                return await call_next(request)

            # ── Extract resource ID from the URL ────────────────────────────

            # Check for store_id in path
            m = _STORE_ID_PATTERN.match(path)
            if m:
                store_id_str = m.group("store_id")
                try:
                    store_id = UUID(store_id_str)
                    check_store_ownership(store_id, agent, db)
                except ValueError:
                    # Invalid UUID — let the route handler deal with it
                    pass
                except ApiError:
                    return self._forbidden_response()

                return await call_next(request)

            # Check for memory_id in path
            m = _MEMORY_ID_PATTERN.match(path)
            if m:
                memory_id_str = m.group("memory_id")
                try:
                    memory_id = UUID(memory_id_str)
                    check_memory_store_ownership(memory_id, agent, db)
                except ValueError:
                    pass
                except ApiError:
                    return self._forbidden_response()

                return await call_next(request)

            # List / create endpoints — pass through (routes filter themselves)
            return await call_next(request)
        finally:
            db.close()

    @staticmethod
    def _forbidden_response() -> JSONResponse:
        """Return a 403 JSON error response."""
        from uuid import uuid4
        rid = uuid4()
        return JSONResponse(
            status_code=403,
            content=error_envelope(
                request_id=rid,
                correlation_id=rid,
                code="permission_denied",
                message="Agent does not have access to this resource",
                details={},
            ),
        )
