"""Re-segment all existing knowledge chunks with jieba.

Run this after adding jieba segmentation to fix Chinese FTS matching.
Usage: python -m scripts.resegment_chunks
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from mneme.db.base import SessionLocal
from mneme.knowledge.jieba_segment import segment, is_available


def resegment_all_chunks() -> int:
    """Re-segment all chunk_text in knowledge_chunks table."""
    if not is_available():
        print("ERROR: jieba is not installed. Run: pip install jieba")
        return 0

    db = SessionLocal()
    try:
        # Get all chunks with their current text
        rows = db.execute(
            text("SELECT chunk_id, chunk_text FROM knowledge_chunks")
        ).all()

        if not rows:
            print("No chunks found in database.")
            return 0

        print(f"Found {len(rows)} chunks to re-segment...")
        updated = 0

        for row in rows:
            chunk_id = row[0]
            raw_text = row[1]

            if not raw_text:
                continue

            # Apply jieba segmentation
            segmented = segment(raw_text)

            # Only update if text actually changed
            if segmented != raw_text:
                db.execute(
                    text("UPDATE knowledge_chunks SET chunk_text = :text WHERE chunk_id = :id"),
                    {"text": segmented, "id": chunk_id},
                )
                updated += 1

        db.commit()
        print(f"Done. Updated {updated}/{len(rows)} chunks.")
        return updated

    except Exception as e:
        db.rollback()
        print(f"ERROR: {e}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    resegment_all_chunks()
