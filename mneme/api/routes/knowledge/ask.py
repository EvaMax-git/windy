"""POST /api/v4/ask — AI 回答端点 (A3 MVP).

链路: 问题 → FTS 搜索 → 上下文组装 → Gateway 调用 → 回答 + 引用

当 Gateway 不可用时（未配置 provider），降级返回搜索结果。
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from mneme.api.dependencies.api_key import get_api_consumer
from mneme.api.context import (  # noqa: E402
    ActorContext,
    RequestContext,
    get_request_context,
    with_actor,
)
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.auth import AuthenticatedSession
from mneme.db.base import get_db
from mneme.knowledge.fts import ensure_fts_index, search_fts
from mneme.knowledge.token_estimator import estimate_tokens
from mneme.schemas.ask import AskRequest, AskResponse, AskCitation
from mneme.schemas import ResponseEnvelope

router = APIRouter(prefix="/ask", tags=["ask"])
logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """你是一个知识库助手。根据提供的参考资料回答用户的问题。
要求：
1. 基于参考资料回答，不要编造信息
2. 如果参考资料不足以回答，明确说明
3. 回答简洁准确，使用中文
4. 在回答末尾标注引用来源编号 [1][2]..."""


@router.post("", response_model=ResponseEnvelope[AskResponse], status_code=200)
async def ask_question(
    payload: AskRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    consumer=Depends(get_api_consumer),
) -> dict:
    """回答用户问题，基于知识库搜索结果调用 AI 生成回答。

    流程:
    1. FTS 搜索知识库获取相关分块
    2. 组装上下文（截断到 token 预算）
    3. 调用 Gateway chat.completion 生成回答
    4. 返回回答 + 引用

    降级策略:
    - Gateway 未配置 → 返回搜索结果摘要，degraded=True
    - Gateway 调用失败 → 返回搜索结果摘要，degraded=True
    """
    # Wire actor
    if isinstance(consumer, AuthenticatedSession):
        actor = ActorContext(
            actor_type="user",
            actor_id=consumer.user.user_id,
            auth_context_type="user_session",
            auth_context_id=consumer.session.session_id,
        )
    else:
        actor = ActorContext(
            actor_type="agent",
            actor_id=consumer.agent.agent_id,
            auth_context_type="agent_token",
            auth_context_id=consumer.token.token_id,
        )
    context = with_actor(context, actor=actor)

    # 1. Search knowledge
    ensure_fts_index(db)
    results, total = search_fts(
        db,
        query=payload.question.strip(),
        project_id=payload.project_id,
        sensitivity_floor=payload.sensitivity_floor,
        page=1,
        page_size=payload.max_citations * 3,  # over-fetch for better context
    )

    if not results:
        resp = AskResponse(
            answer="在知识库中未找到与您问题相关的内容。",
            citations=[],
            context_token_count=0,
            model=None,
            degraded=True,
            degradation_reason="no_search_results",
        )
        return envelope(
            jsonable_encoder(resp.model_dump(mode="json")),
            request_id=context.request_id,
            correlation_id=context.correlation_id,
        )

    # 2. Build citations
    citations: list[AskCitation] = []
    for r in results[:payload.max_citations]:
        citations.append(AskCitation(
            chunk_id=str(r.chunk_id),
            document_title=r.document_title or "未命名文档",
            snippet=(r.chunk_text or "")[:800],
            rank=float(r.rank),
        ))

    # 3. Assemble context for LLM
    context_parts: list[str] = []
    for i, r in enumerate(results[:payload.max_citations]):
        context_parts.append(f"[{i+1}] {r.document_title or '文档'}\n{r.chunk_text}")
    context_text = "\n\n---\n\n".join(context_parts)
    context_tokens = estimate_tokens(context_text)

    # Truncate if too long (leave room for system prompt + question + output)
    max_context_chars = 30000
    if len(context_text) > max_context_chars:
        context_text = context_text[:max_context_chars] + "\n...(已截断)"

    # 4. Try Gateway call
    answer_text = ""
    model_name = None
    degraded = False
    degradation_reason = None

    try:
        from mneme.gateway import get_gateway, GatewayError

        gw = get_gateway()
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"参考资料:\n{context_text}\n\n问题: {payload.question}"},
        ]

        result = gw.call(
            capability_code="chat.completion",
            params={
                "messages": messages,
                "max_tokens": 2048,
                "temperature": 0.3,
            },
            project_id=payload.project_id,
            sensitivity=payload.sensitivity_floor or "private",
            actor_type=actor.actor_type,
            actor_id=actor.actor_id,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            idempotency_key=str(context.idempotency_key or uuid4()),
        )

        # Extract answer from OpenAI-compatible response
        resp_data = result.get("data", {})
        choices = resp_data.get("choices", [])
        if choices:
            answer_text = choices[0].get("message", {}).get("content", "")
        model_name = resp_data.get("model")

    except Exception as exc:
        # Gateway not configured or call failed — degrade gracefully
        logger.warning("Gateway call failed, degrading to search-only: %s", exc)
        degraded = True
        degradation_reason = type(exc).__name__

        # Build a simple summary from search results
        summary_parts = []
        for i, r in enumerate(results[:3]):
            snippet = (r.chunk_text or "")[:200]
            summary_parts.append(f"[{i+1}] {r.document_title or '文档'}: {snippet}")
        answer_text = "（AI 服务暂不可用，以下是搜索结果摘要）\n\n" + "\n\n".join(summary_parts)

    resp = AskResponse(
        answer=answer_text,
        citations=citations,
        context_token_count=context_tokens,
        model=model_name,
        degraded=degraded,
        degradation_reason=degradation_reason,
    )

    return envelope(
        jsonable_encoder(resp.model_dump(mode="json")),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
