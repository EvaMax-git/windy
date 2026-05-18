from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from mneme.api.context import install_request_context_middleware
from mneme.api.dependencies.store_access import StoreAccessMiddleware
from mneme.api.errors import install_exception_handlers
from mneme.api.router import api_v4_router
from mneme.api.routes.system.health import mark_startup_complete
from mneme.api.schemas import error_envelope
from mneme.config import get_settings
from mneme.db.auth import bootstrap_owner_if_configured
from mneme.db.base import check_database_connection
from mneme.db.pipelines import seed_default_asset_import_pipelines
from mneme.db.sub_library_registry import bootstrap_sub_libraries
from mneme.observability.logging import AccessLogMiddleware, configure_logging
from mneme.observability.metrics import install_metrics_endpoint, install_metrics_middleware
from mneme.observability.health import check_database, check_redis, check_outbox_pending, DependencyStatus
from mneme.schemas.health import HealthState, HealthLiveData, HealthReadyData, HealthStartupData


def _frontend_dir() -> Path:
    package_dir = Path(__file__).parent
    for candidate in (package_dir / "web" / "dist", package_dir / "static"):
        if (candidate / "index.html").is_file():
            return candidate
    return package_dir / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_database_connection()
    bootstrap_owner_if_configured()
    bootstrap_sub_libraries()
    seed_default_asset_import_pipelines()
    mark_startup_complete()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="Mneme API", version="0.1.0", lifespan=lifespan)

    # Middleware order matters: context first, then metrics, then CORS, then access log.
    install_request_context_middleware(app)
    install_metrics_middleware(app)

    # CORS — allow browser-based access from the Vite dev server and peers.
    # In production this should be tightened to the specific frontend origin.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.frontend_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Store-access isolation middleware (agent A cannot read agent B's store)
    app.add_middleware(StoreAccessMiddleware)

    app.add_middleware(AccessLogMiddleware)
    install_exception_handlers(app)

    app.include_router(api_v4_router)

    # -- static files ---------------------------------------------------------
    _static_dir = _frontend_dir()
    if _static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
    _assets_dir = _static_dir / "assets"
    if _assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

    def _frontend_index():
        index_path = _static_dir / "index.html"
        if index_path.is_file():
            return FileResponse(index_path)
        return JSONResponse(
            status_code=404,
            content={"detail": "Frontend index.html not found"},
        )

    # -- service info / dashboard ---------------------------------------------

    @app.get("/", tags=["service"])
    def service_root(request: Request):
        """Return the dashboard HTML when the browser requests text/html,
        otherwise return the service-info JSON (for API clients).
        """
        accept = request.headers.get("accept", "")
        if not accept or "text/html" in accept or "*/*" in accept:
            return _frontend_index()
        return {
            "service": "Mneme",
            "version": app.version,
            "health": "/health/live",
            "docs": "/docs",
            "dashboard": "/ (Accept: text/html)",
        }

    @app.get("/login", include_in_schema=False)
    def spa_login():
        return _frontend_index()

    @app.get("/app", include_in_schema=False)
    @app.get("/app/{path:path}", include_in_schema=False)
    def spa_app(path: str = ""):
        return _frontend_index()

    @app.get("/test-chat.html", include_in_schema=False)
    def test_chat_page():
        test_page = _static_dir / "test-chat.html"
        if test_page.is_file():
            return FileResponse(test_page)
        return JSONResponse(status_code=404, content={"detail": "test-chat.html not found"})

    # -- standalone health endpoints (outside /api/v4) -----------------------

    @app.get("/health/live", tags=["health"])
    def live_root() -> dict[str, str]:
        return {"status": "ok", "environment": settings.environment}

    @app.get("/health/startup", tags=["health"])
    def startup_root() -> dict:
        """Return 200 when startup is complete, 503 otherwise."""
        from mneme.api.routes.system.health import is_startup_complete
        if is_startup_complete():
            return {"status": "ok", "migrations": "applied"}
        from fastapi.responses import JSONResponse
        _request_id = uuid4()
        return JSONResponse(
            status_code=503,
            content=error_envelope(
                request_id=_request_id,
                correlation_id=_request_id,
                code="dependency_unavailable",
                message="应用程序启动尚未完成",
                details={"status": "unavailable", "migrations": "pending"},
            ),
        )

    @app.get("/health/ready", tags=["health"])
    def ready_root() -> dict:
        """Readiness probe with DB + Redis checks."""
        db_status = check_database()
        if db_status == DependencyStatus.unavailable:
            from fastapi.responses import JSONResponse
            _request_id = uuid4()
            return JSONResponse(
                status_code=503,
                content=error_envelope(
                    request_id=_request_id,
                    correlation_id=_request_id,
                    code="dependency_unavailable",
                    message="数据库不可用",
                    details={"database": "unavailable"},
                ),
            )

        redis_status = check_redis()
        outbox_pending = check_outbox_pending()
        overall = "degraded" if redis_status == DependencyStatus.degraded else "ok"

        result = {
            "status": overall,
            "database": db_status,
            "redis": redis_status,
            "outbox_pending": outbox_pending,
        }
        # Return 503 when database is down (already handled above);
        # degraded redis → still 200 per Phase 1 strategy.
        return result

    # -- standalone metrics endpoint -----------------------------------------
    install_metrics_endpoint(app)

    # -- standalone feature flags endpoint (mirrors /health pattern) -----------

    @app.get("/health/features", tags=["feature-flags"])
    def feature_flags_root() -> dict:
        """Return currently active feature flags."""
        return {
            "legacy_redirects": settings.feature_legacy_redirects,
        }

    return app


app = create_app()
