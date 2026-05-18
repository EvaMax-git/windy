"""Phase 1 Knowledge API v2 — project config, document content, tree, health.

These complement the existing knowledge routes without modifying them.
"""
from __future__ import annotations

from uuid import UUID

# Note: Full audit_events + events (outbox) + idempotency-key integration
# for write endpoints will be added in Phase 6 (hardening).  Phase 1
# focuses on correct data model and API shape.  See §5.6 of the redesign plan.

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.db.base import get_db
from mneme.db import project_backends as pb_dal
from mneme.db import document_index_states as dis_dal
from mneme.db import knowledge_tree as kt_dal
from mneme.schemas.project_config import (
    BackendHealthSummary,
    BackendToggleRequest,
    ContentUpdateRequest,
    DocumentContentResponse,
    DocumentCreateRequest,
    DocumentMoveRequest,
    IndexStatesResponse,
    IndexStateItem,
    PipelineRulesUpdateRequest,
    ProjectBackendRead,
    ProjectHealthResponse,
    TreeResponse,
)

router = APIRouter(tags=["knowledge-v2"])


# ── Project Backends ─────────────────────────────────────────────────

@router.get("/projects/{project_id}/backends", response_model=list[ProjectBackendRead])
def list_project_backends(project_id: UUID, db: Session = Depends(get_db)):
    return pb_dal.get_project_backends(db, project_id)


@router.put("/projects/{project_id}/backends/{backend_type}")
def toggle_backend(
    project_id: UUID, backend_type: str, body: BackendToggleRequest,
    db: Session = Depends(get_db),
):
    if backend_type not in ("fulltext", "vector", "graph"):
        raise HTTPException(400, f"Unknown backend type: {backend_type}")
    pb_dal.set_backend_enabled(db, project_id, backend_type, body.enabled)
    return {"ok": True}


# ── Pipeline Rules ───────────────────────────────────────────────────

@router.get("/projects/{project_id}/pipeline-rules")
def get_pipeline_rules(project_id: UUID, db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "SELECT id, project_id, pattern, pipeline_def_id, priority, created_at "
            "FROM project_pipeline_rules WHERE project_id = :pid ORDER BY priority"
        ),
        {"pid": project_id},
    ).mappings().all()
    return [dict(r) for r in rows]


@router.put("/projects/{project_id}/pipeline-rules")
def update_pipeline_rules(
    project_id: UUID, body: PipelineRulesUpdateRequest,
    db: Session = Depends(get_db),
):
    # Replace all rules (session implicit transaction ensures atomicity)
    db.execute(
        text("DELETE FROM project_pipeline_rules WHERE project_id = :pid"),
        {"pid": project_id},
    )
    for r in body.rules:
        db.execute(
            text(
                "INSERT INTO project_pipeline_rules "
                "(project_id, pattern, pipeline_def_id, priority) "
                "VALUES (:pid, :pat, :def_id, :prio)"
            ),
            {"pid": project_id, "pat": r.pattern,
             "def_id": r.pipeline_def_id, "prio": r.priority},
        )
    return {"ok": True, "rules": len(body.rules)}


# ── Document Tree ────────────────────────────────────────────────────

@router.get("/projects/{project_id}/tree", response_model=TreeResponse)
def document_tree(project_id: UUID, db: Session = Depends(get_db)):
    tree = kt_dal.get_document_tree(db, project_id)
    return TreeResponse(project_id=project_id, tree=tree)


# ── Document Content ─────────────────────────────────────────────────

