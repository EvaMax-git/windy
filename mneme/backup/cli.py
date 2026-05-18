"""P2-14 / P2-15 Backup & Restore CLI — command-line interface.

Usage::

    python -m mneme.backup run                # Run a full backup
    python -m mneme.backup list               # List existing backups
    python -m mneme.backup verify BACKUP_ID   # Verify a backup's integrity
    python -m mneme.backup info BACKUP_ID     # Show detailed manifest
    python -m mneme.backup drill BACKUP_ID    # Execute a full restore drill
    python -m mneme.backup restores           # List restore reports

Environment variables (same as main app):
    DATABASE_URL      – PostgreSQL connection string
    MNEME_BACKUP_ROOT – Backup output root directory (default: MnemeData/backups)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from mneme.backup.engine import (
    _default_backup_root,
    list_backups,
    run_backup,
    verify_backup,
)
from mneme.backup.manifest import load_manifest
from mneme.backup.restore_engine import (
    list_restores,
    run_restore_drill,
    load_restore_report,
)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def cmd_run(args: argparse.Namespace) -> int:
    """Execute a backup."""
    database_url: str | None = args.database_url or None
    output_root: Path | None = Path(args.output_root) if args.output_root else None
    backup_id: str | None = args.backup_id or None

    result = run_backup(
        database_url=database_url,
        output_root=output_root,
        backup_id=backup_id,
    )

    if result.success and result.manifest:
        print(f"✓ Backup succeeded: {result.manifest.backup_id}")
        print(f"  Output:    {result.output_dir}")
        print(f"  Size:      {result.manifest.file_size_bytes:,} bytes")
        print(f"  Checksum:  {result.manifest.checksum_sha256[:32]}...")
        print(f"  Tables:    {result.manifest.tables}")
        print(f"  PG:        {result.manifest.pg_version}")
        print(f"  Alembic:   {result.manifest.alembic_revision}")
        return 0
    else:
        print(f"✗ Backup failed: {result.error_message}", file=sys.stderr)
        return 1


def cmd_list(args: argparse.Namespace) -> int:
    """List available backups."""
    output_root: Path | None = Path(args.output_root) if args.output_root else None
    backups = list_backups(output_root)

    if not backups:
        print("No backups found.")
        return 0

    print(f"Found {len(backups)} backup(s):\n")
    for b in backups:
        size_str = f"{b['file_size_bytes']:,} bytes" if b["file_size_bytes"] else "N/A"
        summary = b.get("table_count_summary", {})
        total_rows = summary.get("total_rows", "?")
        print(f"  {b['backup_id']}")
        print(f"    Created:    {b['created_at']}")
        print(f"    Status:     {b['status']}")
        print(f"    Size:       {size_str}")
        print(f"    PG:         {b['pg_version']}")
        print(f"    Alembic:    {b['alembic_revision']}")
        print(f"    Total rows: {total_rows}")
        print(f"    Directory:  {b['backup_directory']}")
        print()
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify a specific backup by backup_id."""
    backup_id = args.backup_id
    output_root: Path | None = Path(args.output_root) if args.output_root else None

    if output_root is None:
        output_root = _default_backup_root()

    manifest = None
    backup_dir = None
    for entry in sorted(output_root.iterdir(), reverse=True):
        if entry.is_dir():
            m = load_manifest(entry)
            if m is not None and m.backup_id == backup_id:
                manifest = m
                backup_dir = entry
                break

    if manifest is None:
        print(f"Backup '{backup_id}' not found.", file=sys.stderr)
        return 1

    print(f"Verifying backup: {backup_id}")
    print(f"  Directory: {backup_dir}")

    result = verify_backup(manifest)
    if result["valid"]:
        print("✓ Integrity check passed")
        print(f"  File size:  {manifest.file_size_bytes:,} bytes (OK)")
        print(f"  Checksum:   {manifest.checksum_sha256[:32]}... (OK)")
        print(f"  Tables:     {manifest.tables}")
        print(f"  PG version: {manifest.pg_version}")
        return 0
    else:
        print("✗ Integrity check FAILED:")
        for issue in result["issues"]:
            print(f"  - {issue}")
        return 1


