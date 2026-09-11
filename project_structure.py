"""Persistence boundary for Tree-sitter definitions, dependencies, and calls."""

from __future__ import annotations

import ast
import json
import posixpath
import sqlite3
from dataclasses import dataclass

from language_adapters import ExtractedStructure, SourceSpan


@dataclass(frozen=True)
class StructureCounts:
    definitions: int
    dependencies: int
    calls: int


@dataclass(frozen=True)
class DependencyResolutionCounts:
    internal: int
    ambiguous: int


@dataclass(frozen=True)
class CallResolutionCounts:
    internal: int
    ambiguous: int
    unresolved: int


def clear_project_structure(db: sqlite3.Connection, project_id: str) -> None:
    """Clear derived rows so a re-index is an atomic replacement, never an append."""
    db.execute("DELETE FROM project_calls WHERE project_id = ?", (project_id,))
    db.execute("DELETE FROM project_dependencies WHERE project_id = ?", (project_id,))
    db.execute("DELETE FROM project_symbols WHERE project_id = ?", (project_id,))


def clear_file_structure(db: sqlite3.Connection, file_id: int) -> None:
    db.execute("DELETE FROM project_calls WHERE file_id = ?", (file_id,))
    db.execute("DELETE FROM project_dependencies WHERE file_id = ?", (file_id,))
    db.execute("DELETE FROM project_symbols WHERE file_id = ?", (file_id,))


