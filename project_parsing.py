"""Parse inventoried project files through registered Tree-sitter adapters."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from language_adapters import get_adapter
from project_inventory import decode_text_content
from project_structure import (
    StructureCounts,
    clear_file_structure,
    clear_project_structure,
    finalize_project_structure,
    mark_file_structure_status,
    replace_file_structure,
)


@dataclass(frozen=True)
class ProjectParseSummary:
    status: str
    eligible_file_count: int
    supported_file_count: int
    parsed_file_count: int
    syntax_error_file_count: int
    failed_file_count: int
    structure_status: str
    definition_count: int
    dependency_count: int
    call_count: int


def _update_unparsed_file(
    db: sqlite3.Connection,
    file_id: int,
    status: str,
    *,
    adapter: str | None = None,
    error: str | None = None,
) -> None:
    db.execute(
        """
        UPDATE project_files
        SET parser_adapter = ?, parser_status = ?, grammar_name = NULL,
            grammar_version = NULL, parser_source_sha256 = NULL,
            parser_error_count = 0, parser_missing_count = 0,
            parser_diagnostics_json = '[]', parser_diagnostics_truncated = 0,
            parser_error = ?, parser_updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (adapter, status, error, file_id),
    )


def parse_project_database(
    db: sqlite3.Connection,
    project_id: str,
) -> ProjectParseSummary:
    """Parse eligible files, persisting bounded diagnostics but no source snippets."""
    rows = db.execute(
        """
        SELECT id, path, content, language, analysis_eligible
        FROM project_files
        WHERE project_id = ?
        ORDER BY path COLLATE NOCASE, path
        """,
        (project_id,),
    ).fetchall()
    if not rows:
        raise ValueError("The project does not contain stored files")

    clear_project_structure(db, project_id)

    eligible_count = 0
    supported_count = 0
    parsed_count = 0
    syntax_error_count = 0
    failed_count = 0
    failure_messages: list[str] = []
    structure_indexed_count = 0
    structure_failed_count = 0
    definition_count = 0
    dependency_count = 0
    call_count = 0
    structure_failure_messages: list[str] = []

    for row in rows:
        file_id = int(row["id"])
        if not bool(row["analysis_eligible"]):
            _update_unparsed_file(db, file_id, "not_applicable")
            mark_file_structure_status(db, file_id, "not_applicable")
            continue
        eligible_count += 1
        language = str(row["language"] or "")
        adapter = get_adapter(language)
        if adapter is None:
            _update_unparsed_file(db, file_id, "unsupported")
            mark_file_structure_status(db, file_id, "unsupported")
            continue
        supported_count += 1
        try:
            text, _encoding = decode_text_content(bytes(row["content"]))
            if text is None:
                raise ValueError("Source could not be decoded as text")
            adapter_status = adapter.status()
            if not adapter_status.available:
                raise RuntimeError(
                    f"The {adapter.display_name} Tree-sitter grammar is unavailable"
                )
            normalized_source = text.encode("utf-8")
            parsed = adapter.parse_text(text)
            status = "syntax_error" if parsed.has_syntax_errors else "parsed"
            diagnostics_json = json.dumps(
                [diagnostic.as_dict() for diagnostic in parsed.diagnostics],
                separators=(",", ":"),
            )
            db.execute(
                """
                UPDATE project_files
                SET parser_adapter = ?, parser_status = ?, grammar_name = ?,
                    grammar_version = ?, parser_source_sha256 = ?,
                    parser_error_count = ?, parser_missing_count = ?,
                    parser_diagnostics_json = ?, parser_diagnostics_truncated = ?,
                    parser_error = NULL, parser_updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    adapter.language_id,
                    status,
                    adapter_status.grammar_name,
                    adapter_status.grammar_version,
                    hashlib.sha256(normalized_source).hexdigest(),
                    parsed.error_count,
                    parsed.missing_count,
                    diagnostics_json,
                    int(parsed.diagnostics_truncated),
                    file_id,
                ),
            )
            parsed_count += 1
            if parsed.has_syntax_errors:
                syntax_error_count += 1
            try:
                structure = adapter.extract_structure(parsed, text)
                structure_counts = replace_file_structure(
                    db,
                    project_id,
                    file_id,
                    hashlib.sha256(normalized_source).hexdigest(),
                    structure,
                )
            except Exception as exc:
                clear_file_structure(db, file_id)
                structure_failed_count += 1
                structure_message = f"{type(exc).__name__}: {exc}"[:1_000]
                structure_failure_messages.append(f"{row['path']}: {structure_message}")
                mark_file_structure_status(
                    db,
                    file_id,
                    "failed",
                    error=structure_message,
                )
            else:
                structure_indexed_count += 1
                definition_count += structure_counts.definitions
                dependency_count += structure_counts.dependencies
                call_count += structure_counts.calls
        except Exception as exc:  # one malformed file must not abort the project inventory
            failed_count += 1
            message = f"{type(exc).__name__}: {exc}"[:1_000]
            failure_messages.append(f"{row['path']}: {message}")
            _update_unparsed_file(
                db,
                file_id,
                "failed",
                adapter=adapter.language_id,
                error=message,
            )
            clear_file_structure(db, file_id)
            structure_failed_count += 1
            structure_failure_messages.append(f"{row['path']}: {message}")
            mark_file_structure_status(db, file_id, "failed", error=message)

    if supported_count == 0:
        project_status = "unsupported"
    elif failed_count and parsed_count == 0:
        project_status = "failed"
    elif failed_count or supported_count < eligible_count:
        project_status = "partial"
    else:
        project_status = "completed"
    project_error = "; ".join(failure_messages)[:2_000] or None
    db.execute(
        """
        UPDATE projects
        SET parser_status = ?, parser_supported_file_count = ?,
            parser_parsed_file_count = ?, parser_syntax_error_file_count = ?,
            parser_failed_file_count = ?, parser_error = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            project_status,
            supported_count,
            parsed_count,
            syntax_error_count,
            failed_count,
            project_error,
            project_id,
        ),
    )
    structure_status = finalize_project_structure(
        db,
        project_id,
        eligible_count=eligible_count,
        supported_count=supported_count,
        indexed_count=structure_indexed_count,
        failed_count=structure_failed_count,
        counts=StructureCounts(definition_count, dependency_count, call_count),
        error="; ".join(structure_failure_messages)[:2_000] or None,
    )
    return ProjectParseSummary(
        status=project_status,
        eligible_file_count=eligible_count,
        supported_file_count=supported_count,
        parsed_file_count=parsed_count,
        syntax_error_file_count=syntax_error_count,
        failed_file_count=failed_count,
        structure_status=structure_status,
        definition_count=definition_count,
        dependency_count=dependency_count,
        call_count=call_count,
    )
