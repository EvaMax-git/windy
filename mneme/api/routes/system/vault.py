"""P2-08/P2-09/P2-10 凭据保险库 API — 凭据 CRUD、解密查看、轮换、访问日志。

接口列表
--------
* ``POST   /api/v4/vault/credentials``              – 创建凭据
* ``GET    /api/v4/vault/credentials``              – 分页列表（脱敏）
* ``GET    /api/v4/vault/credentials/{id}``         – 单条凭据详情（不含明文）
* ``POST   /api/v4/vault/credentials/{id}/reveal``  – 解密并显示明文（需升级认证 + 审核 + 审计）
* ``PUT    /api/v4/vault/credentials/{id}``         – 更新凭据（轮换 / 变更状态 / 范围）
* ``DELETE /api/v4/vault/credentials/{id}``         – 删除凭据
* ``GET    /api/v4/vault/credentials/{id}/access-logs`` – 分页访问日志
"""

from __future__ import annotations

import math
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope, error_envelope
from mneme.db.base import SessionLocal
from mneme.db.vault import (
    create_credential,
    delete_credential,
    get_credential_by_id,
    get_credentials,
    get_vault_access_logs,
    mark_credential_used,
    rotate_credential,
    update_credential,
)
from mneme.db.review_items import get_review_item_by_id
from mneme.db.audit import add_audit_event
from mneme.schemas import (
    PageInfo,
    PaginationParams,
    ResponseEnvelope,
)
from mneme.schemas.vault import (
    CredentialCreate,
    CredentialFilterParams,
    CredentialListResponse,
    CredentialRead,
    CredentialRevealRequest,
    CredentialRevealResponse,
    CredentialUpdate,
    VaultAccessLogFilterParams,
    VaultAccessLogListResponse,
    VaultAccessLogRead,
)
from mneme.security.audit import (
    audit_event_for_action,
)
from mneme.security.policy import (
    Action,
    Actor,
    Decision,
    Object,
    PolicyContext,
    can as policy_can,
)
from mneme.security.review_router import handle_review_required
from mneme.vault.access_log import write_vault_access_log
from mneme.vault.encryption import get_vault_encryption

router = APIRouter(prefix="/vault/credentials", tags=["vault"])


