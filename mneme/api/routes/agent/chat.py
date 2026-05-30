"""POST /api/v4/chat — 多轮对话端点 (A4 MVP).

链路: 用户消息 → 保存 → 搜索知识 → 组装上下文(含历史) → Gateway → 保存回答 → 返回

支持:
- 新建对话 (不传 conversation_id)
- 继续已有对话 (传 conversation_id)
- 自动保存用户消息和 AI 回答
- 知识库搜索作为上下文补充
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from mneme.api.dependencies.api_key import get_api_consumer
from mneme.api.context import (
    ActorContext,
    RequestContext,
    get_request_context,
    with_actor,
)
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.auth import AuthenticatedSession
from mneme.db.base import get_db
from mneme.db.conversations import create_conversation, get_conversation
from mneme.db.messages import create_message, list_messages
from mneme.knowledge.fts import ensure_fts_index, search_fts
from mneme.knowledge.token_estimator import estimate_tokens
from mneme.schemas import ResponseEnvelope
from mneme.schemas.conversations import (
    ConversationCreateRequest,
    ConversationType,
    MessageCreate,
    RoleCode,
)
from mneme.schemas.common import SensitivityLevel

router = APIRouter(prefix="/chat", tags=["chat"])
logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """你是一个知识库助手，通过多轮对话帮助用户。
要求：
1. 有参考资料时，优先基于参考资料回答，并在回答中标注引用来源
2. 没有参考资料时，用你自身的知识回答用户问题，不要拒绝回答
3. 保持对话连贯性，记住之前的对话内容
4. 回答简洁准确，使用中文"""


# ── Schemas ──────────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    """Request body for POST /api/v4/chat."""
    message: str = Field(..., min_length=1, max_length=4000, description="用户消息")
    conversation_id: UUID | None = Field(None, description="已有对话 ID（留空则新建）")
    project_id: UUID | None = Field(None, description="限定项目范围")
    max_context_chunks: int = Field(5, ge=0, le=20, description="知识库搜索结果数")


class ChatCitation(BaseModel):
    """A single citation."""
    chunk_id: str
    document_title: str
    snippet: str


class ChatResponse(BaseModel):
    """Response body for POST /api/v4/chat."""
    conversation_id: str
    message_id: str
    answer: str
    citations: list[ChatCitation] = Field(default_factory=list)
    model: str | None = None
    degraded: bool = False
    degradation_reason: str | None = None


# ── Endpoint ─────────────────────────────────────────────────────────────


@router.post("", response_model=ResponseEnvelope[ChatResponse], status_code=200)
async def chat(
    payload: ChatRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    consumer=Depends(get_api_consumer),
) -> dict:
    """多轮对话端点。

    流程:
    1. 获取或创建对话
    2. 保存用户消息
    3. 获取对话历史 (最近 10 条)
    4. 搜索知识库获取相关上下文
    5. 组装 prompt (系统 + 知识 + 历史 + 用户消息)
    6. 调用 Gateway
    7. 保存 AI 回答
    8. 返回回答 + 引用
    """
    # Wire actor
    from mneme.db.auth import AuthenticatedSession
    if isinstance(consumer, AuthenticatedSession):
        actor = ActorContext(
            actor_type="user",
            actor_id=consumer.user.user_id,
            auth_context_type="user_session",
            auth_context_id=consumer.session.session_id,
        )
        user_id = consumer.user.user_id
    else:
        actor = ActorContext(
            actor_type="agent",
            actor_id=consumer.agent.agent_id,
            auth_context_type="agent_token",
            auth_context_id=consumer.token.token_id,
        )
        user_id = consumer.agent.agent_id
    context = with_actor(context, actor=actor)

    # Helper: create a fresh context with unique idempotency key for each DB operation
    def _ctx(label: str) -> RequestContext:
        return RequestContext(
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            actor=context.actor,
            idempotency_key=f"{context.idempotency_key or uuid4()}-{label}",
        )

    # 1. Get or create conversation
    conv_id = payload.conversation_id
    if conv_id:
        conv = get_conversation(db, conv_id)
        if conv is None:
            raise ApiError(404, "not_found", f"对话 {conv_id} 不存在")
        # Verify ownership
        if conv.owner_user_id and conv.owner_user_id != user_id:
            raise ApiError(403, "forbidden", "无权访问此对话")
    else:
        if not payload.project_id:
            raise ApiError(400, "bad_request", "新建对话时必须提供 project_id")
        conv = create_conversation(
            db, _ctx("conv"),
            payload=ConversationCreateRequest(
                project_id=payload.project_id,
                conversation_type=ConversationType.chat,
                source_platform="mneme_web",
                title=payload.message[:50],
                sensitivity_level=SensitivityLevel.private,
            ),
        )
        conv_id = conv.conversation_id

    # 2. Get conversation history BEFORE saving new message (avoid duplication)
    history, _ = list_messages(db, conversation_id=conv_id, page=1, page_size=10)
    # list_messages already returns oldest-first (ORDER BY message_time ASC)

    # 3. Save user message
    user_msg = create_message(
        db, _ctx("user-msg"),
        conversation_id=conv_id,
        payload=MessageCreate(
            role_code=RoleCode.user,
            content_text=payload.message,
            message_time=datetime.now(timezone.utc),
        ),
    )

    # 4. Search knowledge for context
    citations: list[ChatCitation] = []
    context_text = ""

    if payload.max_context_chunks > 0:
        try:
            ensure_fts_index(db)
            results, _ = search_fts(
                db,
                query=payload.message.strip(),
                project_id=payload.project_id,
                page=1,
                page_size=payload.max_context_chunks,
            )
            if results:
                parts = []
                for i, r in enumerate(results):
                    parts.append(f"[{i+1}] {r.document_title or '文档'}\n{r.chunk_text}")
                    citations.append(ChatCitation(
                        chunk_id=str(r.chunk_id),
                        document_title=r.document_title or "未命名文档",
                        snippet=(r.chunk_text or "")[:200],
                    ))
                context_text = "\n\n---\n\n".join(parts)
                # Truncate
                if len(context_text) > 8000:
                    context_text = context_text[:8000] + "\n...(已截断)"
        except Exception as exc:
            logger.warning("Knowledge search failed in chat: %s", exc)

    # 5. Build messages for Gateway
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]

    if context_text:
        messages.append({"role": "system", "content": f"参考资料:\n{context_text}"})

    # Add conversation history
    for msg in history:
        role = msg.role_code.value if hasattr(msg.role_code, "value") else str(msg.role_code)
        content = msg.content_text or ""
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    # Append current user message
    messages.append({"role": "user", "content": payload.message})

    # 6. Try Gateway call
    answer_text = ""
    model_name = None
    degraded = False
    degradation_reason = None

    try:
        from mneme.gateway import get_gateway

        gw = get_gateway()
        result = gw.call(
            capability_code="chat.completion",
            params={
                "messages": messages,
                "max_tokens": 2048,
                "temperature": 0.3,
            },
            project_id=payload.project_id,
            sensitivity="private",
            actor_type="user",
            actor_id=user_id,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            idempotency_key=str(context.idempotency_key or uuid4()),
        )

        resp_data = result.get("data", {})
        choices = resp_data.get("choices", [])
        if choices:
            answer_text = choices[0].get("message", {}).get("content", "")
        model_name = resp_data.get("model")

    except Exception as exc:
        logger.warning("Gateway call failed in chat, degrading: %s", exc)
        degraded = True
        degradation_reason = type(exc).__name__
        answer_text = "（AI 服务暂不可用）\n\n"

        if citations:
            answer_text += "以下是搜索到的相关内容：\n"
            for i, c in enumerate(citations[:3]):
                answer_text += f"\n[{i+1}] {c.document_title}: {c.snippet}"
        else:
            answer_text += "未找到相关内容。"

    # 7. Save assistant message
    assistant_msg = create_message(
        db, _ctx("asst-msg"),
        conversation_id=conv_id,
        payload=MessageCreate(
            role_code=RoleCode.assistant,
            content_text=answer_text,
            message_time=datetime.now(timezone.utc),
        ),
    )

    # 8. Return
    resp = ChatResponse(
        conversation_id=str(conv_id),
        message_id=str(assistant_msg.message_id),
        answer=answer_text,
        citations=citations,
        model=model_name,
        degraded=degraded,
        degradation_reason=degradation_reason,
    )

    return envelope(
        jsonable_encoder(resp.model_dump(mode="json")),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
