"""Tests for the /api/v4/ask endpoint (A3: AI 回答 MVP).

This endpoint combines knowledge search + context assembly + Gateway call
to provide AI-powered answers with citations.
"""

from unittest.mock import patch, MagicMock
from uuid import uuid4

import pytest

from mneme.schemas.ask import AskRequest, AskResponse, AskCitation


# ── Schema tests ─────────────────────────────────────────────────────────


def test_ask_request_schema():
    """AskRequest 应该接受 question 和可选参数。"""
    req = AskRequest(question="什么是 Mneme?")
    assert req.question == "什么是 Mneme?"
    assert req.project_id is None
    assert req.max_citations == 5


def test_ask_request_with_options():
    """AskRequest 应该支持所有可选参数。"""
    pid = uuid4()
    req = AskRequest(
        question="How does chunking work?",
        project_id=pid,
        max_citations=10,
        sensitivity_floor="private",
    )
    assert req.question == "How does chunking work?"
    assert req.project_id == pid
    assert req.max_citations == 10
    assert req.sensitivity_floor == "private"


def test_ask_response_schema():
    """AskResponse 应该包含 answer、citations 和 metadata。"""
    resp = AskResponse(
        answer="Mneme 是一个个人智能资产控制平面。",
        citations=[
            AskCitation(
                chunk_id=str(uuid4()),
                document_title="架构基线",
                snippet="Mneme 是一个 Web-first...",
                rank=0.85,
            ),
        ],
        context_token_count=150,
        model="gpt-4o",
        degraded=False,
    )
    assert "Mneme" in resp.answer
    assert len(resp.citations) == 1
    assert resp.citations[0].document_title == "架构基线"
    assert resp.model == "gpt-4o"
    assert resp.degraded is False


def test_ask_response_degraded():
    """AskResponse 在 Gateway 不可用时应该 degraded=True。"""
    resp = AskResponse(
        answer="",
        citations=[],
        context_token_count=0,
        model=None,
        degraded=True,
        degradation_reason="gateway.binding_not_found",
    )
    assert resp.degraded is True
    assert resp.degradation_reason == "gateway.binding_not_found"


# ── Integration test (mocked Gateway) ────────────────────────────────────


def test_ask_endpoint_returns_answer(db):
    """POST /api/v4/ask 应该返回 AI 回答和引用。"""
    pytest.skip("Requires running PostgreSQL + Gateway config — run manually")


def test_ask_endpoint_no_gateway_returns_citations(db):
    """当 Gateway 不可用时，/api/v4/ask 应该返回搜索结果和 degraded=True。"""
    pytest.skip("Requires running PostgreSQL — run manually")