def cmd_info(args: argparse.Namespace) -> int:
    """Show detailed manifest for a backup."""
    backup_id = args.backup_id
    output_root: Path | None = Path(args.output_root) if args.output_root else None

    if output_root is None:
        output_root = _default_backup_root()

    manifest = None
    for entry in sorted(output_root.iterdir(), reverse=True):
        if entry.is_dir():
            m = load_manifest(entry)
            if m is not None and m.backup_id == backup_id:
                manifest = m
                break

    if manifest is None:
        print(f"Backup '{backup_id}' not found.", file=sys.stderr)
        return 1

    print(json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False))
    return 0


# ── P2-15 Restore CLI commands ─────────────────────────────────────────────────


def cmd_drill(args: argparse.Namespace) -> int:
    """Execute a restore drill: restore → verify → report → clean up."""
    backup_id = args.backup_id
    database_url: str | None = args.database_url or None
    target_database_url: str | None = args.target_database_url or None
    output_root: Path | None = Path(args.output_root) if args.output_root else None
    keep_temp_db: bool = args.keep_temp_db

    print(f"Starting restore drill for backup: {backup_id}")
    if target_database_url:
        print(f"  Target database URL: {target_database_url}")
    else:
        print(f"  Using auto-generated temporary database")
    print(f"  Keep temp DB after drill: {keep_temp_db}")
    print()

    result = run_restore_drill(
        backup_id=backup_id,
        source_database_url=database_url,
        target_database_url=target_database_url,
        output_root=output_root,
        keep_temp_db=keep_temp_db,
    )

    if not result.success:
        print(f"\n✗ Restore drill FAILED")
        if result.error_message:
            print(f"  Error: {result.error_message}")
        if result.report:
            print(f"  Report: {result.output_dir / 'restore_report.json'}")
        return 1

    report = result.report
    if report is None:
        print("✗ Restore drill produced no report", file=sys.stderr)
        return 1

    v = report.verification

    print(f"\n✓ Restore drill completed: {report.restore_id}")
    print(f"  Status:            {report.status}")
    print(f"  Target database:   {report.target_database}")
    print(f"  Started:           {report.started_at}")
    print(f"  Completed:         {report.completed_at}")
    print(f"  Report:            {result.output_dir / 'restore_report.json'}")
    print()

    # Print verification summary
    print("─ Verification Results ─" + "─" * 40)
    tc = v.get("table_count", {})
    print(f"  Table count:       {tc.get('actual', '?')}/{tc.get('expected', 45)} "
          f"({'PASS' if tc.get('match') else 'FAIL'})")

    rc = v.get("row_counts", {})
    mismatches = rc.get("mismatches", [])
    print(f"  Row counts:        {'PASS' if rc.get('match') else 'FAIL'}"
          f"{' (' + str(len(mismatches)) + ' mismatches)' if mismatches else ''}")

    fk = v.get("foreign_keys", {})
    fk_violations = fk.get("violations", [])
    print(f"  Foreign keys:      {'PASS' if fk.get('valid') else 'FAIL'}"
          f"{' (' + str(len(fk_violations)) + ' violations)' if fk_violations else ''}")

    ar = v.get("alembic_revision", {})
    print(f"  Alembic revision:  {'PASS' if ar.get('match') else 'FAIL'}"
          f" (expected: {ar.get('expected', '?')}, actual: {ar.get('actual', '?')})")

    # Print detail on failures
    if mismatches:
        print(f"\n  Row count mismatches:")
        for mm in mismatches[:10]:
            print(f"    - {mm['table']}: expected {mm['expected']}, got {mm['actual']}")
        if len(mismatches) > 10:
            print(f"    ... and {len(mismatches) - 10} more")

    if fk_violations:
        print(f"\n  FK violations:")
        for fv in fk_violations[:10]:
            if "orphan_count" in fv:
                print(f"    - {fv['child_table']}.{fv['child_column']} -> "
                      f"{fv['parent_table']}.{fv['parent_column']}: "
                      f"{fv['orphan_count']} orphans")
            elif "error" in fv:
                print(f"    - {fv.get('child_table', '?')}: {fv['error']}")
        if len(fk_violations) > 10:
            print(f"    ... and {len(fk_violations) - 10} more")

    print("─" * 60)
    return 0


