from __future__ import annotations

from uuid import uuid4

from mneme.schemas import (
    AgentTokenRead,
    ErrorBody,
    ErrorCode,
    LoginRequest,
    LoginResponse,
    ObjectRegistryRead,
    ResponseEnvelope,
    UserSessionRead,
    UserRead,
    WriteRequestHeaders,
)


def test_write_request_headers_use_http_header_aliases() -> None:
    request_id = uuid4()
    headers = WriteRequestHeaders.model_validate(
        {
            "X-Request-Id": str(request_id),
            "Idempotency-Key": "auth-login-1",
        }
    )

    assert headers.x_request_id == request_id
    assert headers.idempotency_key == "auth-login-1"
    assert headers.model_dump(by_alias=True)["Idempotency-Key"] == "auth-login-1"


def test_common_envelope_schema_is_generatable() -> None:
    schema = ResponseEnvelope[dict[str, str]].model_json_schema()

    assert schema["properties"]["request_id"]["format"] == "uuid"
    assert "data" in schema["properties"]


def test_error_code_baseline_contains_phase1_contract_codes() -> None:
    schema = ErrorBody.model_json_schema()

    assert set(schema["$defs"]["ErrorCode"]["enum"]) >= {
        ErrorCode.bad_request.value,
        ErrorCode.auth_required.value,
        ErrorCode.permission_denied.value,
        ErrorCode.idempotency_conflict.value,
        ErrorCode.review_required.value,
        ErrorCode.step_up_required.value,
    }


def test_public_auth_and_agent_schemas_do_not_expose_secret_hash_columns() -> None:
    exported_schema_text = "\n".join(
        str(model.model_json_schema())
        for model in (UserRead, UserSessionRead, LoginRequest, LoginResponse, AgentTokenRead)
    )

    assert "password_hash" not in exported_schema_text
    assert "session_token_hash" not in exported_schema_text
    assert "token_hash" not in exported_schema_text


def test_object_schema_exports_object_registry_contract() -> None:
    schema = ObjectRegistryRead.model_json_schema()

    assert schema["properties"]["object_id"]["format"] == "uuid"
    assert "ObjectType" in schema["$defs"]
    assert "ObjectStatus" in schema["$defs"]


def test_openapi_schema_can_generate_with_phase1_models(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    from mneme.config import get_settings
    from mneme.main import create_app

    get_settings.cache_clear()
    schema = create_app().openapi()

    assert "/api/v4/health/live" in schema["paths"]
    assert "/api/v4/auth/login" in schema["paths"]
    assert "/api/v4/auth/logout" in schema["paths"]
    assert "/api/v4/auth/me" in schema["paths"]
    assert "ResponseEnvelope_HealthLiveData_" in schema["components"]["schemas"]