def mark_file_structure_status(
    db: sqlite3.Connection,
    file_id: int,
    status: str,
    *,
    error: str | None = None,
) -> None:
    db.execute(
        """
        UPDATE project_files
        SET structure_status = ?, definition_count = 0, dependency_count = 0,
            call_count = 0, structure_error = ?, structure_updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (status, error, file_id),
    )


def _span_values(span: SourceSpan) -> tuple[int, int, int, int, int, int]:
    return (
        span.start_line,
        span.start_column,
        span.end_line,
        span.end_column,
        span.start_byte,
        span.end_byte,
    )


def replace_file_structure(
    db: sqlite3.Connection,
    project_id: str,
    file_id: int,
    source_hash: str,
    structure: ExtractedStructure,
) -> StructureCounts:
    """Persist one file's index and resolve local nesting indices to database IDs."""
    symbol_ids: list[int] = []
    for definition in structure.definitions:
        parent_id = (
            symbol_ids[definition.parent_index]
            if definition.parent_index is not None
            else None
        )
        body_values: tuple[int | None, ...]
        if definition.body_span is None:
            body_values = (None, None, None, None)
        else:
            body_values = (
                definition.body_span.start_line,
                definition.body_span.end_line,
                definition.body_span.start_byte,
                definition.body_span.end_byte,
            )
        cursor = db.execute(
            """
            INSERT INTO project_symbols(
                project_id, file_id, parent_symbol_id, symbol_kind, name,
                qualified_name, nesting_depth, start_line, start_column,
                end_line, end_column, start_byte, end_byte, body_start_line,
                body_end_line, body_start_byte, body_end_byte, source_sha256,
                analysis_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                file_id,
                parent_id,
                definition.kind,
                definition.name,
                definition.qualified_name,
                definition.nesting_depth,
                *_span_values(definition.span),
                *body_values,
                source_hash,
                "pending" if definition.kind in {"function", "method"} else "not_applicable",
            ),
        )
        symbol_ids.append(int(cursor.lastrowid))

    for dependency in structure.dependencies:
        db.execute(
            """
            INSERT INTO project_dependencies(
                project_id, file_id, dependency_kind, module_name,
                imported_names_json, is_relative, start_line, start_column,
                end_line, end_column, start_byte, end_byte, source_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                file_id,
                dependency.kind,
                dependency.module_name,
                json.dumps(dependency.imported_names, separators=(",", ":")),
                int(dependency.is_relative),
                *_span_values(dependency.span),
                source_hash,
            ),
        )

    for call in structure.calls:
        caller_id = symbol_ids[call.caller_index] if call.caller_index is not None else None
        cursor = db.execute(
            """
            INSERT INTO project_calls(
                project_id, file_id, caller_symbol_id, callee, start_line,
                start_column, end_line, end_column, start_byte, end_byte,
                source_sha256, usage_kind, expected_return_types_json, detail_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete')
            """,
            (
                project_id,
                file_id,
                caller_id,
                call.callee,
                *_span_values(call.span),
                source_hash,
                call.usage_kind,
                json.dumps(call.expected_return_types, separators=(",", ":")),
            ),
        )
        call_id = int(cursor.lastrowid)
        db.executemany(
            """
            INSERT INTO project_call_arguments(
                call_id, ordinal, keyword_name, expression_kind,
                inferred_types_json, start_line, start_column, end_line,
                end_column, start_byte, end_byte
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    call_id,
                    ordinal,
                    argument.keyword_name,
                    argument.expression_kind,
                    json.dumps(argument.inferred_types, separators=(",", ":")),
                    *_span_values(argument.span),
                )
                for ordinal, argument in enumerate(call.arguments)
            ),
        )

    counts = StructureCounts(
        definitions=len(structure.definitions),
        dependencies=len(structure.dependencies),
        calls=len(structure.calls),
    )
    db.execute(
        """
        UPDATE project_files
        SET structure_status = 'indexed', definition_count = ?, dependency_count = ?,
            call_count = ?, structure_error = NULL,
            structure_updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (counts.definitions, counts.dependencies, counts.calls, file_id),
    )
    return counts


def _suffix_candidates(
    paths: dict[int, str],
    target: str,
) -> list[int]:
    normalized = target.casefold().lstrip("./")
    return [
        file_id
        for file_id, path in paths.items()
        if path.casefold() == normalized or path.casefold().endswith("/" + normalized)
    ]


def _python_dependency_targets(
    module_name: str,
    source_path: str,
    is_relative: bool,
) -> tuple[str, ...]:
    module = module_name
    base = ""
    if is_relative:
        dot_count = len(module) - len(module.lstrip("."))
        module = module[dot_count:]
        base = posixpath.dirname(source_path)
        for _ in range(max(0, dot_count - 1)):
            base = posixpath.dirname(base)
    module_path = module.replace(".", "/")
    target = posixpath.normpath(posixpath.join(base, module_path)) if module_path else base
    return (f"{target}.py", f"{target}/__init__.py")


def _script_dependency_targets(
    module_name: str,
    source_path: str,
    extensions: tuple[str, ...],
) -> tuple[str, ...]:
    target = posixpath.normpath(
        posixpath.join(posixpath.dirname(source_path), module_name)
    )
    suffix = posixpath.splitext(target)[1]
    if suffix:
        return (target,)
    return tuple(
        [*(target + extension for extension in extensions),
         *(target + "/index" + extension for extension in extensions)]
    )


def _rust_dependency_targets(module_name: str, source_path: str) -> tuple[str, ...]:
    parts = [part for part in module_name.split("::") if part and part != "*"]
    base = posixpath.dirname(source_path)
    if parts and parts[0] == "crate":
        parts.pop(0)
        source_parts = source_path.split("/")
        if "src" in source_parts:
            base = "/".join(source_parts[: source_parts.index("src") + 1])
    while parts and parts[0] in {"self", "super"}:
        if parts.pop(0) == "super":
            base = posixpath.dirname(base)
    if len(parts) > 1:
        parts.pop()  # the final segment is normally the imported symbol
    target = posixpath.normpath(posixpath.join(base, *parts)) if parts else base
    return (f"{target}.rs", f"{target}/mod.rs")


def _dependency_targets(
    language: str,
    module_name: str,
    source_path: str,
    is_relative: bool,
) -> tuple[str, ...]:
    if language == "python":
        return _python_dependency_targets(module_name, source_path, is_relative)
    if language in {"javascript", "typescript", "html"} and is_relative:
        return _script_dependency_targets(
            module_name,
            source_path,
            (".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx"),
        )
    if language == "shell" and is_relative:
        return _script_dependency_targets(module_name, source_path, (".sh", ".bash"))
    if language == "powershell" and is_relative:
        return _script_dependency_targets(module_name, source_path, (".psm1", ".ps1"))
    if language == "rust":
        return _rust_dependency_targets(module_name, source_path)
    return ()


def resolve_project_dependencies(
    db: sqlite3.Connection,
    project_id: str,
) -> DependencyResolutionCounts:
    paths = {
        int(row["id"]): str(row["path"])
        for row in db.execute(
            "SELECT id, path FROM project_files WHERE project_id = ?",
            (project_id,),
        ).fetchall()
    }
    rows = db.execute(
        """
        SELECT dependency.id, dependency.file_id, dependency.dependency_kind,
               dependency.module_name, dependency.is_relative, file.path AS source_path,
               file.language
        FROM project_dependencies AS dependency
        JOIN project_files AS file ON file.id = dependency.file_id
        WHERE dependency.project_id = ?
        ORDER BY dependency.id
        """,
        (project_id,),
    ).fetchall()
    internal_count = 0
    ambiguous_count = 0
    for row in rows:
        kind = str(row["dependency_kind"])
        module_name = str(row["module_name"])
        source_path = str(row["source_path"])
        language = str(row["language"] or "")
        is_relative = bool(row["is_relative"])
        candidate_ids: set[int] = set()
        if kind == "include":
            if is_relative:
                relative_target = posixpath.normpath(
                    posixpath.join(posixpath.dirname(source_path), module_name)
                )
                candidate_ids.update(_suffix_candidates(paths, relative_target))
                if not candidate_ids:
                    candidate_ids.update(_suffix_candidates(paths, module_name))
            missing_status = "unresolved" if is_relative else "external"
        else:
            for target in _dependency_targets(
                language, module_name, source_path, is_relative
            ):
                candidate_ids.update(_suffix_candidates(paths, target))
            missing_status = (
                "unresolved"
                if is_relative or language == "rust"
                else "external"
            )

        if len(candidate_ids) == 1:
            resolved_file_id = next(iter(candidate_ids))
            resolution_status = "internal"
            internal_count += 1
        elif len(candidate_ids) > 1:
            resolved_file_id = None
            resolution_status = "ambiguous"
            ambiguous_count += 1
        else:
            resolved_file_id = None
            resolution_status = missing_status
        db.execute(
            """
            UPDATE project_dependencies
            SET resolved_file_id = ?, resolution_status = ?
            WHERE id = ?
            """,
            (resolved_file_id, resolution_status, int(row["id"])),
        )
    return DependencyResolutionCounts(internal_count, ambiguous_count)


def _call_target_candidates(
    callee: str,
    caller: sqlite3.Row,
    symbols: list[sqlite3.Row],
    symbols_by_id: dict[int, sqlite3.Row],
    dependency_targets: dict[int, list[sqlite3.Row]],
    class_ancestors: dict[int, tuple[int, ...]],
    caller_class_id: int | None,
) -> list[int]:
    normalized = callee.strip()
    simple_name = normalized.replace("->", ".").replace("::", ".").rsplit(".", 1)[-1]
    language = str(caller["language"])
    case_insensitive = language in {"powershell", "sql"}
    normalized_qualifiers = normalized.replace("->", ".").replace("::", ".")
    qualifier = normalized_qualifiers.split(".", 1)[0]
    is_qualified_call = (
        "." in normalized_qualifiers or "->" in normalized or "::" in normalized
    )

    def names_equal(left: object, right: object) -> bool:
        left_text, right_text = str(left), str(right)
        return left_text.casefold() == right_text.casefold() if case_insensitive else left_text == right_text

    def callable_ids(rows: list[sqlite3.Row]) -> list[int]:
        """Resolve the effective explicit constructor, otherwise retain the class target."""
        values: list[int] = []
        for row in rows:
            row_id = int(row["id"])
            if str(row["symbol_kind"]) != "class":
                values.append(row_id)
                continue
            constructor_ids: list[int] = []
            for constructor_name in ("__init__", "__new__"):
                for class_id in (row_id, *class_ancestors.get(row_id, ())):
                    constructor_ids = [
                        int(candidate["id"])
                        for candidate in symbols
                        if candidate["parent_symbol_id"] == class_id
                        and str(candidate["name"]) == constructor_name
                    ]
                    if constructor_ids:
                        break
                if constructor_ids:
                    break
            values.extend(constructor_ids or [row_id])
        return list(dict.fromkeys(values))

    exact_rows = [
        row
        for row in symbols
        if names_equal(row["qualified_name"], normalized)
    ]
    exact = callable_ids(exact_rows)
    if len(exact) == 1:
        return exact

    caller_parent = str(caller["parent_qualified_name"] or "")
    caller_qualified = str(caller["caller_qualified_name"] or "")
    is_receiver_call = normalized.startswith(("self.", "cls.", "this.", "this->"))

    if language == "python" and is_receiver_call and caller_class_id is not None:
        for class_id in (caller_class_id, *class_ancestors.get(caller_class_id, ())):
            receiver_matches = [
                int(row["id"])
                for row in symbols
                if row["parent_symbol_id"] == class_id
                and str(row["symbol_kind"]) == "method"
                and names_equal(row["name"], simple_name)
            ]
            if receiver_matches:
                return receiver_matches

    class_scope = caller_parent
    if not class_scope and language in {"cpp", "rust"} and "::" in caller_qualified:
        class_scope = caller_qualified.rsplit("::", 1)[0]
    if (is_receiver_call or language in {"cpp", "rust"}) and class_scope:
        separator = "::" if language in {"cpp", "rust"} else "."
        qualified = class_scope + separator + simple_name
        receiver_matches = [
            int(row["id"])
            for row in symbols
            if names_equal(row["qualified_name"], qualified)
        ]
        if receiver_matches:
            return receiver_matches

    # A bare name first resolves in the caller's lexical scope.  This prevents
    # same-named closures elsewhere in a file from making a local call ambiguous.
    if not is_qualified_call and caller_qualified:
        if language == "python" and caller["caller_symbol_id"] is not None:
            scope_id: int | None = int(caller["caller_symbol_id"])
            while scope_id is not None:
                scope = symbols_by_id.get(scope_id)
                if scope is None or str(scope["symbol_kind"]) == "class":
                    break
                lexical_rows = [
                    row
                    for row in symbols
                    if row["parent_symbol_id"] == scope_id
                    and names_equal(row["name"], simple_name)
                ]
                lexical_matches = callable_ids(lexical_rows)
                if lexical_matches:
                    return lexical_matches
                parent_id = scope["parent_symbol_id"]
                scope_id = int(parent_id) if parent_id is not None else None
        else:
            lexical_scope = caller_qualified
            while lexical_scope:
                qualified = lexical_scope + "." + simple_name
                lexical_rows = [
                    row
                    for row in symbols
                    if names_equal(row["qualified_name"], qualified)
                ]
                lexical_matches = callable_ids(lexical_rows)
                if lexical_matches:
                    return lexical_matches
                if "." not in lexical_scope:
                    break
                lexical_scope = lexical_scope.rsplit(".", 1)[0]

    dependency_rows: dict[int, sqlite3.Row] = {}
    for dependency in dependency_targets.get(int(caller["file_id"]), []):
        imported_names = set(json.loads(str(dependency["imported_names_json"] or "[]")))
        dependency_module = str(dependency["module_name"])
        module_leaf = dependency_module.lstrip(".").rsplit(".", 1)[-1]
        rust_symbol = dependency_module.rsplit("::", 1)[-1]
        if (
            str(dependency["dependency_kind"]) == "include"
            or simple_name in imported_names
            or qualifier in imported_names
            or qualifier == module_leaf
            or (language == "rust" and simple_name == rust_symbol)
        ):
            target_file_id = int(dependency["resolved_file_id"])
            for row in symbols:
                if (
                    int(row["file_id"]) == target_file_id
                    and names_equal(row["name"], simple_name)
                ):
                    dependency_rows[int(row["id"])] = row
    dependency_matches = callable_ids(list(dependency_rows.values()))
    if dependency_matches:
        return sorted(dependency_matches)

    if is_qualified_call:
        return []

    same_file_rows = [
        row
        for row in symbols
        if int(row["file_id"]) == int(caller["file_id"])
        and names_equal(row["name"], simple_name)
    ]
    same_file = callable_ids(same_file_rows)
    if len(same_file) == 1:
        return same_file
    named_rows = [row for row in symbols if names_equal(row["name"], simple_name)]
    return callable_ids(named_rows)


def _python_class_ancestors(
    db: sqlite3.Connection,
    project_id: str,
    symbols: list[sqlite3.Row],
) -> dict[int, tuple[int, ...]]:
    """Build a conservative project-local Python inheritance map."""
    class_rows = [row for row in symbols if str(row["symbol_kind"]) == "class"]
    by_file_and_name = {
        (int(row["file_id"]), str(row["qualified_name"])): int(row["id"])
        for row in class_rows
    }
    by_leaf: dict[str, list[int]] = {}
    for row in class_rows:
        by_leaf.setdefault(str(row["name"]), []).append(int(row["id"]))
    declared_bases: dict[int, tuple[str, ...]] = {}

    class Collector(ast.NodeVisitor):
        def __init__(self, file_id: int) -> None:
            self.file_id = file_id
            self.stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            qualified_name = ".".join((*self.stack, node.name))
            symbol_id = by_file_and_name.get((self.file_id, qualified_name))
            if symbol_id is not None:
                bases = []
                for base in node.bases:
                    try:
                        value = ast.unparse(base).strip()
                    except Exception:
                        value = ""
                    if value:
                        bases.append(value.rsplit(".", 1)[-1])
                declared_bases[symbol_id] = tuple(bases)
            self.stack.append(node.name)
            for statement in node.body:
                self.visit(statement)
            self.stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node.name)
            for statement in node.body:
                self.visit(statement)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

    for file_row in db.execute(
        "SELECT id, content FROM project_files WHERE project_id = ? AND language = 'python'",
        (project_id,),
    ).fetchall():
        try:
            source = bytes(file_row["content"]).decode("utf-8")
            module = ast.parse(source)
        except (UnicodeDecodeError, SyntaxError, TypeError):
            continue
        Collector(int(file_row["id"])).visit(module)

    direct: dict[int, tuple[int, ...]] = {}
    for class_id, names in declared_bases.items():
        resolved: list[int] = []
        for name in names:
            candidates = by_leaf.get(name, [])
            if len(candidates) == 1:
                resolved.append(candidates[0])
        direct[class_id] = tuple(dict.fromkeys(resolved))

    ancestors: dict[int, tuple[int, ...]] = {}

    def expand(class_id: int, seen: set[int]) -> tuple[int, ...]:
        values: list[int] = []
        for base_id in direct.get(class_id, ()):
            if base_id in seen:
                continue
            values.append(base_id)
            values.extend(expand(base_id, {*seen, base_id}))
        return tuple(dict.fromkeys(values))

    for row in class_rows:
        class_id = int(row["id"])
        ancestors[class_id] = expand(class_id, {class_id})
    return ancestors


