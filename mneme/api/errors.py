from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from mneme.api.context import get_request_context
from mneme.api.schemas import error_envelope


HTTP_STATUS_TO_CODE = {
    400: "bad_request",
    401: "auth_required",
    403: "permission_denied",
    409: "idempotency_conflict",
    422: "review_required",
    429: "rate_limited",
    503: "dependency_unavailable",
}


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def _context_ids(request: Request):
    context = getattr(request.state, "context", None)
    if context is not None:
        return context.request_id, context.correlation_id
    generated_id = uuid4()
    return generated_id, generated_id


def _json_error(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    request_id, correlation_id = _context_ids(request)
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(
            error_envelope(
                request_id=request_id,
                correlation_id=correlation_id,
                code=code,
                message=message,
                details=details,
            )
        ),
    )


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return _json_error(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _json_error(
            request,
            status_code=400,
            code="bad_request",
            message="请求参数验证失败",
            details={"errors": exc.errors()},
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        code = HTTP_STATUS_TO_CODE.get(exc.status_code, "bad_request")
        message = exc.detail if isinstance(exc.detail, str) else "请求失败"
        details = exc.detail if isinstance(exc.detail, dict) else {}
        return _json_error(
            request,
            status_code=exc.status_code,
            code=code,
            message=message,
            details=details,
        )

    @app.exception_handler(SQLAlchemyError)
    async def sqlalchemy_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        context = get_request_context(request)
        return _json_error(
            request,
            status_code=503,
            code="dependency_unavailable",
            message="数据库不可用",
            details={"request_id": str(context.request_id)},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        return _json_error(
            request,
            status_code=500,
            code="internal_error",
            message="服务器内部错误",
        )
