"""Import validators — schema and business-rule validation.

Validators are called during dry-run and before formal import.
They produce :class:`ValidationResult` with zero side effects.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from mneme.schemas.importer import (
    FieldMappingSchema,
    ImportPayload,
    ImportSourceItem,
    ValidationIssue,
    ValidationResult,
)

# Maximum title length (matches assets.title constraint)
_MAX_TITLE_LENGTH = 300
# Minimum content_hash length for validity
_MIN_HASH_LENGTH = 8
# Max content_hash length (matches assets.content_hash constraint)
_MAX_HASH_LENGTH = 128


def validate_import_payload(
    payload: ImportPayload,
    mapping: FieldMappingSchema | None = None,
) -> ValidationResult:
    """Validate an import payload against schema and business rules.

    This is the main entry point for dry-run validation.

    Args:
        payload: The import payload to validate.
        mapping: Optional field mapping schema for mapping-level checks.

    Returns:
        A :class:`ValidationResult` with all issues.
    """
    issues: list[ValidationIssue] = []

    for i, item in enumerate(payload.items):
        issues.extend(_validate_single_item(i, item, mapping))

    error_count = sum(1 for iss in issues if iss.severity == "error")
    warning_count = sum(1 for iss in issues if iss.severity == "warning")
    info_count = sum(1 for iss in issues if iss.severity == "info")

    return ValidationResult(
        passed=(error_count == 0),
        total_items=len(payload.items),
        valid_count=len(payload.items) - error_count,
        error_count=error_count,
        warning_count=warning_count,
        issues=issues,
    )


def _validate_single_item(
    index: int,
    item: ImportSourceItem,
    mapping: FieldMappingSchema | None = None,
) -> list[ValidationIssue]:
    """Validate a single source item.

    Returns:
        List of validation issues (empty = pass).
    """
    issues: list[ValidationIssue] = []

    # --- Required fields ---
    if not item.legacy_id or not item.legacy_id.strip():
        issues.append(
            ValidationIssue(
                index=index,
                legacy_id=item.legacy_id or "(missing)",
                field="legacy_id",
                severity="error",
                message="legacy_id is required",
            )
        )

    if not item.title or not item.title.strip():
        issues.append(
            ValidationIssue(
                index=index,
                legacy_id=item.legacy_id or "(missing)",
                field="title",
                severity="error",
                message="title is required",
            )
        )
    elif len(item.title) > _MAX_TITLE_LENGTH:
        issues.append(
            ValidationIssue(
                index=index,
                legacy_id=item.legacy_id,
                field="title",
                severity="error",
                message=f"title exceeds {_MAX_TITLE_LENGTH} characters (got {len(item.title)})",
            )
        )

    # --- content_hash format ---
    if item.content_hash:
        if len(item.content_hash) < _MIN_HASH_LENGTH:
            issues.append(
                ValidationIssue(
                    index=index,
                    legacy_id=item.legacy_id,
                    field="content_hash",
                    severity="warning",
                    message=f"content_hash seems too short ({len(item.content_hash)} chars, min {_MIN_HASH_LENGTH})",
                )
            )
        if len(item.content_hash) > _MAX_HASH_LENGTH:
            issues.append(
                ValidationIssue(
                    index=index,
                    legacy_id=item.legacy_id,
                    field="content_hash",
                    severity="error",
                    message=f"content_hash exceeds {_MAX_HASH_LENGTH} characters",
                )
            )
        # Basic hex check
        for ch in item.content_hash:
            if ch not in "0123456789abcdefABCDEF":
                issues.append(
                    ValidationIssue(
                        index=index,
                        legacy_id=item.legacy_id,
                        field="content_hash",
                        severity="error",
                        message=f"content_hash contains non-hex character: '{ch}'",
                    )
                )
                break
    else:
        issues.append(
            ValidationIssue(
                index=index,
                legacy_id=item.legacy_id,
                field="content_hash",
                severity="warning",
                message="content_hash is missing — will be computed during import",
            )
        )

    # --- content_uri format ---
    if item.content_uri:
        uri = item.content_uri
        if uri.startswith("/"):
            issues.append(
                ValidationIssue(
                    index=index,
                    legacy_id=item.legacy_id,
                    field="content_uri",
                    severity="info",
                    message="content_uri is a local path; ensure the file is accessible",
                )
            )
        if ".." in uri:
            issues.append(
                ValidationIssue(
                    index=index,
                    legacy_id=item.legacy_id,
                    field="content_uri",
                    severity="warning",
                    message="content_uri contains '..' which may indicate path traversal risk",
                )
            )

    # --- size_bytes ---
    if item.size_bytes is not None and item.size_bytes < 0:
        issues.append(
            ValidationIssue(
                index=index,
                legacy_id=item.legacy_id,
                field="size_bytes",
                severity="error",
                message=f"size_bytes must be ≥ 0, got {item.size_bytes}",
            )
        )

    # --- Mapping-level validation ---
    if mapping is not None:
        for m in mapping.mappings:
            if m.required:
                # The legacy_field must exist in the item
                field_value = getattr(item, m.legacy_field, None)
                if field_value is None or field_value == "" or field_value == []:
                    # title is checked above; other required fields warn
                    if m.legacy_field != "title" and m.legacy_field != "legacy_id":
                        issues.append(
                            ValidationIssue(
                                index=index,
                                legacy_id=item.legacy_id,
                                field=m.legacy_field,
                                severity="warning",
                                message=f"Required field '{m.legacy_field}' is empty",
                            )
                        )

    return issues
