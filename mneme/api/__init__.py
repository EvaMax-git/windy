"""FastAPI v4 API foundation.

Public API
----------
* ``RequestContext`` — per-request actor / idempotency / tracing context
* ``router`` — assembled FastAPI ``APIRouter`` for all v4 endpoints
* ``mneme.api.routes`` — individual route modules (agents, memory, etc.)
"""

from mneme.api.context import RequestContext

__all__ = [
    "RequestContext",
]
