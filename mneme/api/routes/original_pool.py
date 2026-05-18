"""Original pool management — stats and cold-storage cleanup."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.db.base import get_db

router = APIRouter(prefix="/admin/originals", tags=["original-pool"])


@router.get("/stats")
def original_pool_stats(db: Session = Depends(get_db)):
    """Return storage statistics for the global original pool."""
    hot = db.execute(
        text(
            "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) "
            "FROM assets "
            "WHERE original_pool_ref LIKE 'hot:%' AND status = 'active'"
        )
    ).first()
    cold = db.execute(
        text(
            "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) "
            "FROM assets "
            "WHERE original_pool_ref LIKE 'cold:%' AND status = 'archived'"
        )
    ).first()
    orphan = db.execute(
        text(
            "SELECT COUNT(*) FROM assets "
            "WHERE original_pool_ref IS NULL AND staging_expires_at < now()"
        )
    ).scalar()

    return {
        "hot_count": hot[0] if hot else 0,
        "hot_bytes": hot[1] if hot else 0,
        "cold_count": cold[0] if cold else 0,
        "cold_bytes": cold[1] if cold else 0,
        "orphan_count": orphan,
    }


@router.post("/cleanup")
def cleanup_originals(
    max_age_days: int = Query(90, ge=30),
    db: Session = Depends(get_db),
):
    """Move originals with no references older than ``max_age_days``
    from hot to cold storage.  This does NOT physically delete data."""
    result = db.execute(
        text(
            "UPDATE assets "
            "SET original_pool_ref = 'cold:' || substring(original_pool_ref, 5), "
            "    status = 'archived' "
            "WHERE original_pool_ref LIKE 'hot:%' "
            "  AND created_at < now() - make_interval(days => :days) "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM knowledge_documents kd "
            "    WHERE kd.source_asset_id = assets.asset_id"
            "  )"
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM assets a2 "
            "    WHERE a2.original_pool_ref = assets.original_pool_ref "
            "      AND a2.asset_id != assets.asset_id"
            "      AND a2.status = 'active'"
            "  )"
        ),
        {"days": max_age_days},
    )
    return {"moved_to_cold": result.rowcount}
