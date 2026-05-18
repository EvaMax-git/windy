"""API dependencies — reusable FastAPI dependencies for auth, access control, etc."""

from mneme.api.dependencies.store_access import (
    check_store_access_middleware,
    check_store_ownership,
    check_memory_store_ownership,
    StoreAccessMiddleware,
)
