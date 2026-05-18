"""P3-09 Importer skeleton — Mneme2 → v4.1 migration framework.

The importer provides three operation modes:

* **dry-run** — zero-side-effect validation of source items
* **preview** — field mapping preview without creating assets
* **import** — formal import (creates inbox items + assets via pipeline)

All runs are tracked via ``pipeline_runs`` (trigger_type='importer').

Public API
----------
* ``ImportEngine`` — main orchestrator for all three modes
* ``build_import_report`` — generate a report from import results
* ``report_to_json`` / ``report_to_markdown`` — format reports
* ``get_mapping`` — resolve field mapping for a source type
* ``apply_transform`` — apply a field transform
"""

from mneme.importer.engine import ImportEngine
from mneme.importer.mappers import (
    get_mapping,
    apply_transform,
)
from mneme.importer.reporter import (
    build_preview_result,
    build_import_report,
    report_to_json,
    report_to_markdown,
)

__all__ = [
    "ImportEngine",
    "get_mapping",
    "apply_transform",
    "build_preview_result",
    "build_import_report",
    "report_to_json",
    "report_to_markdown",
]
