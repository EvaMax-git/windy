"""Auto-match files to pipeline definitions based on project rules.

Priority (highest first):
  1. Full-name match  (``Dockerfile``, ``Makefile``)
  2. Extension match  (``*.ts``, ``*.py``)
  3. Content sniff     (read first 512 bytes)
  4. Default fallback  (``*`` → standard_chunk)
"""
from __future__ import annotations

import os
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

# Extension → likely language hint (for code_parse pipeline selection)
EXT_TO_LANG: dict[str, str] = {
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript",
    ".py": "python", ".pyi": "python",
    ".go": "go", ".rs": "rust",
    ".java": "java", ".kt": "kotlin",
    ".cpp": "cpp", ".c": "c", ".h": "c",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".md": "markdown", ".mdx": "markdown",
    ".css": "css", ".html": "html",
    ".sh": "bash", ".bash": "bash",
    ".sql": "sql", ".txt": "text",
}


def get_file_extension(name: str) -> str:
    """Return lowercase extension including dot, e.g. '.ts'."""
    return os.path.splitext(name)[1].lower()


def sniff_language(content: bytes) -> str | None:
    """Try to determine language from file content (first 512 bytes)."""
    text_sample = content[:512].decode("utf-8", errors="ignore").strip()

    # Shebang detection
    if text_sample.startswith("#!/"):
        shebang = text_sample.split("\n")[0].lower()
        if "python" in shebang:
            return "python"
        if "node" in shebang or "deno" in shebang:
            return "javascript"
        if "bash" in shebang or "sh" in shebang:
            return "bash"

    # JSON detection
    if text_sample.startswith("{") or text_sample.startswith("["):
        try:
            __import__("json").loads(text_sample)
            return "json"
        except Exception:
            pass

    # Markdown detection (starts with heading)
    if text_sample.startswith("# "):
        return "markdown"

    # Looks like C/Java/TS (lots of semicolons and braces)
    if text_sample.count(";") > 3 and ("{" in text_sample or "class " in text_sample):
        return "typescript"

    return None


def match_pipeline(
    db: Session,
    project_id: UUID,
    filename: str,
    content_preview: bytes | None = None,
) -> tuple[UUID | None, str]:
    """Return (pipeline_def_id, matched_rule) or (None, reason).

    ``matched_rule`` is a human-readable string like 'ext:*.ts' or 'default:*'.
    """
    rules = db.execute(
        text("""
            SELECT pattern, pipeline_def_id, priority
            FROM project_pipeline_rules
            WHERE project_id = :pid
            ORDER BY priority, pattern
        """),
        {"pid": project_id},
    ).mappings().all()

    if not rules:
        return None, "no rules configured for project"

    ext = get_file_extension(filename)

    # Pass 1: full-name match
    for r in rules:
        if r["pattern"] == filename:
            return r["pipeline_def_id"], f"name:{filename}"

    # Pass 2: extension match
    if ext:
        ext_pattern = f"*{ext}"
        for r in rules:
            if r["pattern"] == ext_pattern:
                return r["pipeline_def_id"], f"ext:{ext}"

    # Pass 3: content sniff
    if content_preview:
        lang = sniff_language(content_preview)
        if lang:
            ext2 = f".{lang}" if not lang.startswith(".") else lang
            for r in rules:
                if r["pattern"] == f"*{ext2}" or r["pattern"] == lang:
                    return r["pipeline_def_id"], f"sniff:{lang}"

    # Pass 4: default fallback
    for r in rules:
        if r["pattern"] == "*":
            return r["pipeline_def_id"], "default:*"

    return None, "no matching rule"


def get_lang_for_file(filename: str) -> str:
    """Return lang hint for a filename, used when creating documents."""
    ext = get_file_extension(filename)
    return EXT_TO_LANG.get(ext, "markdown")


# ── Import exclusions (.mnemeignore) ─────────────────────────────────

DEFAULT_EXCLUSIONS = [
    "node_modules/**",
    ".git/**",
    ".svn/**",
    "__pycache__/**",
    "*.pyc",
    "*.pyo",
    ".DS_Store",
    "Thumbs.db",
    "*.lock",
    "*.log",
    "dist/**",
    "build/**",
    ".next/**",
    "target/**",       # Rust
    "vendor/**",       # Go / PHP
    ".venv/**",
    "venv/**",
    ".env",
    ".env.*",
]


def should_exclude(filepath: str, patterns: list[str]) -> bool:
    """Check if a file path matches any exclusion pattern.

    Supports ``**`` (recursive) and ``*`` (single-level) glob patterns.
    """
    import re

    filepath = filepath.replace("\\", "/")
    for pat in patterns:
        pat = pat.replace("\\", "/")
        # Convert glob pattern to regex
        regex = "^" + re.escape(pat).replace(r"\*\*", "____RECURSIVE____").replace(r"\*", "[^/]*").replace("____RECURSIVE____", ".*") + "$"
        if re.match(regex, filepath):
            return True
        # Also match against trailing path segments
        parts = filepath.split("/")
        for i in range(len(parts)):
            partial = "/".join(parts[i:])
            if re.match(regex, partial):
                return True
    return False


def get_import_exclusions(db: Session, project_id: UUID) -> list[str]:
    """Get exclusion patterns for a project. Stored as JSON in project settings
    or return defaults."""
    # For Phase 2, return defaults. Future: store per-project.
    return list(DEFAULT_EXCLUSIONS)