def resolve_project_calls(db: sqlite3.Connection, project_id: str) -> CallResolutionCounts:
    symbols = db.execute(
        """
        SELECT symbol.id, symbol.file_id, symbol.parent_symbol_id,
               symbol.symbol_kind, symbol.name, symbol.qualified_name
        FROM project_symbols AS symbol
        WHERE symbol.project_id = ?
          AND symbol.symbol_kind IN ('function', 'method', 'class')
        ORDER BY symbol.id
        """,
        (project_id,),
    ).fetchall()
    calls = db.execute(
        """
        SELECT call.id, call.file_id, call.caller_symbol_id, call.callee, file.language,
               caller.parent_symbol_id,
               caller.qualified_name AS caller_qualified_name,
               parent.qualified_name AS parent_qualified_name
        FROM project_calls AS call
        JOIN project_files AS file ON file.id = call.file_id
        LEFT JOIN project_symbols AS caller ON caller.id = call.caller_symbol_id
        LEFT JOIN project_symbols AS parent ON parent.id = caller.parent_symbol_id
        WHERE call.project_id = ? ORDER BY call.id
        """,
        (project_id,),
    ).fetchall()
    symbols_by_id = {int(row["id"]): row for row in symbols}
    class_ancestors = _python_class_ancestors(db, project_id, symbols)

    def enclosing_class_id(call: sqlite3.Row) -> int | None:
        symbol_id = call["caller_symbol_id"]
        while symbol_id is not None:
            symbol = symbols_by_id.get(int(symbol_id))
            if symbol is None:
                return None
            if str(symbol["symbol_kind"]) == "class":
                return int(symbol["id"])
            symbol_id = symbol["parent_symbol_id"]
        return None
    dependency_targets: dict[int, list[sqlite3.Row]] = {}
    for dependency in db.execute(
        """
        SELECT file_id, dependency_kind, module_name, imported_names_json,
               resolved_file_id
        FROM project_dependencies
        WHERE project_id = ? AND resolution_status = 'internal'
              AND resolved_file_id IS NOT NULL
        ORDER BY id
        """,
        (project_id,),
    ).fetchall():
        dependency_targets.setdefault(int(dependency["file_id"]), []).append(dependency)
    internal = ambiguous = unresolved = 0
    for call in calls:
        candidates = _call_target_candidates(
            str(call["callee"]),
            call,
            symbols,
            symbols_by_id,
            dependency_targets,
            class_ancestors,
            enclosing_class_id(call),
        )
        if len(candidates) == 1:
            resolved_symbol_id = candidates[0]
            status = "internal"
            internal += 1
        elif candidates:
            resolved_symbol_id = None
            status = "ambiguous"
            ambiguous += 1
        else:
            resolved_symbol_id = None
            status = "unresolved"
            unresolved += 1
        db.execute(
            "UPDATE project_calls SET resolved_symbol_id = ?, resolution_status = ? WHERE id = ?",
            (resolved_symbol_id, status, int(call["id"])),
        )
    return CallResolutionCounts(internal, ambiguous, unresolved)


