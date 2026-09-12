"""Transactional edits to stored project files; uploaded source is never executed."""

from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from project_inventory import inventory_project_database
from project_parsing import parse_project_database
from project_structure import clear_project_structure
from project_uploads import ProjectBundle, UploadLimits, normalize_project_path, safe_project_name


def owned_project(db: sqlite3.Connection, project_id: str, user_id: int, *, editing: bool = False):
    project = db.execute("SELECT * FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id)).fetchone()
    if project is None:
        raise HTTPException(404, "Project not found")
    if editing and db.execute(
        "SELECT 1 FROM chat_jobs WHERE project_id = ? AND status IN ('queued', 'processing') LIMIT 1",
        (project_id,),
    ).fetchone():
        raise HTTPException(409, "Stop the project analysis before changing its files or entry point")
    return project


def store_upload_batch(db: sqlite3.Connection, project_id: str, bundle: ProjectBundle,
                       source_kind: str, upload_name: str) -> None:
    batch_id = db.execute(
        "INSERT INTO project_upload_batches(project_id, source_kind, name) VALUES (?, ?, ?)",
        (project_id, source_kind, safe_project_name(upload_name)),
    ).lastrowid
    db.executemany(
        "INSERT INTO project_files(project_id, path, content, size_bytes, sha256, is_binary, upload_batch_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(project_id, file.path, file.content, file.size_bytes, file.sha256, int(file.is_binary), batch_id) for file in bundle.files],
    )


def validate_append(db: sqlite3.Connection, project_id: str, bundle: ProjectBundle, limits: UploadLimits) -> None:
    existing = db.execute("SELECT path, size_bytes FROM project_files WHERE project_id = ?", (project_id,)).fetchall()
    paths = {str(row["path"]).casefold() for row in existing}
    for file in bundle.files:
        if file.path.casefold() in paths:
            raise HTTPException(409, f"A file already exists at {file.path}. Delete it first or choose a different path.")
    combined = paths | {file.path.casefold() for file in bundle.files}
    for child in combined:
        parts = child.split("/")
        for length in range(1, len(parts)):
            parent = "/".join(parts[:length])
            if parent in combined:
                raise HTTPException(409, f"The path {parent} would be both a file and a folder")
    if len(existing) + len(bundle.files) > limits.max_files:
        raise HTTPException(413, f"The combined project exceeds the {limits.max_files:,}-file limit")
    if sum(int(row["size_bytes"]) for row in existing) + bundle.total_bytes > limits.max_expanded_bytes:
        raise HTTPException(413, f"The combined project exceeds the {limits.max_expanded_bytes:,}-byte limit")


def rebuild_project(db: sqlite3.Connection, project_id: str) -> None:
    """Invalidate reports and re-resolve all dependencies after an atomic file edit."""
    clear_project_structure(db, project_id)
    db.execute("""
        UPDATE projects SET
            file_count = (SELECT COUNT(*) FROM project_files WHERE project_id = ?),
            total_bytes = (SELECT COALESCE(SUM(size_bytes), 0) FROM project_files WHERE project_id = ?),
            main_file_path = CASE WHEN EXISTS(SELECT 1 FROM project_files WHERE project_id = ? AND path = projects.main_file_path)
                                  THEN main_file_path ELSE NULL END,
            function_analysis_cache_hit_count = 0, function_analysis_model_request_count = 0,
            function_analysis_batch_request_count = 0, function_analysis_deterministic_count = 0,
            function_analysis_batch_fallback_count = 0, function_analysis_batch_error = NULL,
            call_compatibility_status = 'pending', call_compatibility_checked_count = 0,
            call_compatibility_incompatible_count = 0, call_compatibility_unknown_count = 0,
            call_compatibility_not_checked_count = 0, call_compatibility_error = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (project_id, project_id, project_id, project_id))
    if not db.execute("SELECT 1 FROM project_files WHERE project_id = ? LIMIT 1", (project_id,)).fetchone():
        db.execute("DELETE FROM project_languages WHERE project_id = ?", (project_id,))
        db.execute("""
            UPDATE projects SET primary_language = NULL, languages_json = '[]', inventory_status = 'completed', inventory_error = NULL,
                parser_status = 'unsupported', parser_supported_file_count = 0,
                parser_parsed_file_count = 0, parser_syntax_error_file_count = 0,
                parser_failed_file_count = 0, parser_error = NULL,
                structure_status = 'unsupported', indexed_file_count = 0, definition_count = 0,
                dependency_count = 0, call_count = 0, resolved_dependency_count = 0,
                ambiguous_dependency_count = 0, structure_error = NULL,
                function_analysis_status = 'completed', function_analysis_total_count = 0,
                function_analysis_completed_count = 0, function_analysis_failed_count = 0,
                function_analysis_skipped_count = 0, function_analysis_error = NULL
            WHERE id = ?
        """, (project_id,))
        return
    inventory_project_database(db, project_id)
    parse_project_database(db, project_id)


def entry_file_ids(db: sqlite3.Connection, project_id: str, kind: str,
                   file_id: int | None, batch_id: int | None, path: str | None) -> list[int]:
    if kind == "file" and file_id is not None:
        rows = db.execute("SELECT id FROM project_files WHERE project_id = ? AND id = ?", (project_id, file_id)).fetchall()
    elif kind in {"folder", "upload"} and batch_id is not None:
        rows = db.execute("SELECT id, path FROM project_files WHERE project_id = ? AND upload_batch_id = ?", (project_id, batch_id)).fetchall()
        if kind == "folder":
            if not path:
                raise HTTPException(422, "A folder path is required")
            prefix = normalize_project_path(path).casefold() + "/"
            rows = [row for row in rows if str(row["path"]).casefold().startswith(prefix)]
    else:
        raise HTTPException(422, "Select a file, folder or upload")
    if not rows:
        raise HTTPException(404, "Project entry not found")
    return [int(row["id"]) for row in rows]