# ═══════════════════════════════════════════════════════════════════════════════
# POST /vault/credentials
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("", response_model=ResponseEnvelope[CredentialRead], status_code=201)
def create_credential_endpoint(
    body: CredentialCreate,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """创建新的加密凭据。

    ``plaintext`` 明文值在存储前使用信封加密 **立即加密**。
    它不会被记录或在任何 API 响应中返回。
    其他 API 响应中返回。
    """
    vault = get_vault_encryption()

    # 1. Encrypt the plaintext
    try:
        plaintext_bytes = body.plaintext.encode("utf-8")
        ciphertext, key_wrap, fingerprint = vault.encrypt(plaintext_bytes)
    except ValueError as exc:
        raise ApiError(
            400,
            "bad_request",
            f"加密失败: {exc}",
        )

    # 2. Store in database
    try:
        row = create_credential(
            provider_id=body.provider_id,
            credential_name=body.credential_name,
            credential_type=body.credential_type.value,
            ciphertext=ciphertext,
            key_wrap=key_wrap,
            key_version=vault.key_version,
            fingerprint=fingerprint,
            scope_json=body.scope_json,
            metadata_json=body.metadata_json,
            created_by_user_id=context.actor.actor_id,
        )
    except IntegrityError:
        raise ApiError(
            409,
            "idempotency_conflict",
            f"credential '{body.credential_name}' already exists for provider",
        )

    credential_id = UUID(row["credential_id"])

    # 3. Write audit + access log
    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="vault.credential.created",
                result="success",
                object_type="credential",
                object_id=credential_id,
                diff_summary={
                    "credential_name": body.credential_name,
                    "credential_type": body.credential_type.value,
                    "key_version": vault.key_version,
                },
            ),
        )
        db.commit()

    write_vault_access_log(
        credential_id=credential_id,
        actor_type=context.actor.actor_type,
        actor_id=context.actor.actor_id,
        auth_context_type=context.actor.auth_context_type,
        auth_context_id=context.actor.auth_context_id,
        action="create",
        result="success",
        provider_id=body.provider_id,
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        target_scope=body.scope_json,
    )

    item = CredentialRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# GET /vault/credentials
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("", response_model=ResponseEnvelope[CredentialListResponse])
def list_credentials(
    pagination: PaginationParams = Depends(),
    filters: CredentialFilterParams = Depends(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """列出凭据，支持可选过滤。

    **此接口不会返回任何明文或密文。**
    仅可见元数据（名称、类型、状态、指纹、范围等）。

    查询参数
    ----------------
    * ``page`` / ``page_size`` — 分页（默认 1 / 50，最大 200）。
    * ``provider_id`` — 按 Provider 过滤。
    * ``credential_type`` — 按类型过滤（api_key, oauth, cert, secret）。
    * ``status`` — 按状态过滤（active, disabled, rotated, revoked）。
    * ``created_after`` / ``created_before`` — 时间区间过滤。
    """
    rows, total = get_credentials(
        page=pagination.page,
        page_size=pagination.page_size,
        provider_id=filters.provider_id,
        credential_type=filters.credential_type.value if filters.credential_type else None,
        status=filters.status.value if filters.status else None,
        created_after=filters.created_after,
        created_before=filters.created_before,
    )

    items = [CredentialRead(**row) for row in rows]
    total_pages = max(1, math.ceil(total / max(pagination.page_size, 1)))

    page_info = PageInfo(
        page=pagination.page,
        page_size=pagination.page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=pagination.page < total_pages,
        has_previous=pagination.page > 1,
    )

    data = CredentialListResponse(items=items, page_info=page_info)
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# GET /vault/credentials/{credential_id}
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/{credential_id}", response_model=ResponseEnvelope[CredentialRead])
def get_credential_detail(
    credential_id: UUID,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """通过主键返回单个凭据。

    **不含明文。** 如需查看明文，请使用
    ``POST /vault/credentials/{id}/reveal`` 接口。
    """
    row = get_credential_by_id(credential_id)
    if row is None:
        raise ApiError(
            404,
            "bad_request",
            f"凭据 'credential_id' 未找到",
        )

    # Remove ciphertext/key_wrap from response
    row.pop("ciphertext", None)
    row.pop("key_wrap", None)

    item = CredentialRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# POST /vault/credentials/{credential_id}/reveal
# ═══════════════════════════════════════════════════════════════════════════════


@router.post(
    "/{credential_id}/reveal",
    response_model=ResponseEnvelope[CredentialRevealResponse],
)
def reveal_credential(
    credential_id: UUID,
    body: CredentialRevealRequest = CredentialRevealRequest(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """解密并显示凭据的明文值。

    **这是高度敏感的接口。** 必须通过
    升级认证保护，所有访问都会记录在
    ``audit_events`` 和 ``vault_access_logs`` 中。

    P2-10: 解密查看受策略引擎控制。如果策略返回
    ``review_required``，将自动创建 ``review_item`` 并返回
    202 响应及 ``review_item_id``。调用方必须
    经过审核工作流后明文才会被释放。

    请求体中的 ``reason`` 字段会记录在审计日志中。

    必需的安全上下文
    -------------------------
    * 升级认证（已验证的会话）
    * 对 ``vault.credential.reveal`` 操作的策略检查
    * 完整的审计日志
    * 审核控制 (P2-10)
    """
    # 1. Fetch credential with secrets
    row = get_credential_by_id(credential_id)
    if row is None:
        write_vault_access_log(
            credential_id=credential_id,
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            action="access_denied",
            result="failed",
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            reason_code="not_found",
        )
        raise ApiError(
            404,
            "bad_request",
            f"凭据 'credential_id' 未找到",
        )

    # 2. Check status
    if row["status"] in ("revoked",):
        write_vault_access_log(
            credential_id=credential_id,
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            action="access_denied",
            result="denied",
            provider_id=UUID(row["provider_id"]),
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            reason_code="credential_revoked",
        )
        raise ApiError(
            403,
            "permission_denied",
            f"credential '{credential_id}' is revoked",
        )

    # 3. P2-10: If a review_item_id is provided and the review was approved
    #    for this credential, skip the policy gate and proceed to decryption.
    review_preapproved = False
    if body.review_item_id is not None:
        review_row = get_review_item_by_id(body.review_item_id)
        if review_row is None:
            raise ApiError(
                404,
                "bad_request",
                f"审核项 'body.review_item_id' 未找到",
            )
        # Verify the review is for this credential and was approved
        if (
            review_row["target_type"] == "credential"
            and str(review_row["target_id"]) == str(credential_id)
            and review_row["status"] == "approved"
            and review_row["decision"] == "approved"
        ):
            review_preapproved = True
            # Mark the review as having been used (via audit)
            with SessionLocal() as db:
                add_audit_event(
                    db,
                    context,
                    audit_event_for_action(
                        action="vault.credential.revealed_after_review",
                        result="success",
                        object_type="credential",
                        object_id=credential_id,
                        sensitivity_level="secret",
                        metadata_json={
                            "review_item_id": str(body.review_item_id),
                            "credential_name": row["credential_name"],
                        },
                    ),
                )
                db.commit()
        else:
            raise ApiError(
                422,
                "review_required",
                f"review_item '{body.review_item_id}' is not an approved "
                f"review for credential '{credential_id}' "
                f"(status={review_row['status']}, decision={review_row['decision']})",
            )

    # 4. P2-10: Policy Engine check — reveal is a high-risk operation
    #    Skip the policy check if the review was already approved.
    if not review_preapproved:
        policy_actor = Actor(
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
        )
        policy_action = Action(name="vault.credential.reveal", requires_step_up=True)
        policy_object = Object(
            object_type="credential",
            object_id=credential_id,
            sensitivity_level="secret",
        )
        policy_ctx = PolicyContext(
            request_id=context.request_id,
            correlation_id=context.correlation_id,
        )

        decision = policy_can(policy_actor, policy_action, policy_object, policy_ctx)

        # 4a. If review is required, auto-create a review_item and return 202
        if decision.decision == Decision.review_required:
            enriched = handle_review_required(
                decision=decision,
                actor=policy_actor,
                action=policy_action,
                object=policy_object,
                context=policy_ctx,
            )

            write_vault_access_log(
                credential_id=credential_id,
                actor_type=context.actor.actor_type,
                actor_id=context.actor.actor_id,
                auth_context_type=context.actor.auth_context_type,
                auth_context_id=context.actor.auth_context_id,
                action="access_denied",
                result="denied",
                provider_id=UUID(row["provider_id"]),
                request_id=context.request_id,
                correlation_id=context.correlation_id,
                reason_code="review_required",
                metadata_json={
                    "review_item_id": str(enriched.review_item_id),
                    "reveal_reason": body.reason,
                },
            )

            # Return 202 Accepted — plaintext not released until review is approved
            return JSONResponse(
                status_code=202,
                content=envelope(
                    {
                        "credential_id": str(credential_id),
                        "credential_name": row["credential_name"],
                        "plaintext": None,
                        "review_required": True,
                        "review_item_id": str(enriched.review_item_id),
                        "message": (
                            "凭据查看需要审核批准。 "
                            "审核批准后明文将可用。"
                        ),
                    },
                    request_id=context.request_id,
                    correlation_id=context.correlation_id,
                ),
            )

        # 4b. If denied for any other reason, reject the request
        if decision.decision == Decision.deny:
            write_vault_access_log(
                credential_id=credential_id,
                actor_type=context.actor.actor_type,
                actor_id=context.actor.actor_id,
                action="access_denied",
                result="denied",
                provider_id=UUID(row["provider_id"]),
                request_id=context.request_id,
                correlation_id=context.correlation_id,
                reason_code=decision.deny_reason.value if decision.deny_reason else "policy_denied",
            )
            raise ApiError(
                403,
                "permission_denied",
                decision.message or "策略拒绝凭据查看",
            )

        # 4c. If step_up_required, reject
        if decision.decision == Decision.step_up_required:
            write_vault_access_log(
                credential_id=credential_id,
                actor_type=context.actor.actor_type,
                actor_id=context.actor.actor_id,
                action="access_denied",
                result="denied",
                provider_id=UUID(row["provider_id"]),
                request_id=context.request_id,
                correlation_id=context.correlation_id,
                reason_code="step_up_required",
            )
            raise ApiError(
                403,
                "step_up_required",
                decision.message or "凭据查看需要升级认证",
            )

    # 5. Policy allows (or review was pre-approved) — proceed with decryption
    vault = get_vault_encryption()
    try:
        ciphertext = row["ciphertext"]
        key_wrap = row["key_wrap"]

        if not isinstance(ciphertext, bytes):
            ciphertext = bytes(ciphertext)
        if not isinstance(key_wrap, bytes):
            key_wrap = bytes(key_wrap)

        plaintext = vault.decrypt(ciphertext, key_wrap)
    except Exception as exc:
        write_vault_access_log(
            credential_id=credential_id,
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            action="access_denied",
            result="failed",
            provider_id=UUID(row["provider_id"]),
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            reason_code="decryption_failed",
            metadata_json={"error": str(exc)},
        )
        raise ApiError(
            500,
            "internal_error",
            "凭据解密失败 — KEK 可能已被轮换 "
            "or the stored data is corrupted",
        )

    # 6. Verify fingerprint
    if not vault.verify_fingerprint(plaintext, row["fingerprint"]):
        write_vault_access_log(
            credential_id=credential_id,
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            action="access_denied",
            result="failed",
            provider_id=UUID(row["provider_id"]),
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            reason_code="fingerprint_mismatch",
        )
        raise ApiError(
            500,
            "internal_error",
            "凭据完整性校验失败 — 存储的指纹不匹配",
        )

    # 7. Audit + access log
    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="vault.credential.revealed",
                result="success",
                object_type="credential",
                object_id=credential_id,
                reason_code=body.reason,
                sensitivity_level="secret",
                metadata_json={
                    "credential_name": row["credential_name"],
                    "credential_type": row["credential_type"],
                    "reveal_reason": body.reason,
                },
            ),
        )
        db.commit()

    write_vault_access_log(
        credential_id=credential_id,
        actor_type=context.actor.actor_type,
        actor_id=context.actor.actor_id,
        auth_context_type=context.actor.auth_context_type,
        auth_context_id=context.actor.auth_context_id,
        action="use",
        result="success",
        provider_id=UUID(row["provider_id"]),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )

    # Mark last_used_at
    mark_credential_used(credential_id)

    # 8. Return plaintext
    response_data = CredentialRevealResponse(
        credential_id=credential_id,
        credential_name=row["credential_name"],
        credential_type=row["credential_type"],
        plaintext=plaintext.decode("utf-8"),
        fingerprint=row["fingerprint"],
    )

    return envelope(
        response_data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# PUT /vault/credentials/{credential_id}
# ═══════════════════════════════════════════════════════════════════════════════


@router.put("/{credential_id}", response_model=ResponseEnvelope[CredentialRead])
def update_credential_endpoint(
    credential_id: UUID,
    body: CredentialUpdate,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """更新凭据。支持轮换、状态变更、范围变更。

    如果提供了 ``plaintext``，凭据将**轮换**：生成新的
    DEK 并加密新值。
    ``credential_vault.rotated_at`` 时间戳会更新。

    如果仅提供 ``status``、``scope_json`` 或 ``metadata_json``，
    这些字段将在不重新加密的情况下更新。
    """
    # 1. Fetch existing
    row = get_credential_by_id(credential_id)
    if row is None:
        raise ApiError(
            404,
            "bad_request",
            f"凭据 'credential_id' 未找到",
        )

    vault = get_vault_encryption()

    if body.plaintext is not None:
        # ── Rotation path ─────────────────────────────────────────────────
        try:
            plaintext_bytes = body.plaintext.encode("utf-8")
            ciphertext, key_wrap, fingerprint = vault.encrypt(plaintext_bytes)
        except ValueError as exc:
            raise ApiError(
                400,
                "bad_request",
                f"加密失败: {exc}",
            )

        success = rotate_credential(
            credential_id=credential_id,
            ciphertext=ciphertext,
            key_wrap=key_wrap,
            key_version=vault.key_version,
            fingerprint=fingerprint,
            status=body.status.value if body.status else None,
            scope_json=body.scope_json,
            metadata_json=body.metadata_json,
        )

        action = "rotate"
        diff = {
            "rotated": True,
            "key_version": vault.key_version,
        }
        if body.status:
            diff["status"] = body.status.value
    else:
        # ── Metadata-update path (no re-encryption) ───────────────────────
        success = update_credential(
            credential_id=credential_id,
            status=body.status.value if body.status else None,
            scope_json=body.scope_json,
            metadata_json=body.metadata_json,
        )
        # Map status change to a DDL-valid action (CHECK constraint:
        # create|enable|disable|rotate|revoke|export|use|access_denied)
        _status_to_action = {
            "active": "enable",
            "disabled": "disable",
            "revoked": "revoke",
        }
        if body.status:
            action = _status_to_action.get(body.status.value, "enable")
        else:
            # scope or metadata-only change — closest valid action is "rotate"
            action = "rotate"

        diff = {}
        if body.status:
            diff["status"] = body.status.value
        if body.scope_json is not None:
            diff["scope_updated"] = True
        if body.metadata_json is not None:
            diff["metadata_updated"] = True

    if not success:
        raise ApiError(
            409,
            "bad_request",
            f"credential '{credential_id}' could not be updated",
        )

    # 2. Reload
    row = get_credential_by_id(credential_id)
    row.pop("ciphertext", None)
    row.pop("key_wrap", None)

    # 3. Audit + access log
    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action=f"vault.credential.{action}d",
                result="success",
                object_type="credential",
                object_id=credential_id,
                sensitivity_level="secret",
                diff_summary=diff,
            ),
        )
        db.commit()

    write_vault_access_log(
        credential_id=credential_id,
        actor_type=context.actor.actor_type,
        actor_id=context.actor.actor_id,
        auth_context_type=context.actor.auth_context_type,
        auth_context_id=context.actor.auth_context_id,
        action=action,
        result="success",
        provider_id=UUID(row["provider_id"]),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )

    item = CredentialRead(**row)
    return envelope(
        item.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# DELETE /vault/credentials/{credential_id}
# ═══════════════════════════════════════════════════════════════════════════════


@router.delete("/{credential_id}", response_model=ResponseEnvelope[dict])
def delete_credential_endpoint(
    credential_id: UUID,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """永久删除凭据。

    这是破坏性操作。已删除的凭据无法恢复。
    建议使用 ``status: revoked`` 进行
    非破坏性停用。
    """
    row = get_credential_by_id(credential_id)
    if row is None:
        raise ApiError(
            404,
            "bad_request",
            f"凭据 'credential_id' 未找到",
        )

    provider_id = UUID(row["provider_id"])
    credential_name = row["credential_name"]

    success = delete_credential(credential_id)
    if not success:
        raise ApiError(
            409,
            "bad_request",
            f"credential '{credential_id}' could not be deleted",
        )

    # Audit + access log (write BEFORE delete so credential_id is valid)
    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="vault.credential.deleted",
                result="success",
                object_type="credential",
                object_id=credential_id,
                sensitivity_level="secret",
                diff_summary={
                    "credential_name": credential_name,
                    "deleted": True,
                },
            ),
        )
        db.commit()

    write_vault_access_log(
        credential_id=credential_id,
        actor_type=context.actor.actor_type,
        actor_id=context.actor.actor_id,
        auth_context_type=context.actor.auth_context_type,
        auth_context_id=context.actor.auth_context_id,
        action="revoke",
        result="success",
        provider_id=provider_id,
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )

    return envelope(
        {"deleted": True, "credential_id": str(credential_id)},
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# GET /vault/credentials/{credential_id}/access-logs
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "/{credential_id}/access-logs",
    response_model=ResponseEnvelope[VaultAccessLogListResponse],
)
def list_access_logs(
    credential_id: UUID,
    pagination: PaginationParams = Depends(),
    filters: VaultAccessLogFilterParams = Depends(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """返回凭据的分页访问日志。

    **访问日志条目中绝不包含明文凭据。**

    查询参数
    ----------------
    * ``page`` / ``page_size`` — 分页（默认 1 / 50，最大 200）。
    * ``action`` — 按操作过滤（create, enable, disable, rotate, ...）。
    * ``result`` — 按结果过滤（success, denied, failed）。
    * ``occurred_after`` / ``occurred_before`` — 时间区间过滤。
    """
    # Verify credential exists
    row = get_credential_by_id(credential_id)
    if row is None:
        raise ApiError(
            404,
            "bad_request",
            f"凭据 'credential_id' 未找到",
        )

    rows, total = get_vault_access_logs(
        credential_id=credential_id,
        page=pagination.page,
        page_size=pagination.page_size,
        action=filters.action.value if filters.action else None,
        result=filters.result.value if filters.result else None,
        occurred_after=filters.occurred_after,
        occurred_before=filters.occurred_before,
    )

    items = [VaultAccessLogRead(**row) for row in rows]
    total_pages = max(1, math.ceil(total / max(pagination.page_size, 1)))

    page_info = PageInfo(
        page=pagination.page,
        page_size=pagination.page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=pagination.page < total_pages,
        has_previous=pagination.page > 1,
    )

    data = VaultAccessLogListResponse(items=items, page_info=page_info)
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