def finalize_project_structure(
    db: sqlite3.Connection,
    project_id: str,
    *,
    eligible_count: int,
    supported_count: int,
    indexed_count: int,
    failed_count: int,
    counts: StructureCounts,
    error: str | None,
) -> str:
    if supported_count == 0:
        status = "unsupported"
    elif failed_count and indexed_count == 0:
        status = "failed"
    elif failed_count or supported_count < eligible_count:
        status = "partial"
    else:
        status = "completed"
    resolution_counts = resolve_project_dependencies(db, project_id)
    call_resolution_counts = resolve_project_calls(db, project_id)
    analyzable_count = int(
        db.execute(
            """
            SELECT COUNT(*) FROM project_symbols
            WHERE project_id = ? AND symbol_kind IN ('function', 'method')
            """,
            (project_id,),
        ).fetchone()[0]
    )
    db.execute(
        """
        UPDATE projects
        SET structure_status = ?, indexed_file_count = ?, definition_count = ?,
            dependency_count = ?, call_count = ?, resolved_dependency_count = ?,
            ambiguous_dependency_count = ?, structure_error = ?,
            function_analysis_status = ?, function_analysis_total_count = ?,
            function_analysis_completed_count = 0, function_analysis_failed_count = 0,
            function_analysis_skipped_count = 0, function_analysis_error = NULL,
            function_analysis_cache_hit_count = 0,
            function_analysis_model_request_count = 0,
            function_analysis_batch_request_count = 0,
            function_analysis_deterministic_count = 0,
            function_analysis_batch_fallback_count = 0,
            function_analysis_batch_error = NULL,
            function_analysis_updated_at = CURRENT_TIMESTAMP,
            call_compatibility_status = ?, call_compatibility_checked_count = 0,
            call_compatibility_incompatible_count = 0,
            call_compatibility_unknown_count = ?,
            call_compatibility_not_checked_count = 0,
            call_compatibility_error = NULL,
            call_compatibility_updated_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            status,
            indexed_count,
            counts.definitions,
            counts.dependencies,
            counts.calls,
            resolution_counts.internal,
            resolution_counts.ambiguous,
            error,
            "pending" if analyzable_count else "completed",
            analyzable_count,
            "pending" if call_resolution_counts.internal else "unavailable",
            call_resolution_counts.ambiguous + call_resolution_counts.unresolved,
            project_id,
        ),
    )
    return status