@router.get(
    "/knowledge/documents/{document_id}/v2/content",
    response_model=DocumentContentResponse,
)
def get_document_v2(document_id: UUID, db: Session = Depends(get_db)):
    doc = kt_dal.get_document_content(db, document_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    return doc


@router.patch("/knowledge/documents/{document_id}/v2/content")
def update_document_v2(
    document_id: UUID, body: ContentUpdateRequest,
    db: Session = Depends(get_db),
):
    result = kt_dal.update_document_content(
        db, document_id,
        body.content_markdown,
        body.affected_block_ids,
    )
    db.commit()
    return {"ok": True, **result}


# ── Document Create / Move ───────────────────────────────────────────

@router.post("/knowledge/documents/v2")
def create_document_v2(body: DocumentCreateRequest, db: Session = Depends(get_db)):
    """Create a new empty knowledge document in a project."""
    import hashlib
    from uuid import uuid4

    doc_id = uuid4()
    content_hash = None
    if body.content_markdown:
        content_hash = hashlib.sha256(
            body.content_markdown.encode()
        ).hexdigest()

    db.execute(
        text(
            "INSERT INTO knowledge_documents "
            "(document_id, project_id, title, lang, folder_path, content_hash) "
            "VALUES (CAST(:did AS uuid), CAST(:pid AS uuid), "
            " :title, :lang, :fpath, :hash)"
        ),
        {"did": str(doc_id), "pid": str(body.project_id), "title": body.title,
         "lang": body.lang, "fpath": body.folder_path, "hash": content_hash},
    )

    # Create initial blocks if content_markdown was provided
    if body.content_markdown:
        blocks = kt_dal._parse_markdown_to_blocks(body.content_markdown, doc_id)
        for b in blocks:
            db.execute(
                text("""
                    INSERT INTO knowledge_blocks
                        (document_id, block_key, block_order, block_type,
                         content_markdown, content_text, token_count, current_version)
                    VALUES (:did, :bkey, :border, :btype,
                            :md, :txt, :tokens, 1)
                """),
                {
                    "did": doc_id,
                    "bkey": b["block_key"],
                    "border": b["block_order"],
                    "btype": b["block_type"],
                    "md": b["content_markdown"],
                    "txt": b["content_text"],
                    "tokens": b.get("token_count", 0),
                },
            )

    dis_dal.init_index_states(db, doc_id, body.project_id)
    db.commit()
    return {"document_id": str(doc_id)}


@router.post("/knowledge/documents/{document_id}/move")
def move_document(
    document_id: UUID, body: DocumentMoveRequest,
    db: Session = Depends(get_db),
):
    doc = db.execute(
        text("SELECT project_id FROM knowledge_documents WHERE document_id = :did"),
        {"did": document_id},
    ).first()
    if not doc:
        raise HTTPException(404, "Document not found")

    current_pid = doc[0]
    if current_pid == body.target_project_id:
        return {"ok": True, "same_project": True}

    # Clear tags (project-scoped)
    db.execute(
        text("DELETE FROM document_tag_links WHERE document_id = :did"),
        {"did": document_id},
    )
    # Update project + optional folder
    db.execute(
        text(
            "UPDATE knowledge_documents SET project_id = :pid, "
            "folder_path = COALESCE(:fpath, folder_path), updated_at = now() "
            "WHERE document_id = :did"
        ),
        {"did": document_id, "pid": body.target_project_id,
         "fpath": body.target_folder},
    )
    # Re-init index states for new project's enabled backends
    dis_dal.init_index_states(db, document_id, body.target_project_id)
    return {"ok": True}


# ── Index States ─────────────────────────────────────────────────────

@router.get(
    "/knowledge/documents/{document_id}/index-states",
    response_model=IndexStatesResponse,
)
def document_index_states(document_id: UUID, db: Session = Depends(get_db)):
    states = dis_dal.get_index_states(db, document_id)
    return IndexStatesResponse(
        document_id=document_id,
        backends=[IndexStateItem(**s) for s in states],
    )


# ── Project Health ───────────────────────────────────────────────────

@router.get(
    "/projects/{project_id}/health",
    response_model=ProjectHealthResponse,
)
def project_health(project_id: UUID, db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "SELECT pb.backend_type, pb.enabled, "
            "COALESCE(SUM(CASE WHEN dis.state='ready' THEN 1 ELSE 0 END), 0) AS ready, "
            "COALESCE(SUM(CASE WHEN dis.state='stale' THEN 1 ELSE 0 END), 0) AS stale, "
            "COALESCE(SUM(CASE WHEN dis.state='failed' THEN 1 ELSE 0 END), 0) AS failed, "
            "COALESCE(SUM(CASE WHEN dis.state='disabled' THEN 1 ELSE 0 END), 0) AS disabled "
            "FROM project_backends pb "
            "LEFT JOIN knowledge_documents kd ON kd.project_id = pb.project_id "
            "LEFT JOIN document_index_states dis "
            "  ON dis.document_id = kd.document_id AND dis.backend_type = pb.backend_type "
            "WHERE pb.project_id = :pid "
            "GROUP BY pb.backend_type, pb.enabled "
            "ORDER BY pb.backend_type"
        ),
        {"pid": project_id},
    ).mappings().all()

    backends = [
        BackendHealthSummary(
            backend_type=r["backend_type"],
            enabled=r["enabled"],
            docs_ready=r["ready"],
            docs_stale=r["stale"],
            docs_failed=r["failed"],
            docs_disabled=r["disabled"],
        )
        for r in rows
    ]

    # Overall status
    has_failures = any(b.docs_failed > 0 for b in backends)
    has_stale = any(b.docs_stale > 0 for b in backends)
    overall = "attention" if has_failures else ("degraded" if has_stale else "healthy")

    return ProjectHealthResponse(
        project_id=project_id, backends=backends, overall=overall,
    )


# ── Rebuild Stale ────────────────────────────────────────────────────

@router.post("/projects/{project_id}/indexes/rebuild-stale")
def rebuild_stale_indexes(
    project_id: UUID,
    backends: list[str] | None = None,
    db: Session = Depends(get_db),
):
    count = dis_dal.rebuild_stale_for_project(db, project_id, backends)
    return {"ok": True, "rebuilding": count}