def cmd_restores(args: argparse.Namespace) -> int:
    """List restore reports."""
    output_root: Path | None = Path(args.output_root) if args.output_root else None
    restores = list_restores(output_root)

    if not restores:
        print("No restore reports found.")
        return 0

    print(f"Found {len(restores)} restore report(s):\n")
    for r in restores:
        print(f"  {r['restore_id']}")
        print(f"    Backup:      {r['backup_id']}")
        print(f"    Type:        {r['restore_type']}")
        print(f"    Status:      {r['status']}")
        print(f"    Target DB:   {r['target_database']}")
        print(f"    Started:     {r['started_at']}")
        print(f"    Completed:   {r['completed_at']}")
        print(f"    Directory:   {r['report_directory']}")
        print()
    return 0


def cmd_restore_info(args: argparse.Namespace) -> int:
    """Show detailed restore report JSON."""
    restore_id = args.restore_id
    output_root: Path | None = Path(args.output_root) if args.output_root else None

    if output_root is None:
        output_root = _default_backup_root()

    report = None
    for entry in sorted(output_root.iterdir(), reverse=True):
        if entry.is_dir():
            r = load_restore_report(entry)
            if r is not None and r.restore_id == restore_id:
                report = r
                break

    if report is None:
        print(f"Restore report '{restore_id}' not found.", file=sys.stderr)
        return 1

    print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Main CLI entry point."""
    _setup_logging()

    parser = argparse.ArgumentParser(
        description="Mneme Backup & Restore Tool — pg_dump + restore + drill",
        prog="mneme-backup",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # run
    run_parser = subparsers.add_parser("run", help="Execute a full backup")
    run_parser.add_argument("--database-url", help="PostgreSQL connection URL")
    run_parser.add_argument("--output-root", help="Backup output root directory")
    run_parser.add_argument("--backup-id", help="Custom backup UUID (auto-generated if omitted)")

    # list
    list_parser = subparsers.add_parser("list", help="List existing backups")
    list_parser.add_argument("--output-root", help="Backup root directory")

    # verify
    verify_parser = subparsers.add_parser("verify", help="Verify a backup's integrity")
    verify_parser.add_argument("backup_id", help="Backup UUID to verify")
    verify_parser.add_argument("--output-root", help="Backup root directory")

    # info
    info_parser = subparsers.add_parser("info", help="Show detailed manifest JSON")
    info_parser.add_argument("backup_id", help="Backup UUID")
    info_parser.add_argument("--output-root", help="Backup root directory")

    # ── P2-15 Restore commands ──────────────────────────────────────────────

    # drill
    drill_parser = subparsers.add_parser(
        "drill",
        help="Execute a full restore drill (backup -> temp DB -> verify -> report -> clean up)",
    )
    drill_parser.add_argument("backup_id", help="Backup UUID to restore from")
    drill_parser.add_argument("--database-url", help="Source PostgreSQL connection URL (for temp DB creation)")
    drill_parser.add_argument("--target-database-url", help="Explicit target DB URL (skip temp DB creation)")
    drill_parser.add_argument("--output-root", help="Backup/restore root directory")
    drill_parser.add_argument("--keep-temp-db", action="store_true", default=False,
                              help="Keep the temporary database after the drill (for inspection)")

    # restores (list restore reports)
    restores_parser = subparsers.add_parser("restores", help="List restore reports")
    restores_parser.add_argument("--output-root", help="Backup root directory")

    # restore-info
    restore_info_parser = subparsers.add_parser(
        "restore-info", help="Show detailed restore report JSON"
    )
    restore_info_parser.add_argument("restore_id", help="Restore report UUID")
    restore_info_parser.add_argument("--output-root", help="Backup root directory")

    args = parser.parse_args(argv)

    if args.command == "run":
        return cmd_run(args)
    elif args.command == "list":
        return cmd_list(args)
    elif args.command == "verify":
        return cmd_verify(args)
    elif args.command == "info":
        return cmd_info(args)
    elif args.command == "drill":
        return cmd_drill(args)
    elif args.command == "restores":
        return cmd_restores(args)
    elif args.command == "restore-info":
        return cmd_restore_info(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
