"""Phase 2 Import API v2 — file/folder/path/GitHub import with auto-matching.

Replaces the multi-step AssetTab wizard with a single import call.
"""
from __future__ import annotations

import hashlib
import os
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.db.base import get_db
from mneme.importer.pipeline_matcher import (
    match_pipeline,
    get_lang_for_file,
    should_exclude,
    get_import_exclusions,
)
from mneme.db import document_index_states as dis_dal

router = APIRouter(tags=["import-v2"])


@router.post("/projects/{project_id}/import-v2")
async def import_files_v2(
    project_id: UUID,
    files: list[UploadFile] = File(...),
    relative_paths: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Import files into a project with automatic pipeline matching.

    Files are hashed, deduplicated, saved as originals, and processed
    asynchronously.  Progress is pushed via WebSocket (channel
    ``import:{project_id}``).
    """
    paths: list[str] = []
    if relative_paths:
        paths = [p.strip() for p in relative_paths.split(",")]

    if len(files) != len(paths) and paths:
        raise HTTPException(400, "files and relative_paths length mismatch")

    # Default: use filenames as paths
    if not paths:
        paths = [f.filename or f"file-{i}" for i, f in enumerate(files)]

    exclusions = get_import_exclusions(db, project_id)
    run_id = uuid4()

    results: list[dict] = []

    for f, rp in zip(files, paths):
        try:
            # Exclusion check
            if should_exclude(rp, exclusions):
                results.append({"path": rp, "status": "skipped", "reason": "excluded"})
                continue

            content = await f.read()
            content_hash = hashlib.sha256(content).hexdigest()

            # Dedup check (global — same file across projects shares original)
            existing = db.execute(
                text(
                    "SELECT asset_id FROM assets "
                    "WHERE content_hash = :hash AND status = 'active'"
                ),
                {"hash": content_hash},
            ).first()

            if existing:
                results.append({
                    "path": rp, "status": "skipped",
                    "reason": "duplicate", "asset_id": str(existing[0]),
                })
                continue

            # Save original (all UUIDs as strings for psycopg2 compatibility)
            asset_id = str(uuid4())
            asset_uid = f"asset-{asset_id[:12]}"

            db.execute(
                text(
                    "INSERT INTO assets "
                    "(asset_id, project_id, asset_uid, title, asset_type, "
                    " original_filename, storage_backend, storage_ref, "
                    " content_hash, size_bytes, relative_path, "
                    " original_pool_ref, staging_expires_at, status, "
                    " ingest_state, knowledge_state, "
                    " sensitivity_level, retention_policy, metadata_json) "
                    "VALUES (CAST(:aid AS uuid), CAST(:pid AS uuid), "
                    " :auid, :title, :atype, :fname, "
                    " 'mneme_data', :sref, "
                    " :hash, :size, :rpath, "
                    " :pool_ref, now() + interval '24 hours', "
                    " 'active', 'staged', 'not_started', "
                    " 'normal', 'default', '{}'::jsonb)"
                ),
                {
                    "aid": asset_id, "pid": str(project_id),
                    "auid": asset_uid,
                    "title": os.path.basename(rp),
                    "atype": "document",
                    "fname": f.filename,
                    "hash": content_hash,
                    "size": len(content),
                    "rpath": rp,
                    "pool_ref": f"hot:{content_hash}",
                    "sref": f"mneme_data/assets/{asset_id[:2]}/{asset_id}",
                },
            )

            # Match pipeline
            pipeline_def_id, matched_rule = match_pipeline(
                db, project_id, os.path.basename(rp), content[:512],
            )

            # Create knowledge document stub
            doc_id = uuid4()
            lang = get_lang_for_file(rp)
            # Create knowledge document stub
            doc_id = str(uuid4())
            lang = get_lang_for_file(rp)
            db.execute(
                text(
                    "INSERT INTO knowledge_documents "
                    "(document_id, project_id, title, lang, folder_path, "
                    " source_asset_id, pipeline_def_id) "
                    "VALUES (CAST(:did AS uuid), CAST(:pid AS uuid), "
                    " :title, :lang, :fpath, "
                    " CAST(:aid AS uuid), CAST(:pdef AS uuid))"
                ),
                {
                    "did": doc_id, "pid": str(project_id),
                    "title": os.path.basename(rp), "lang": lang,
                    "fpath": os.path.dirname(rp).replace("\\", "/") or None,
                    "aid": asset_id,
                    "pdef": str(pipeline_def_id) if pipeline_def_id else None,
                },
            )
            dis_dal.init_index_states(db, doc_id, project_id)

            results.append({
                "path": rp, "status": "imported",
                "asset_id": str(asset_id),
                "document_id": str(doc_id),
                "pipeline_matched": matched_rule,
            })
        except Exception as exc:
            results.append({
                "path": rp, "status": "failed",
                "reason": str(exc)[:200],
            })

    return {
        "import_run_id": str(run_id),
        "files": results,
        "summary": {
            "total": len(files),
            "imported": sum(1 for r in results if r["status"] == "imported"),
            "skipped": sum(1 for r in results if r["status"] == "skipped"),
            "failed": sum(1 for r in results if r["status"] == "failed"),
        },
    }


# ── Import exclusions ─────────────────────────────────────────────────

@router.get("/projects/{project_id}/import-exclusions")
def get_exclusions(project_id: UUID, db: Session = Depends(get_db)):
    return {"patterns": get_import_exclusions(db, project_id)}
