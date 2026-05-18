from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID, uuid4

from fastapi import Request
from fastapi.responses import JSONResponse

from mneme.api.schemas import error_envelope


REQUEST_ID_HEADER = "X-Request-Id"
CORRELATION_ID_HEADER = "X-Correlation-Id"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"


@dataclass(frozen=True)
class ActorContext:
    actor_type: str = "system"
    actor_id: UUID | None = None
    auth_context_type: str | None = None
    auth_context_id: UUID | None = None


@dataclass(frozen=True)
class RequestContext:
    request_id: UUID
    correlation_id: UUID
    actor: ActorContext
    idempotency_key: str | None = None


class InvalidRequestContext(ValueError):
    def __init__(self, header_name: str, header_value: str) -> None:
        super().__init__(f"{header_name} must be a UUID")
        self.header_name = header_name
        self.header_value = header_value


_request_context: ContextVar[RequestContext | None] = ContextVar("request_context", default=None)


def _parse_uuid_header(header_name: str, value: str | None) -> UUID | None:
    if value is None or value == "":
        return None
    try:
        return UUID(value)
    except ValueError as exc:
        raise InvalidRequestContext(header_name, value) from exc


def build_request_context(request: Request) -> RequestContext:
    request_id = _parse_uuid_header(REQUEST_ID_HEADER, request.headers.get(REQUEST_ID_HEADER)) or uuid4()
    correlation_id = (
        _parse_uuid_header(CORRELATION_ID_HEADER, request.headers.get(CORRELATION_ID_HEADER))
        or request_id
    )
    return RequestContext(
        request_id=request_id,
        correlation_id=correlation_id,
        actor=ActorContext(),
        idempotency_key=request.headers.get(IDEMPOTENCY_KEY_HEADER),
    )


def get_current_context() -> RequestContext:
    context = _request_context.get()
    if context is None:
        generated_id = uuid4()
        return RequestContext(
            request_id=generated_id,
            correlation_id=generated_id,
            actor=ActorContext(),
        )
    return context


def peek_request_context() -> RequestContext | None:
    return _request_context.get()


def with_actor(context: RequestContext, /, *, actor: ActorContext) -> RequestContext:
    """Return a new ``RequestContext`` with *actor* replaced, preserving all
    other fields (request_id, correlation_id, idempotency_key).

    Use this when an auth dependency resolves the authenticated principal so
    that downstream writes (e.g. object registry, audit) carry the correct
    ``actor_type`` and ``actor_id``.
    """
    return RequestContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        actor=actor,
        idempotency_key=context.idempotency_key,
    )


def get_request_context(request: Request) -> RequestContext:
    context = getattr(request.state, "context", None)
    if context is None:
        return get_current_context()
    return context


def install_request_context_middleware(app) -> None:
    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        try:
            context = build_request_context(request)
        except InvalidRequestContext as exc:
            fallback_id = uuid4()
            return JSONResponse(
                status_code=400,
                content=error_envelope(
                    request_id=fallback_id,
                    correlation_id=fallback_id,
                    code="bad_request",
                    message=str(exc),
                    details={"header": exc.header_name, "value": exc.header_value},
                ),
                headers={
                    REQUEST_ID_HEADER: str(fallback_id),
                    CORRELATION_ID_HEADER: str(fallback_id),
                },
            )

        token = _request_context.set(context)
        request.state.context = context
        try:
            response = await call_next(request)
        finally:
            _request_context.reset(token)
        response.headers[REQUEST_ID_HEADER] = str(context.request_id)
        response.headers[CORRELATION_ID_HEADER] = str(context.correlation_id)
        return response
