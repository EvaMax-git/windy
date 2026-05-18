"""Knowledge document tree, content, and folder-path operations.

These complement mneme.db.knowledge (which handles document + block CRUD)
without modifying that stable file.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


# ── Tree building ────────────────────────────────────────────────────

def get_document_tree(db: Session, project_id: UUID) -> list[dict]:
    """Return a nested tree of folders and files for a project.

    Tree is built from knowledge_documents.folder_path.  Documents with
    ``folder_path IS NULL`` appear at the root.
    """
    rows = db.execute(
        text("""
            SELECT document_id, title, folder_path, lang,
                   document_status, current_version, updated_at
            FROM knowledge_documents
            WHERE project_id = :pid
              AND document_status = 'active'
            ORDER BY folder_path NULLS FIRST, title
        """),
        {"pid": project_id},
    ).mappings().all()

    # Build tree: partition by folder segments
    tree: list[dict] = []
    folder_map: dict[str, dict] = {}  # path -> folder node

    for row in rows:
        doc = dict(row)
        path = doc.get("folder_path") or ""
        segments = [s for s in path.split("/") if s] if path else []

        # Ensure all ancestor folders exist
        current_path = ""
        parent_list = tree
        for seg in segments:
            current_path = f"{current_path}/{seg}" if current_path else seg
            if current_path not in folder_map:
                node: dict = {
                    "name": seg,
                    "type": "folder",
                    "path": current_path,
                    "children": [],
                }
                parent_list.append(node)
                folder_map[current_path] = node
            parent_list = folder_map[current_path]["children"]

        # Add document leaf
        parent_list.append({
            "name": doc["title"],
            "type": "file",
            "path": path,
            "document_id": str(doc["document_id"]),
            "lang": doc.get("lang", "markdown"),
            "version": doc["current_version"],
            "updated_at": str(doc["updated_at"]) if doc.get("updated_at") else None,
        })

    return tree


# ── Document content ─────────────────────────────────────────────────

def get_document_content(db: Session, document_id: UUID) -> dict | None:
    """Return document with content fields for the editor."""
    row = db.execute(
        text("""
            SELECT kd.document_id, kd.title, kd.lang, kd.folder_path,
                   kd.document_status, kd.current_version,
                   kd.sensitivity_level, kd.project_id,
                   kd.pipeline_def_id, kd.source_asset_id,
                   kd.content_hash, kd.created_at, kd.updated_at
            FROM knowledge_documents kd
            WHERE kd.document_id = :did
        """),
        {"did": document_id},
    ).mappings().first()

    if not row:
        return None

    result = dict(row)
    # Concatenate block markdown to build full content
    blocks = db.execute(
        text("""
            SELECT block_id, block_order, block_type, content_markdown, token_count
            FROM knowledge_blocks
            WHERE document_id = :did
            ORDER BY block_order
        """),
        {"did": document_id},
    ).mappings().all()

    markdown = "\n\n".join(b["content_markdown"] for b in blocks)
    result["content_markdown"] = markdown
    result["blocks"] = [dict(b) for b in blocks]
    return result


def update_document_content(
    db: Session,
    document_id: UUID,
    content_markdown: str,
    affected_block_ids: list[UUID] | None = None,
) -> dict:
    """Replace document content: clear blocks, insert new, bump version.

    Returns { "document_id", "new_version", "index_status" }.
    """
    import hashlib

    # 1. Compute next version (knowledge_documents uses its own current_version)
    row = db.execute(
        text("SELECT current_version FROM knowledge_documents WHERE document_id = :did"),
        {"did": document_id},
    ).first()
    current_ver = row[0] if row else 1
    new_version = current_ver + 1

    # 2. Clear old blocks
    db.execute(
        text("DELETE FROM knowledge_blocks WHERE document_id = :did"),
        {"did": document_id},
    )

    # 3. Parse and insert new blocks
    blocks = _parse_markdown_to_blocks(content_markdown, document_id)
    for b in blocks:
        db.execute(
            text("""
                INSERT INTO knowledge_blocks
                    (document_id, block_key, block_order, block_type,
                     content_markdown, content_text, token_count, current_version)
                VALUES (:did, :bkey, :border, :btype,
                        :md, :txt, :tokens, :ver)
            """),
            {
                "did": document_id,
                "bkey": b["block_key"],
                "border": b["block_order"],
                "btype": b["block_type"],
                "md": b["content_markdown"],
                "txt": b["content_text"],
                "tokens": b.get("token_count", 0),
                "ver": new_version,
            },
        )

    # 4. Update content_hash
    content_hash = hashlib.sha256(content_markdown.encode()).hexdigest()
    db.execute(
        text("""
            UPDATE knowledge_documents
            SET content_hash = :hash, current_version = :ver, updated_at = now()
            WHERE document_id = :did
        """),
        {"did": document_id, "hash": content_hash, "ver": new_version},
    )

    # 5. Mark index states stale
    from mneme.db.document_index_states import mark_stale_for_blocks
    mark_stale_for_blocks(db, document_id, affected_block_ids, new_version)

    return {
        "document_id": str(document_id),
        "new_version": new_version,
        "content_hash": content_hash,
    }


def _parse_markdown_to_blocks(
    markdown: str, document_id: UUID
) -> list[dict]:
    """Simple paragraph-based block parser.  Full pipeline parsing
    (code_parse etc) happens at import time; this is for manual edits."""
    import re

    blocks = []
    doc_str = str(document_id)[:8]

    # Split on any-level headings (#, ##, ###, ...)
    sections = re.split(r"\n(?=#{1,6}\s)", markdown)

    order = 0
    for section in sections:
        section = section.strip()
        if not section:
            continue

        # Determine block type from first line
        first_line = section.split("\n")[0]
        if re.match(r"^#{1,6}\s", first_line):
            btype = "title"
        elif first_line.startswith("```"):
            btype = "code"
        elif first_line.startswith("|") and "|" in first_line[2:]:
            btype = "table"
        elif first_line.startswith("> "):
            btype = "quote"
        elif first_line.startswith("- ") or first_line.startswith("* "):
            btype = "list"
        else:
            btype = "paragraph"

        order += 1
        block_key = f"{doc_str}-b{order:04d}"

        # Strip markdown syntax for plain text (preserve common punctuation)
        content_text = re.sub(r"```[^`]*```", "", section)  # remove code blocks
        content_text = re.sub(r"`[^`]+`", "", content_text)  # remove inline code
        content_text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", content_text)  # links
        content_text = re.sub(r"[#>*\-]", " ", content_text)  # heading/list/quote markers
        content_text = re.sub(r"\s+", " ", content_text).strip()

        # Rough token count (CJK-aware)
        cjk_chars = len(re.findall(r"[一-鿿]", content_text))
        other = len(content_text) - cjk_chars
        token_count = int(cjk_chars * 0.5 + other * 0.25) or 1

        blocks.append({
            "block_key": block_key,
            "block_order": order,
            "block_type": btype,
            "content_markdown": section,
            "content_text": content_text,
            "token_count": token_count,
        })

    return blocks
