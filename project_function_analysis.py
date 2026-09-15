"""Resumable, one-symbol-at-a-time Ollama analysis and persistence."""

from __future__ import annotations

import ast
import builtins
import hashlib
import json
import re
import sqlite3
import textwrap
import threading
from collections import OrderedDict
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Callable

import analysis_engine
from function_budget import prepare_budget, save_budget, run_budgeted
from app_config import (
    FUNCTION_ANALYSIS_BATCH_MAX_CHARS,
    FUNCTION_ANALYSIS_BATCH_MAX_OUTPUT_TOKENS,
    FUNCTION_ANALYSIS_ADAPTIVE_OUTPUT,
    FUNCTION_ANALYSIS_BATCH_SIZE,
    FUNCTION_ANALYSIS_CHUNK_CHARS,
    FUNCTION_ANALYSIS_CONTEXT_CHARS,
    FUNCTION_ANALYSIS_MAX_SOURCE_CHARS,
    OLLAMA_MODEL,
    current_ollama_model,
)
from project_inventory import decode_text_content
from project_call_compatibility import normalize_type, types_compatible
from dependency_context import dependency_order, file_dependency_items, resolved_context_items, python_control_flow_items
from deterministic_rules import javascript_null_member_issues, typescript_signature


class StaleSymbolSource(ValueError):
    """The indexed byte range no longer describes the stored file."""


@dataclass(frozen=True)
class FunctionAnalysisTask:
    symbol_id: int
    project_id: str
    user_id: int
    file_id: int
    file_path: str
    language: str
    symbol_kind: str
    qualified_name: str
    start_line: int
    end_line: int
    source_sha256: str
    function_sha256: str
    source: str
    semantic_function_sha256: str = ""
    analysis_context: str = ""
    cache_function_sha256: str = ""
    semantic_cache_function_sha256: str = ""
    include_inferred_context: bool = False
    enclosing_scope_names: tuple[str, ...] = ()
    module_defined_names: tuple[str, ...] = ()
    has_wildcard_import: bool = False


@dataclass(frozen=True)
class ProjectFunctionAnalysisSummary:
    status: str
    total_count: int
    completed_count: int
    failed_count: int
    skipped_count: int
    cache_hit_count: int = 0
    model_request_count: int = 0
    batch_request_count: int = 0
    deterministic_count: int = 0
    batch_fallback_count: int = 0
    batch_error: str | None = None


@dataclass(frozen=True)
class FunctionSourceChunk:
    index: int
    total: int
    start_line: int
    end_line: int
    source: str


@dataclass(frozen=True)
class _FunctionBatchCandidate:
    current: int
    task: FunctionAnalysisTask
    file_path: str
    qualified_name: str
    file_current: int
    file_total: int
    function_current: int
    function_total: int


ConnectionFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]
ProgressCallback = Callable[[str, int, int, str, str, int, int, int, int], None]
AnalysisRequest = Callable[..., analysis_engine.FunctionAnalysisResult]
BatchAnalysisRequest = Callable[..., dict[str, analysis_engine.FunctionAnalysisResult]]

_DEFAULT_ANALYSIS_REQUEST = analysis_engine.request_function_analysis
_DEFAULT_BATCH_ANALYSIS_REQUEST = analysis_engine.request_function_analysis_batch
_DEFAULT_CHUNK_ANALYSIS_REQUEST = analysis_engine.request_function_chunk_analysis


_UNDEFINED_ISSUE_PATTERN = re.compile(
    r"\b("
    r"undefined|not defined|missing import|not imported|not found|"
    r"never defined|prior definition|without imports?|without importing|"
    r"referenced before assignment|must exist globally|"
    r"called but not imported or defined"
    r")\b",
    re.IGNORECASE,
)

_BUILTIN_NAMES = set(dir(builtins))


def _absolute_line(task: FunctionAnalysisTask, node: ast.AST | None) -> int | None:
    line = getattr(node, "lineno", None)
    return task.start_line + int(line) - 1 if isinstance(line, int) else None


def _issue(
    *,
    severity: str,
    category: str,
    title: str,
    description: str,
    line: int | None = None,
    provenance: str = "deterministic",
    evidence: str | None = None,
    failure_type: str | None = None,
    trigger: str | None = None,
) -> analysis_engine.FunctionIssue:
    return analysis_engine.FunctionIssue(
        severity=severity,  # type: ignore[arg-type]
        category=category,  # type: ignore[arg-type]
        title=title[:240],
        description=description[:2_000],
        start_line=line,
        end_line=line,
        proof="source-v1" if evidence and failure_type and trigger else None,
        evidence=evidence,
        failure_type=failure_type,
        trigger=trigger,
        provenance=provenance,  # type: ignore[arg-type]
    )


def _result_with_issue_provenance(
    result: analysis_engine.FunctionAnalysisResult,
    provenance: str,
) -> analysis_engine.FunctionAnalysisResult:
    return result.model_copy(
        update={
            "issues": [
                issue
                if issue.provenance == "deterministic"
                else issue.model_copy(update={"provenance": provenance})
                for issue in result.issues
            ]
        }
    )


def _source_defines_name(source: str, name: str) -> bool:
    if not name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", name):
        return False
    leaf = name.rsplit(".", 1)[-1]
    try:
        module = ast.parse(source)
    except SyntaxError:
        module = None
    if module is not None:
        for node in ast.walk(module):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                if node.name == leaf:
                    return True
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                if node.id == leaf:
                    return True
            elif isinstance(node, ast.arg):
                if node.arg == leaf:
                    return True
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if (alias.asname or alias.name.split(".", 1)[0]) == leaf:
                        return True
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if (alias.asname or alias.name) == leaf:
                        return True
    patterns = (
        rf"(?m)^\s*(?:async\s+)?def\s+{re.escape(leaf)}\s*\(",
        rf"(?m)^\s*class\s+{re.escape(leaf)}\b",
        rf"(?m)^\s*{re.escape(leaf)}\s*[:=]",
        rf"(?m)^\s*import\s+.*\b{re.escape(leaf)}\b",
        rf"(?m)^\s*from\s+[\w.]+\s+import\s+.*\b{re.escape(leaf)}\b",
        rf"(?ms)^\s*from\s+[\w.]+\s+import\s*\([^)]*\b{re.escape(leaf)}\b[^)]*\)",
    )
    return any(re.search(pattern, source) for pattern in patterns)


def _python_source_resolves_name(
    source: str,
    task: FunctionAnalysisTask,
    name: str,
) -> bool:
    """Check module and enclosing-function bindings without using unrelated local scopes."""
    leaf = name.rsplit(".", 1)[-1]
    if leaf in _PYTHON_PREDEFINED_GLOBAL_NAMES or leaf in _BUILTIN_NAMES:
        return True
    if leaf in _build_python_module_context_index(source):
        return True
    module = _parse_python_module_for_context(source)
    if module is None:
        return _source_defines_name(source, leaf)
    parents = {
        child: parent
        for parent in ast.walk(module)
        for child in ast.iter_child_nodes(parent)
    }
    function_leaf = task.qualified_name.rsplit(".", 1)[-1]
    candidates = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == function_leaf
        and int(getattr(node, "lineno", 0) or 0) <= task.end_line
        and int(getattr(node, "end_lineno", 0) or 0) >= task.start_line
    ]
    if not candidates:
        return False
    current: ast.AST | None = candidates[0]
    while current is not None:
        current = parents.get(current)
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
            local_names, _global_names, _nonlocal_names = _python_scope_bindings(current)
            if leaf in local_names:
                return True
    return False


def _fixed_tuple_arity(type_name: str, source: str) -> int | None:
    normalized = type_name.strip()
    tuple_match = re.fullmatch(r"tuple\[(.+)\]", normalized, re.IGNORECASE)
    if tuple_match:
        body = tuple_match.group(1).strip()
        if "..." in body:
            return None
        return len([part for part in body.split(",") if part.strip()])
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", normalized):
        alias_pattern = (
            rf"(?m)^\s*{re.escape(normalized)}\s*=\s*tuple\s*\[([^\]]+)\]"
        )
        alias = re.search(alias_pattern, source)
        if alias and "..." not in alias.group(1):
            return len([part for part in alias.group(1).split(",") if part.strip()])
    return None


def _parameter_contracts_by_name(
    result: analysis_engine.FunctionAnalysisResult,
) -> dict[str, analysis_engine.FunctionParameterContract]:
    return {parameter.name: parameter for parameter in result.parameters}


def _python_function_from_task(
    task: FunctionAnalysisTask,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    try:
        module = ast.parse(_parseable_python_fragment(task.source))
    except SyntaxError:
        return None
    return _locate_task_function(task, module)


def _parameter_is_reassigned(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    name: str,
) -> bool:
    visitor = _PythonScopeBindingsVisitor()
    for statement in function.body:
        visitor.visit(statement)
    return name in visitor.bound


def _type_contract_allows_none(type_names: list[str]) -> bool:
    if not type_names:
        return True
    for type_name in type_names:
        normalized = re.sub(r"\s+", "", type_name).casefold()
        if (
            normalized in {"any", "object", "unknown", "none", "nonetype"}
            or "optional[" in normalized
            or "|none" in normalized
            or "none|" in normalized
            or re.search(r"union\[[^\]]*\bnone\b", normalized)
        ):
            return True
    return False


def _type_contract_is_iterable(type_names: list[str]) -> bool:
    iterable_markers = (
        "list",
        "tuple",
        "set",
        "frozenset",
        "dict",
        "str",
        "bytes",
        "range",
        "iterable",
        "iterator",
        "sequence",
        "collection",
        "mapping",
        "generator",
    )
    concrete = [re.sub(r"\s+", "", item).casefold() for item in type_names]
    return bool(concrete) and all(
        any(marker in item for marker in iterable_markers)
        for item in concrete
        if item not in {"none", "nonetype"}
    )


def _python_context_class_fields(context: str) -> dict[str, set[str]]:
    fields: dict[str, set[str]] = {}
    try:
        module = ast.parse(context)
    except SyntaxError:
        module = None
    if module is not None:
        for node in module.body:
            if not isinstance(node, ast.ClassDef):
                continue
            class_fields = fields.setdefault(node.name, set())
            for statement in node.body:
                if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                    class_fields.add(statement.target.id)
                elif isinstance(statement, ast.Assign):
                    class_fields.update(
                        target.id
                        for target in statement.targets
                        if isinstance(target, ast.Name)
                    )
        return fields
    current_class: str | None = None
    for line in context.splitlines():
        class_match = re.match(r"^class\s+([A-Za-z_][A-Za-z0-9_]*)\b", line)
        if class_match:
            current_class = class_match.group(1)
            fields.setdefault(current_class, set())
            continue
        field_match = re.match(r"^\s+([A-Za-z_][A-Za-z0-9_]*)\s*:", line)
        if current_class and field_match:
            fields[current_class].add(field_match.group(1))
        elif line and not line[0].isspace() and not line.startswith("#"):
            current_class = None
    return fields


def _static_string_expression(
    node: ast.AST | None,
    assignments: dict[str, list[ast.AST]],
    seen: set[str] | None = None,
) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string_expression(node.left, assignments, seen)
        right = _static_string_expression(node.right, assignments, seen)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        pieces: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                pieces.append(value.value)
            else:
                return None
        return "".join(pieces)
    if isinstance(node, ast.Name):
        visited = set(seen or ())
        if node.id in visited:
            return None
        values = assignments.get(node.id, [])
        if len(values) != 1:
            return None
        visited.add(node.id)
        return _static_string_expression(values[0], assignments, visited)
    return None


def _execute_query_from_expression(
    node: ast.AST | None,
    assignments: dict[str, list[ast.AST]],
) -> str | None:
    current = node
    while isinstance(current, ast.Call):
        if isinstance(current.func, ast.Attribute):
            if current.func.attr in {"execute", "executemany"} and current.args:
                return _static_string_expression(current.args[0], assignments)
            current = current.func.value
            continue
        break
    return None


def _selected_query_columns(query: str | None) -> set[str]:
    if not query:
        return set()
    match = re.search(r"\bselect\s+(.*?)\s+from\b", query, re.IGNORECASE | re.DOTALL)
    if not match:
        return set()
    projection = match.group(1)
    if "*" in projection:
        return set()
    columns: set[str] = set()
    for item in projection.split(","):
        expression = item.strip()
        alias = re.search(
            r"\bas\s+([A-Za-z_][A-Za-z0-9_]*)\s*$",
            expression,
            re.IGNORECASE,
        )
        if alias:
            columns.add(alias.group(1).casefold())
            continue
        bare = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*$", expression)
        if bare and "(" not in expression:
            columns.add(bare.group(1).casefold())
    return columns


def _selected_columns_by_row_name(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, set[str]]:
    assignments = _assignment_values(function)
    query_columns: dict[str, set[str]] = {}
    row_columns: dict[str, set[str]] = {}
    collection_columns: dict[str, set[str]] = {}
    for statement in function.body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                target = node.targets[0].id
                query = _execute_query_from_expression(node.value, assignments)
                columns = _selected_query_columns(query)
                call_name = _call_name(node.value.func) if isinstance(node.value, ast.Call) else ""
                if columns and call_name.endswith((".fetchall", ".fetchmany")):
                    collection_columns[target] = columns
                elif columns:
                    row_columns[target] = columns
                elif isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute):
                    owner = node.value.func.value
                    if isinstance(owner, ast.Name) and owner.id in query_columns:
                        if node.value.func.attr in {"fetchall", "fetchmany"}:
                            collection_columns[target] = query_columns[owner.id]
                        elif node.value.func.attr == "fetchone":
                            row_columns[target] = query_columns[owner.id]
                direct_query = _execute_query_from_expression(node.value, assignments)
                if direct_query and isinstance(node.value, ast.Call):
                    direct_name = _call_name(node.value.func)
                    if direct_name.endswith((".execute", ".executemany")):
                        query_columns[target] = _selected_query_columns(direct_query)
            elif isinstance(node, ast.For | ast.AsyncFor) and isinstance(node.target, ast.Name):
                columns: set[str] = set()
                if isinstance(node.iter, ast.Name):
                    columns = collection_columns.get(node.iter.id, set()) or query_columns.get(node.iter.id, set())
                if not columns:
                    columns = _selected_query_columns(
                        _execute_query_from_expression(node.iter, assignments)
                    )
                if columns:
                    row_columns[node.target.id] = columns
    return row_columns


def _issue_db_column_is_selected(
    issue: analysis_engine.FunctionIssue,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    text = f"{issue.title}\n{issue.description}\n{issue.evidence or ''}"
    if not re.search(r"\b(?:keyerror|missing (?:key|column)|lacks?\s+\w+\s+key)\b", text, re.IGNORECASE):
        return False
    row_columns = _selected_columns_by_row_name(function)
    for row_name, column in re.findall(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*['\"]([^'\"]+)['\"]\s*\]",
        text,
    ):
        if column.casefold() in row_columns.get(row_name, set()):
            return True
    return False


def _issue_contradicts_trusted_python_contract(
    issue: analysis_engine.FunctionIssue,
    task: FunctionAnalysisTask,
    result: analysis_engine.FunctionAnalysisResult,
) -> bool:
    """Reject model claims contradicted by source-derived annotations or schemas."""
    if issue.provenance not in {"model", "cache"}:
        return False
    function = _python_function_from_task(task)
    if function is None:
        return False
    text = f"{issue.title}\n{issue.description}\n{issue.failure_type or ''}".casefold()
    parameters = _parameter_contracts_by_name(result)
    for name, parameter in parameters.items():
        if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(name.casefold())}(?![A-Za-z0-9_])", text):
            continue
        if _parameter_is_reassigned(function, name):
            continue
        if (
            re.search(r"\b(?:may|might|could|possibly|potentially)?\s*(?:be\s+)?none\b|nonetype", text)
            and not _type_contract_allows_none(parameter.accepted_types)
        ):
            return True
        if (
            re.search(r"\b(?:non[- ]iterable|not iterable|cannot be iterated|iteration may fail)\b", text)
            and _type_contract_is_iterable(parameter.accepted_types)
        ):
            return True

    if "attributeerror" in text or "missing attribute" in text or "has no attribute" in text:
        fields = _python_context_class_fields(task.analysis_context)
        evidence = issue.evidence or ""
        for object_name, attribute in re.findall(
            r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b",
            evidence,
        ):
            parameter = parameters.get(object_name)
            if parameter is None or _parameter_is_reassigned(function, object_name):
                continue
            for type_name in parameter.accepted_types:
                leaf = re.split(r"[\[|, ]", type_name.strip(), maxsplit=1)[0].rsplit(".", 1)[-1]
                if attribute in fields.get(leaf, set()):
                    return True

    return _issue_db_column_is_selected(issue, function)


def _raise_exception_name(node: ast.Raise) -> str | None:
    value = node.exc
    if isinstance(value, ast.Call):
        value = value.func
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        parts: list[str] = []
        current: ast.AST | None = value
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts)) if parts else None
    return None


def _explicit_raise_names(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[str]:
    return _unique_strings(
        [
            name
            for node in _python_function_scope_nodes(function)
            if isinstance(node, ast.Raise)
            and (name := _raise_exception_name(node)) is not None
        ],
        30,
    )


def _model_issue_matches_explicit_raise(
    issue: analysis_engine.FunctionIssue,
    task: FunctionAnalysisTask,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    if issue.provenance not in {"model", "cache"}:
        return False
    text = (
        f"{issue.title}\n{issue.description}\n{issue.failure_type or ''}"
    ).casefold()
    if not any(marker in text for marker in ("raise", "raised", "throws", "thrown", "exception")):
        return False
    start = issue.start_line or task.start_line
    end = issue.end_line or start
    for node in _python_function_scope_nodes(function):
        if not isinstance(node, ast.Raise):
            continue
        name = _raise_exception_name(node)
        line = _absolute_line(task, node)
        if name is None or line is None or not start - 1 <= line <= end + 2:
            continue
        if name.casefold() in text or "raised_exception" in text:
            return True
    return False


def _model_issue_claims_safe_slice_failure(
    issue: analysis_engine.FunctionIssue,
    task: FunctionAnalysisTask,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    text = (
        f"{issue.title}\n{issue.description}\n{issue.failure_type or ''}"
    ).casefold()
    if "indexerror" not in text and not re.search(r"\b(?:out[- ]of[- ]range|index out of bounds)\b", text):
        return False
    start = issue.start_line or task.start_line
    end = issue.end_line or start
    overlapping = [
        node
        for node in _python_function_scope_nodes(function)
        if isinstance(node, ast.Subscript)
        and (line := _absolute_line(task, node)) is not None
        and start <= line <= end
    ]
    return bool(overlapping) and all(isinstance(node.slice, ast.Slice) for node in overlapping)


def _model_issue_claims_missing_file_despite_missing_ok(
    issue: analysis_engine.FunctionIssue,
    task: FunctionAnalysisTask,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    text = (
        f"{issue.title}\n{issue.description}\n{issue.failure_type or ''}"
    ).casefold()
    if "filenotfound" not in text and not (
        "file" in text and any(marker in text for marker in ("missing", "does not exist", "not exist"))
    ):
        return False
    start = issue.start_line or task.start_line
    end = issue.end_line or start
    for node in _python_function_scope_nodes(function):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        line = _absolute_line(task, node)
        if node.func.attr != "unlink" or line is None or not start - 1 <= line <= end + 1:
            continue
        if any(
            keyword.arg == "missing_ok"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        ):
            return True
    return False


def _isinstance_refined_names(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, set[str]]:
    """Return loop variables proven by an isinstance-filtered comprehension."""
    filtered_collections: dict[str, set[str]] = {}
    refined: dict[str, set[str]] = {}
    for node in _python_function_scope_nodes(function):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.ListComp | ast.SetComp | ast.GeneratorExp)
        ):
            types: set[str] = set()
            for generator in node.value.generators:
                if not isinstance(generator.target, ast.Name):
                    continue
                for condition in generator.ifs:
                    if (
                        isinstance(condition, ast.Call)
                        and isinstance(condition.func, ast.Name)
                        and condition.func.id == "isinstance"
                        and len(condition.args) >= 2
                        and isinstance(condition.args[0], ast.Name)
                        and condition.args[0].id == generator.target.id
                    ):
                        try:
                            types.add(ast.unparse(condition.args[1]).rsplit(".", 1)[-1])
                        except Exception:
                            pass
            if types:
                filtered_collections[node.targets[0].id] = types
        elif (
            isinstance(node, ast.For | ast.AsyncFor)
            and isinstance(node.target, ast.Name)
            and isinstance(node.iter, ast.Name)
            and node.iter.id in filtered_collections
        ):
            refined.setdefault(node.target.id, set()).update(
                filtered_collections[node.iter.id]
            )
    return refined


def _model_issue_contradicts_isinstance_refinement(
    issue: analysis_engine.FunctionIssue,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    if issue.provenance not in {"model", "cache"}:
        return False
    text = (
        f"{issue.title}\n{issue.description}\n{issue.evidence or ''}\n"
        f"{issue.failure_type or ''}"
    ).casefold()
    if "typeerror" not in text and "unknown type" not in text:
        return False
    for name, type_names in _isinstance_refined_names(function).items():
        if not re.search(rf"\b{re.escape(name.casefold())}\b", text):
            continue
        if any(
            re.search(rf"\bnon[- ]?{re.escape(type_name.casefold())}\b", text)
            or f"not {type_name.casefold()}" in text
            for type_name in type_names
        ):
            return True
    return False


def _model_issue_violates_annotated_input_contract(
    issue: analysis_engine.FunctionIssue,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    result: analysis_engine.FunctionAnalysisResult,
) -> bool:
    if issue.provenance not in {"model", "cache"}:
        return False
    text = f"{issue.title}\n{issue.description}".casefold()
    if not any(
        marker in text
        for marker in (
            "caller can pass",
            "caller may pass",
            "can pass any value",
            "including non-",
            "including a non-",
            "if a non-",
            "if non-",
            "wrong type",
        )
    ):
        return False
    for name, parameter in _parameter_contracts_by_name(result).items():
        if not re.search(rf"\b{re.escape(name.casefold())}\b", text):
            continue
        concrete = {
            value.casefold()
            for value in parameter.accepted_types
            if value.casefold() not in {"any", "object", "unknown", "dynamic"}
        }
        if concrete and not _parameter_is_reassigned(function, name):
            return True
    return False


def _issue_exception_names(issue: analysis_engine.FunctionIssue) -> set[str]:
    text = f"{issue.title}\n{issue.description}\n{issue.failure_type or ''}"
    return {
        match.rsplit(".", 1)[-1]
        for match in re.findall(
            r"\b(?:[A-Za-z_][A-Za-z0-9_]*\.)*([A-Z][A-Za-z0-9_]*(?:Error|Exception))\b",
            text,
        )
    }


def _exception_handler_names(node: ast.AST | None) -> set[str]:
    if node is None:
        return {"BaseException"}
    if isinstance(node, ast.Tuple):
        return set().union(*(_exception_handler_names(item) for item in node.elts))
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    return set()


def _handler_catches_exception(handler_names: set[str], exception_name: str) -> bool:
    if "BaseException" in handler_names:
        return True
    if exception_name in handler_names:
        return True
    exception_type = getattr(builtins, exception_name, None)
    if not isinstance(exception_type, type) or not issubclass(exception_type, BaseException):
        return False
    return any(
        isinstance(handler_type := getattr(builtins, handler_name, None), type)
        and issubclass(handler_type, BaseException)
        and issubclass(exception_type, handler_type)
        for handler_name in handler_names
    )


def _handler_reraises(handler: ast.ExceptHandler) -> bool:
    class Visitor(ast.NodeVisitor):
        reraises = False

        def visit_Raise(self, node: ast.Raise) -> None:
            if node.exc is None:
                self.reraises = True

        def visit_FunctionDef(self, _node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, _node: ast.AsyncFunctionDef) -> None:
            return

        def visit_Lambda(self, _node: ast.Lambda) -> None:
            return

    visitor = Visitor()
    for statement in handler.body:
        visitor.visit(statement)
    return visitor.reraises


def _node_is_in_try_body(
    node: ast.AST,
    try_node: ast.Try | ast.TryStar,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    current = node
    while (parent := parents.get(current)) is not None:
        if parent is try_node:
            return current in try_node.body
        current = parent
    return False


def _model_issue_exception_is_caught(
    issue: analysis_engine.FunctionIssue,
    task: FunctionAnalysisTask,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    if issue.provenance not in {"model", "cache"}:
        return False
    exception_names = _issue_exception_names(issue)
    if not exception_names:
        return False
    start = issue.start_line or task.start_line
    end = issue.end_line or start
    nodes = _python_function_scope_nodes(function)
    parents = {
        child: parent
        for parent in nodes
        for child in ast.iter_child_nodes(parent)
    }
    evidence_nodes = [
        node
        for node in nodes
        if isinstance(node, ast.Call | ast.Subscript | ast.Attribute)
        and (line := _absolute_line(task, node)) is not None
        and start <= line <= end
    ]
    for evidence_node in evidence_nodes:
        for try_node in (node for node in nodes if isinstance(node, ast.Try | ast.TryStar)):
            if not _node_is_in_try_body(evidence_node, try_node, parents):
                continue
            caught: set[str] = set()
            for handler in try_node.handlers:
                if _handler_reraises(handler):
                    continue
                handler_names = _exception_handler_names(handler.type)
                caught.update(
                    name
                    for name in exception_names
                    if _handler_catches_exception(handler_names, name)
                )
            if caught == exception_names:
                return True
    return False


def _same_ast_expression(left: ast.AST, right: ast.AST) -> bool:
    return ast.dump(left, include_attributes=False) == ast.dump(
        right,
        include_attributes=False,
    )


def _minimum_subscript_length(node: ast.Subscript) -> int | None:
    index: int | None = None
    if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int):
        index = node.slice.value
    elif (
        isinstance(node.slice, ast.UnaryOp)
        and isinstance(node.slice.op, ast.USub)
        and isinstance(node.slice.operand, ast.Constant)
        and isinstance(node.slice.operand.value, int)
    ):
        index = -node.slice.operand.value
    if index is None:
        return None
    return index + 1 if index >= 0 else abs(index)


def _length_comparison(
    node: ast.AST,
    sequence: ast.AST,
) -> tuple[ast.cmpop, int] | None:
    if not (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and len(node.comparators) == 1
        and isinstance(node.left, ast.Call)
        and isinstance(node.left.func, ast.Name)
        and node.left.func.id == "len"
        and len(node.left.args) == 1
        and _same_ast_expression(node.left.args[0], sequence)
        and isinstance(node.comparators[0], ast.Constant)
        and isinstance(node.comparators[0].value, int)
    ):
        return None
    return node.ops[0], node.comparators[0].value


def _guard_proves_subscript_available(
    guard: ast.AST,
    sequence: ast.AST,
    minimum_length: int,
    *,
    truthy: bool,
) -> bool:
    if minimum_length == 1:
        if truthy and _same_ast_expression(guard, sequence):
            return True
        if (
            not truthy
            and isinstance(guard, ast.UnaryOp)
            and isinstance(guard.op, ast.Not)
            and _same_ast_expression(guard.operand, sequence)
        ):
            return True
    comparison = _length_comparison(guard, sequence)
    if comparison is None:
        return False
    operator, boundary = comparison
    if truthy:
        return (
            isinstance(operator, ast.Gt) and boundary >= minimum_length - 1
        ) or (
            isinstance(operator, ast.GtE) and boundary >= minimum_length
        )
    return (
        isinstance(operator, ast.LtE) and boundary >= minimum_length - 1
    ) or (
        isinstance(operator, ast.Lt) and boundary >= minimum_length
    )


def _model_issue_index_is_short_circuit_guarded(
    issue: analysis_engine.FunctionIssue,
    task: FunctionAnalysisTask,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    if issue.provenance not in {"model", "cache"}:
        return False
    text = f"{issue.title}\n{issue.description}\n{issue.failure_type or ''}".casefold()
    if "indexerror" not in text and not re.search(
        r"\b(?:out[- ]of[- ]range|index out of bounds)\b",
        text,
    ):
        return False
    start = issue.start_line or task.start_line
    end = issue.end_line or start
    nodes = _python_function_scope_nodes(function)
    parents = {
        child: parent
        for parent in nodes
        for child in ast.iter_child_nodes(parent)
    }
    for subscript in (
        node
        for node in nodes
        if isinstance(node, ast.Subscript)
        and (line := _absolute_line(task, node)) is not None
        and start <= line <= end
    ):
        minimum_length = _minimum_subscript_length(subscript)
        if minimum_length is None:
            continue
        current: ast.AST = subscript
        while (parent := parents.get(current)) is not None:
            if isinstance(parent, ast.BoolOp):
                operand_index = next(
                    (
                        index
                        for index, operand in enumerate(parent.values)
                        if operand is current or _node_contains(operand, current)
                    ),
                    None,
                )
                if operand_index is not None:
                    truthy = isinstance(parent.op, ast.And)
                    if any(
                        _guard_proves_subscript_available(
                            guard,
                            subscript.value,
                            minimum_length,
                            truthy=truthy,
                        )
                        for guard in parent.values[:operand_index]
                    ):
                        return True
            current = parent
    return False


def _model_issue_is_ast_contradicted(
    issue: analysis_engine.FunctionIssue,
    task: FunctionAnalysisTask,
    result: analysis_engine.FunctionAnalysisResult,
) -> bool:
    function = _python_function_from_task(task)
    if function is None:
        return False
    return any(
        (
            _model_issue_matches_explicit_raise(issue, task, function),
            _model_issue_claims_safe_slice_failure(issue, task, function),
            _model_issue_claims_missing_file_despite_missing_ok(issue, task, function),
            _model_issue_contradicts_isinstance_refinement(issue, function),
            _model_issue_violates_annotated_input_contract(issue, function, result),
            _model_issue_exception_is_caught(issue, task, function),
            _model_issue_index_is_short_circuit_guarded(issue, task, function),
        )
    )


def _issue_is_source_proven_noise(
    issue: analysis_engine.FunctionIssue,
    task: FunctionAnalysisTask,
    result: analysis_engine.FunctionAnalysisResult,
    source_text: str,
) -> bool:
    text = f"{issue.title}\n{issue.description}".casefold()
    parameters = _parameter_contracts_by_name(result)

    if _issue_contradicts_trusted_python_contract(issue, task, result):
        return True

    if _model_issue_is_ast_contradicted(issue, task, result):
        return True

    if (
        "exception" in text
        and ("keyboardinterrupt" in text or "systemexit" in text)
        and re.search(r"\bexcept\s+Exception\b", task.source)
        and not re.search(r"\bexcept\s+BaseException\b", task.source)
    ):
        return True

    if (
        "return type" in text
        and "float" in text
        and "int" in text
        and _float_annotation_accepts_reported_int_return(task, issue)
    ):
        return True

    if "no direct undefined variable issue exists" in text:
        return True

    if "all database operations assume success" in text and "sqlite errors would propagate" in text:
        return True

    if "if db schema changes or queries fail" in text:
        return True

    if "if schema changes" in text:
        return True

    if "missing columns" in text:
        return True

    if "however select ensures it exists" in text:
        return True

    if "missing type annotations" in text:
        return True

    if "data corruption" in text and "infinite" in text:
        return True

    if (
        "user" in text
        and "may be none" in text
        and "user[\"" in text
        and re.search(r"\bif\s+user\s+is\s+None\b", task.source)
    ):
        return True

    if "may be none" in text and ".get" in text:
        for name in _issue_referenced_names(issue):
            leaf = name.rsplit(".", 1)[-1]
            if re.search(rf"\b{re.escape(leaf)}\s+is\s+not\s+None\b", task.source):
                return True

    if "may raise oserror" in text and "caught" in text:
        return True

    if "via getattr" in text and "attributeerror" in text:
        return True

    if "getattr(" in (issue.evidence or "").casefold() and "attributeerror" in text:
        return True

    if "no issue detected" in text:
        return True

    unpack_match = re.search(r"['`]([A-Za-z_][A-Za-z0-9_]*)['`].*unpack", text)
    if unpack_match:
        parameter = parameters.get(unpack_match.group(1))
        if parameter and any(_fixed_tuple_arity(type_name, source_text) for type_name in parameter.accepted_types):
            return True

    if "timeout" in text and re.search(r"\btimeout\s*=", task.source):
        return True

    return False


def _issue_is_speculative_model_noise(
    issue: analysis_engine.FunctionIssue,
) -> bool:
    """Drop unanchored model-only speculation that is not actionable against local source."""
    if issue.provenance not in {"model", "cache"}:
        return False
    text = f"{issue.title}\n{issue.description}".casefold()

    if re.fullmatch(r"(potential issue\s*)+", text):
        return True

    if "syntax appears valid" in text or "no syntax errors detected" in text:
        return True

    self_negating_markers = (
        "thus this access is safe",
        "this access is safe",
        "is safe under current logic",
        "which mitigates injection",
        "the code passes parameters via tuple",
        "the subsequent raise handles it",
        "this call is safe",
        "the function call is syntactically correct",
        "the default truncates entire buffer which is intended here",
        "no –",
    )
    if any(marker in text for marker in self_negating_markers):
        return True

    if issue.start_line is not None or issue.end_line is not None:
        return issue.severity == "info"

    if issue.severity == "info":
        return True

    speculative_markers = (
        "assumes that",
        "assumes these keys exist",
        "depending on its implementation",
        "depending on implementation",
        "constructor signature unknown",
        "if the source does not",
        "if the caller",
        "if callers",
        "if this import fails",
        "implementation details are unknown",
        "cannot be verified statically",
        "cannot be determined statically",
        "assumed available via project context",
        "not visible here",
        "unknown from context",
        "the source does not guarantee",
        "database connection is not available",
        "transaction cannot be started",
        "signature requires",
        "signature is inferred",
        "not explicitly handled here",
        "potentially",
        "might",
        "may ",
        "could ",
        "would raise",
        "would occur",
        "callers must handle",
    )
    if not any(marker in text for marker in speculative_markers):
        return False

    if issue.severity == "unsafe":
        return True
    if issue.severity == "warning" and issue.category == "maintainability":
        return True
    return False


_PROOF_PLACEHOLDERS = {
    "",
    "n/a",
    "na",
    "none",
    "null",
    "potential issue",
    "unknown",
    "unspecified",
}
_CALL_CLAIM_PATTERN = re.compile(
    r"\b(?:call|called|calling|invocation|await|awaited|coroutine|callee)\b",
    re.IGNORECASE,
)


def _normalized_evidence(value: str) -> str:
    candidate = value.strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] == "`":
        candidate = candidate[1:-1].strip()
    return " ".join(candidate.split())


def _proof_value_is_concrete(value: str | None, *, minimum: int) -> bool:
    if value is None:
        return False
    normalized = " ".join(value.split()).casefold()
    return len(normalized) >= minimum and normalized not in _PROOF_PLACEHOLDERS


def _evidence_locations(
    task: FunctionAnalysisTask,
    evidence: str,
) -> list[tuple[int, int]]:
    """Locate exact evidence in a function, with a conservative one-line whitespace fallback."""
    candidate = evidence.strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] == "`":
        candidate = candidate[1:-1].strip()
    if not candidate:
        return []
    locations: set[tuple[int, int]] = set()
    offset = 0
    while True:
        offset = task.source.find(candidate, offset)
        if offset < 0:
            break
        start = task.start_line + task.source.count("\n", 0, offset)
        end = start + candidate.count("\n")
        locations.add((start, end))
        offset += max(1, len(candidate))
    if locations:
        return sorted(locations)

    normalized = _normalized_evidence(candidate)
    if not normalized:
        return []
    for index, line in enumerate(task.source.splitlines()):
        if normalized in _normalized_evidence(line):
            absolute = task.start_line + index
            locations.add((absolute, absolute))
    return sorted(locations)


def _issue_span_contains(
    issue: analysis_engine.FunctionIssue,
    location: tuple[int, int],
) -> bool:
    if issue.start_line is None:
        return False
    issue_end = issue.end_line or issue.start_line
    return issue.start_line <= location[0] and issue_end >= location[1]


def _issue_has_noncomment_source(
    task: FunctionAnalysisTask,
    issue: analysis_engine.FunctionIssue,
) -> bool:
    if issue.start_line is None:
        return False
    end_line = issue.end_line or issue.start_line
    lines = task.source.splitlines()
    relative_start = issue.start_line - task.start_line
    relative_end = end_line - task.start_line
    if relative_start < 0 or relative_end >= len(lines):
        return False
    for line in lines[relative_start : relative_end + 1]:
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", "//", "/*", "*", "--")):
            return True
    return False


def _parser_diagnostics(value: object) -> list[dict[str, object]]:
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(decoded, list):
        return []
    diagnostics: list[dict[str, object]] = []
    for item in decoded:
        if not isinstance(item, dict):
            continue
        start_line = item.get("start_line")
        end_line = item.get("end_line")
        if not isinstance(start_line, int) or isinstance(start_line, bool):
            continue
        if not isinstance(end_line, int) or isinstance(end_line, bool):
            end_line = start_line
        diagnostics.append({**item, "start_line": start_line, "end_line": end_line})
    return diagnostics


def _diagnostic_overlaps_issue(
    diagnostic: dict[str, object],
    issue: analysis_engine.FunctionIssue,
) -> bool:
    if issue.start_line is None:
        return False
    issue_end = issue.end_line or issue.start_line
    diagnostic_start = int(diagnostic["start_line"])
    diagnostic_end = int(diagnostic["end_line"])
    return diagnostic_start <= issue_end and diagnostic_end >= issue.start_line


def _parser_syntax_issues(
    task: FunctionAnalysisTask,
    diagnostics: list[dict[str, object]],
) -> list[analysis_engine.FunctionIssue]:
    issues: list[analysis_engine.FunctionIssue] = []
    for diagnostic in diagnostics:
        line = int(diagnostic["start_line"])
        end_line = int(diagnostic["end_line"])
        if end_line < task.start_line or line > task.end_line:
            continue
        line = max(task.start_line, line)
        relative = line - task.start_line
        source_lines = task.source.splitlines()
        evidence = source_lines[relative].strip() if 0 <= relative < len(source_lines) else None
        message = str(diagnostic.get("message") or "The language parser rejected this syntax.")
        issues.append(
            _issue(
                severity="error",
                category="syntax",
                title="Parser syntax error",
                description=message,
                line=line,
                evidence=evidence or None,
                failure_type="SyntaxError",
                trigger="The parser reads this source file.",
            )
        )
    return issues


def _issue_claims_call_problem(issue: analysis_engine.FunctionIssue) -> bool:
    text = f"{issue.title}\n{issue.description}\n{issue.failure_type or ''}".casefold()
    if _CALL_CLAIM_PATTERN.search(text):
        return True
    return "argument" in text and any(
        marker in text
        for marker in ("missing", "unexpected", "too many", "duplicate", "signature")
    )


def _issue_claims_syntax_problem(issue: analysis_engine.FunctionIssue) -> bool:
    if issue.category == "syntax":
        return True
    text = (
        f"{issue.title}\n{issue.description}\n{issue.failure_type or ''}"
    ).casefold()
    return bool(
        re.search(
            r"\b(?:syntaxerror|indentationerror|taberror|invalid syntax|syntax error|"
            r"unterminated|unmatched|unclosed|missing (?:closing )?"
            r"(?:parenthes(?:is|es)|bracket|brace|quote|colon)|"
            r"expected (?:a )?(?:closing )?(?:parenthes(?:is|es)|bracket|brace|colon))\b",
            text,
        )
    )


def _indexed_call_overlaps_issue(
    db: sqlite3.Connection,
    task: FunctionAnalysisTask,
    issue: analysis_engine.FunctionIssue,
) -> bool:
    if issue.start_line is None:
        return False
    end_line = issue.end_line or issue.start_line
    return db.execute(
        """
        SELECT 1
        FROM project_calls
        WHERE caller_symbol_id = ? AND start_line <= ? AND end_line >= ?
        LIMIT 1
        """,
        (task.symbol_id, end_line, issue.start_line),
    ).fetchone() is not None


def _verified_model_issue(
    db: sqlite3.Connection,
    task: FunctionAnalysisTask,
    issue: analysis_engine.FunctionIssue,
    *,
    structure_status: str,
    diagnostics: list[dict[str, object]],
) -> analysis_engine.FunctionIssue | None:
    """Return a source-matched model issue or reject the unsupported claim."""
    if issue.provenance not in {"model", "cache"}:
        return issue
    if issue.proof != "source-v1":
        return None
    if not _proof_value_is_concrete(issue.evidence, minimum=3):
        return None
    if not _proof_value_is_concrete(issue.failure_type, minimum=3):
        return None
    if not _proof_value_is_concrete(issue.trigger, minimum=6):
        return None
    if not _proof_value_is_concrete(issue.reachability, minimum=6):
        return None
    if not _proof_value_is_concrete(issue.guard_check, minimum=6):
        return None
    if any(not excerpt.strip() or len(excerpt) > 1_000 or excerpt not in task.source
           for excerpt in issue.guard_evidence):
        return None

    locations = _evidence_locations(task, issue.evidence or "")
    if not locations:
        return None
    matching = [location for location in locations if _issue_span_contains(issue, location)]
    updates: dict[str, object] = {}
    if matching:
        location = matching[0]
        if issue.end_line is None and location[1] > location[0]:
            updates["end_line"] = location[1]
    elif len(locations) == 1:
        location = locations[0]
        updates["start_line"] = location[0]
        updates["end_line"] = location[1]
    else:
        return None
    if updates:
        issue = issue.model_copy(update=updates)
    if not _issue_has_noncomment_source(task, issue):
        return None
    if _issue_claims_syntax_problem(issue):
        if not any(_diagnostic_overlaps_issue(item, issue) for item in diagnostics):
            return None
    if (
        structure_status == "indexed"
        and _issue_claims_call_problem(issue)
        and not _indexed_call_overlaps_issue(db, task, issue)
    ):
        return None
    return issue


def issue_report_tier(issue: analysis_engine.FunctionIssue) -> str:
    """Return the stable report lane for an issue."""
    if issue.category == "maintainability" or issue.severity == "info":
        return "advisory"
    return "defect"


def _normalize_verified_model_severity(
    issue: analysis_engine.FunctionIssue,
    diagnostics: list[dict[str, object]],
) -> analysis_engine.FunctionIssue:
    """Reserve hard errors for parser/static corroboration.

    Exact evidence proves where a model claim came from, but by itself does not prove that the
    failure is inevitable. Such reachable, conditional failures belong in the Unsafe lane.
    """
    if issue.provenance in {"model", "cache"} and issue.assessment == "contract_risk":
        return issue.model_copy(update={"severity": "unsafe"})
    if issue.provenance not in {"model", "cache"} or issue.severity != "error":
        return issue
    if issue.category == "syntax" and any(
        _diagnostic_overlaps_issue(item, issue) for item in diagnostics
    ):
        return issue
    return issue.model_copy(update={"severity": "unsafe"})


def _float_annotation_accepts_reported_int_return(
    task: FunctionAnalysisTask,
    issue: analysis_engine.FunctionIssue,
) -> bool:
    try:
        module = ast.parse(textwrap.dedent(task.source))
    except SyntaxError:
        return False
    function = next(
        (
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ),
        None,
    )
    if function is None or _annotation_text(function.returns) != "float":
        return False
    for node in _python_function_scope_nodes(function):
        if not isinstance(node, ast.Return):
            continue
        if issue.start_line is not None and _absolute_line(task, node) != issue.start_line:
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, int):
            return not isinstance(node.value.value, bool)
    return False


def _issue_referenced_names(issue: analysis_engine.FunctionIssue) -> set[str]:
    text = f"{issue.title}\n{issue.description}"
    names: set[str] = set()
    for quote_pattern in (
        r"`([^`]+)`",
        r'"([^"]+)"',
        r"(?<![A-Za-z])'([A-Za-z_][A-Za-z0-9_.]*)'(?![A-Za-z])",
    ):
        for quoted in re.findall(quote_pattern, text):
            names.update(
                match
                for match in re.findall(r"\b[A-Za-z_][A-Za-z0-9_.]*\b", quoted)
            )
    names.update(
        match
        for match in re.findall(r"\b(?:function|variable|module|import)\s+([A-Za-z_][A-Za-z0-9_.]*)\b", text, re.IGNORECASE)
    )
    for globals_phrase in re.findall(
        r"\b(?:global variables?|globals)\s+(.+?)\s+"
        r"(?:undefined|not defined|referenced before assignment|must exist globally)\b",
        text,
        re.IGNORECASE,
    ):
        for token in re.findall(
            r"\b[A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z_][A-Za-z0-9_]*)*\b",
            globals_phrase,
        ):
            parts = token.split("/")
            for index, part in enumerate(parts):
                if index > 0 and "_" not in part and "_" in parts[0]:
                    continue
                names.add(part)
    names.update(
        match
        for match in re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", text)
    )
    ignored = {
        "for",
        "from",
        "of",
        "is",
        "name",
        "variable",
        "variables",
        "function",
        "module",
        "import",
        "globals",
        "global",
        "uses",
        "and",
        "or",
    }
    filtered = {name for name in names if name.casefold() not in ignored}
    return {
        name
        for name in filtered
        if not any(other != name and other.endswith(f"_{name}") for other in filtered)
    }


def _model_variable_flow_claim_is_contradicted(
    task: FunctionAnalysisTask,
    issue: analysis_engine.FunctionIssue,
) -> bool:
    """Reject a model flow claim when the scope analyser proves the evidenced load is assigned."""
    if (
        task.language != "python"
        or issue.provenance not in {"model", "cache"}
        or not _UNDEFINED_ISSUE_PATTERN.search(
            f"{issue.title}\n{issue.description}\n{issue.failure_type or ''}"
        )
    ):
        return False
    function = _python_function_from_task(task)
    if function is None:
        return False
    names = {
        name.rsplit(".", 1)[-1]
        for name in _issue_referenced_names(issue)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", name)
    }
    if not names or issue.start_line is None:
        return False
    relative_start = issue.start_line - task.start_line + 1
    relative_end = (issue.end_line or issue.start_line) - task.start_line + 1
    loads = [
        node
        for node in _python_function_scope_nodes(function)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in names
        and relative_start <= int(getattr(node, "lineno", 0) or 0) <= relative_end
    ]
    if not loads:
        # Models commonly anchor an unbound-local claim to ``except`` or ``if``
        # while naming the actual load on the next line.  Include only a tight
        # source neighborhood so unrelated uses elsewhere cannot disprove it.
        loads = [
            node
            for node in _python_function_scope_nodes(function)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in names
            and relative_start - 1
            <= int(getattr(node, "lineno", 0) or 0)
            <= relative_end + 3
        ]
    if not loads:
        return False
    failures = {
        (
            node.id,
            int(getattr(node, "lineno", 0) or 0),
            int(getattr(node, "col_offset", 0) or 0),
        )
        for node, _failure_type, _description in _PythonDefiniteAssignmentAnalyzer(
            function,
            _python_context_defined_names(task),
        ).analyze()
    }
    return all(
        (
            node.id,
            int(getattr(node, "lineno", 0) or 0),
            int(getattr(node, "col_offset", 0) or 0),
        )
        not in failures
        for node in loads
    )


def _demote_unanchored_model_error(
    issue: analysis_engine.FunctionIssue,
) -> analysis_engine.FunctionIssue:
    """Treat unanchored model-only critical findings as warnings unless evidence is strong."""
    if (
        issue.provenance != "model"
        or issue.severity != "error"
        or issue.start_line is not None
        or issue.end_line is not None
        or issue.category in {"syntax", "security"}
    ):
        return issue
    return issue.model_copy(update={"severity": "warning"})


def _issue_overlap_key(issue: analysis_engine.FunctionIssue) -> tuple[object, ...] | None:
    text = f"{issue.title}\n{issue.description}".casefold()
    names = tuple(sorted(name.casefold() for name in _issue_referenced_names(issue)))
    line = issue.start_line or issue.end_line
    failure_type = (issue.failure_type or "").strip().casefold()

    if _UNDEFINED_ISSUE_PATTERN.search(text) and names:
        return ("undefined-name", names, line)
    if (
        ("missing required" in text or "wrong number" in text or "signature" in text)
        and "argument" in text
    ):
        return ("call-arguments", names, line)
    if "none" in text and (
        "dereference" in text or "attributeerror" in text or "may be none" in text
    ):
        return ("none-dereference", names, line)
    if "sqlite3.row" in text and "attribute" in text:
        return ("sqlite-row-attribute", names, line)
    if "sql" in text and (
        "injection" in text or "interpolation" in text or "interpolated" in text or "f-string" in text
    ):
        return ("sql-injection", names, line)
    if "file" in text and (
        "context manager" in text or "explicit close" in text or "leak" in text
    ):
        return ("file-handle", names, line)
    if "mutable" in text and "default" in text:
        return ("mutable-default", names, line)
    if "return" in text and (
        "type" in text or "annotation" in text or "annotated" in text or "contradict" in text
    ):
        return ("return-type", names, line)
    if "broad exception" in text or "except exception" in text:
        return ("broad-exception", line)
    if "syntax" in text or "missing colon" in text:
        return ("syntax", line)
    if failure_type and line is not None:
        return ("failure", issue.category, failure_type, names, line)
    return None


def _issue_strength(issue: analysis_engine.FunctionIssue) -> tuple[int, int, int, int]:
    provenance_rank = {
        "model": 0,
        "cache": 1,
        "fallback": 2,
        "deterministic": 3,
    }
    severity_rank = {"info": 0, "warning": 1, "unsafe": 2, "error": 3}
    category_rank = {
        "maintainability": 0,
        "logic": 1,
        "resource": 2,
        "runtime": 3,
        "type": 4,
        "security": 5,
        "syntax": 6,
    }
    return (
        int(issue.start_line is not None or issue.end_line is not None),
        provenance_rank.get(issue.provenance, 0),
        severity_rank.get(issue.severity, 0),
        category_rank.get(issue.category, 0),
    )


def _deduplicate_overlapping_issues(
    issues: list[analysis_engine.FunctionIssue],
) -> tuple[list[analysis_engine.FunctionIssue], bool]:
    best_by_key: dict[tuple[object, ...], tuple[int, analysis_engine.FunctionIssue]] = {}
    unique: list[tuple[int, analysis_engine.FunctionIssue]] = []
    for index, issue in enumerate(issues):
        key = _issue_overlap_key(issue)
        if key is None:
            unique.append((index, issue))
            continue
        existing = best_by_key.get(key)
        if existing is None or _issue_strength(issue) > _issue_strength(existing[1]):
            best_by_key[key] = (index, issue)
    selected = [*unique, *best_by_key.values()]
    selected.sort(key=lambda item: item[0])
    deduplicated = [issue for _index, issue in selected]
    return deduplicated, len(deduplicated) != len(issues)


def _semantic_function_cache_sha256(language: str, source: str) -> str:
    """Return a stable cache hash that ignores non-semantic Python formatting."""
    if language == "python":
        try:
            tree = ast.parse(_parseable_python_fragment(source))
        except SyntaxError:
            pass
        else:
            return hashlib.sha256(
                ast.dump(tree, annotate_fields=True, include_attributes=False).encode(
                    "utf-8"
                )
            ).hexdigest()
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


_PYTHON_ANALYSIS_CONTEXT_VERSION = "multilanguage-dependency-slice-v4"
_FUNCTION_ANALYSIS_CACHE_VERSION = "cache-v15-module-and-annotation-precision"


def _cache_contract_version() -> str:
    """Version cache entries independently from the wire/schema contract."""
    return (
        f"{analysis_engine.FUNCTION_ANALYSIS_CONTRACT_VERSION}+"
        f"{_FUNCTION_ANALYSIS_CACHE_VERSION}"
    )
_PYTHON_MODULE_CONTEXT_CACHE_MAX_FILES = 32
_PYTHON_MODULE_CONTEXT_CACHE: OrderedDict[
    str,
    dict[str, tuple[str, ...]],
] = OrderedDict()
_PYTHON_MODULE_CONTEXT_CACHE_LOCK = threading.Lock()


class _PythonReferenceVisitor(ast.NodeVisitor):
    """Collect names used by one function without descending into nested scopes."""

    def __init__(self) -> None:
        self.loaded: set[str] = set()
        self.local: set[str] = set()
        self.global_names: set[str] = set()
        self.direct_calls: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.loaded.add(node.id)
        elif isinstance(node.ctx, ast.Store | ast.Del):
            self.local.add(node.id)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            self.direct_calls.add(node.func.id)
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self.global_names.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.global_names.update(node.names)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.local.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.local.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.local.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_Import(self, node: ast.Import) -> None:
        self.local.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.local.update(alias.asname or alias.name for alias in node.names)


def _python_function_references(
    source: str,
    qualified_name: str,
) -> tuple[set[str], set[str]]:
    try:
        module = ast.parse(_parseable_python_fragment(source))
    except SyntaxError:
        return set(), set()
    leaf_name = qualified_name.rsplit(".", 1)[-1]
    function = next(
        (
            node
            for node in ast.walk(module)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == leaf_name
        ),
        None,
    )
    if function is None:
        return set(), set()
    visitor = _PythonReferenceVisitor()
    arguments = (
        *function.args.posonlyargs,
        *function.args.args,
        *function.args.kwonlyargs,
    )
    visitor.local.update(argument.arg for argument in arguments)
    if function.args.vararg is not None:
        visitor.local.add(function.args.vararg.arg)
    if function.args.kwarg is not None:
        visitor.local.add(function.args.kwarg.arg)
    for argument in arguments:
        if argument.annotation is not None:
            visitor.visit(argument.annotation)
    for value in (*function.args.defaults, *function.args.kw_defaults):
        if value is not None:
            visitor.visit(value)
    if function.args.vararg is not None and function.args.vararg.annotation is not None:
        visitor.visit(function.args.vararg.annotation)
    if function.args.kwarg is not None and function.args.kwarg.annotation is not None:
        visitor.visit(function.args.kwarg.annotation)
    if function.returns is not None:
        visitor.visit(function.returns)
    for decorator in function.decorator_list:
        visitor.visit(decorator)
    for statement in function.body:
        visitor.visit(statement)
    local_names = visitor.local - visitor.global_names
    referenced = {
        name
        for name in visitor.loaded - local_names
        if name not in _BUILTIN_NAMES and name not in {"True", "False", "None"}
    }
    return referenced, visitor.direct_calls - local_names


def _python_function_signature(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    qualified_name: str | None = None,
) -> str:
    decorators = [f"@{ast.unparse(item)}" for item in function.decorator_list[-3:]]
    prefix = "async def" if isinstance(function, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(function.returns)}" if function.returns is not None else ""
    signature = f"{prefix} {function.name}({ast.unparse(function.args)}){returns}: ..."
    if qualified_name and qualified_name != function.name:
        signature = f"# qualified name: {qualified_name}\n{signature}"
    return "\n".join([*decorators, signature])


def _python_symbol_declaration(source: str, qualified_name: str) -> str | None:
    try:
        module = ast.parse(_parseable_python_fragment(source))
    except SyntaxError:
        return None
    leaf_name = qualified_name.rsplit(".", 1)[-1]
    function = next(
        (
            node
            for node in ast.walk(module)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == leaf_name
        ),
        None,
    )
    if function is not None:
        return _python_function_signature(function, qualified_name=qualified_name)
    class_node = next(
        (
            node
            for node in ast.walk(module)
            if isinstance(node, ast.ClassDef) and node.name == leaf_name
        ),
        None,
    )
    if class_node is None:
        return None
    bases = [ast.unparse(base) for base in class_node.bases]
    keywords = [
        f"{keyword.arg}={ast.unparse(keyword.value)}"
        for keyword in class_node.keywords
        if keyword.arg
    ]
    suffix = f"({', '.join([*bases, *keywords])})" if bases or keywords else ""
    declarations = [f"class {class_node.name}{suffix}:"]
    for statement in class_node.body:
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            declaration = f"    {statement.target.id}: {ast.unparse(statement.annotation)}"
            if statement.value is not None:
                rendered_value = ast.unparse(statement.value)
                if len(rendered_value) <= 120:
                    declaration += f" = {rendered_value}"
            declarations.append(declaration)
        elif isinstance(statement, ast.Assign):
            names = [
                target.id
                for target in statement.targets
                if isinstance(target, ast.Name)
            ]
            if names:
                rendered_value = ast.unparse(statement.value)
                if len(rendered_value) <= 120:
                    declarations.append(f"    {' = '.join(names)} = {rendered_value}")
    initializer = next(
        (
            item
            for item in class_node.body
            if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
            and item.name in {"__init__", "__new__"}
        ),
        None,
    )
    if initializer is not None:
        signature = _python_function_signature(initializer)
        declarations.extend(f"    {line}" for line in signature.splitlines())
    if len(declarations) == 1:
        declarations.append("    ...")
    return "\n".join(declarations)


def _statement_bound_names(statement: ast.stmt) -> set[str]:
    if isinstance(statement, ast.Import):
        return {alias.asname or alias.name.split(".", 1)[0] for alias in statement.names}
    if isinstance(statement, ast.ImportFrom):
        return {alias.asname or alias.name for alias in statement.names}
    if isinstance(statement, ast.Assign):
        return {
            node.id
            for target in statement.targets
            for node in ast.walk(target)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        }
    if isinstance(statement, ast.AnnAssign):
        return {
            node.id
            for node in ast.walk(statement.target)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        }
    if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return {statement.name}
    return set()


def _parse_python_module_for_context(source: str) -> ast.Module | None:
    """Parse a module for declarations, masking bounded syntax-error regions if necessary."""
    lines = source.splitlines(keepends=True)
    candidate = list(lines)
    for _attempt in range(24):
        try:
            return ast.parse("".join(candidate))
        except SyntaxError as exc:
            if not isinstance(exc.lineno, int) or not 1 <= exc.lineno <= len(candidate):
                return None
            index = exc.lineno - 1
            original = candidate[index]
            stripped = original.lstrip(" \t")
            indent_text = original[: len(original) - len(stripped)]
            newline = "\r\n" if original.endswith("\r\n") else "\n"
            candidate[index] = f"{indent_text}pass{newline}"
            indent = len(indent_text.expandtabs(8))
            following = index + 1
            while following < len(candidate):
                line = candidate[following]
                content = line.lstrip(" \t")
                if not content.strip():
                    following += 1
                    continue
                child_indent = len(line[: len(line) - len(content)].expandtabs(8))
                if child_indent <= indent:
                    break
                child_newline = "\r\n" if line.endswith("\r\n") else "\n"
                candidate[following] = child_newline
                following += 1
    return None


def _build_python_module_context_index(source_text: str) -> dict[str, tuple[str, ...]]:
    module = _parse_python_module_for_context(source_text)
    if module is None:
        return {}
    declarations: dict[str, list[str]] = {}
    for statement in module.body:
        bound_names = _statement_bound_names(statement)
        if not bound_names:
            continue
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            declaration = _python_function_signature(statement)
        elif isinstance(statement, ast.ClassDef):
            declaration = _python_symbol_declaration(ast.unparse(statement), statement.name)
        else:
            declaration = ast.unparse(statement)
            if len(declaration) > 600:
                names = ", ".join(sorted(bound_names))
                declaration = f"# module declaration for {names} omitted after 600 characters"
        if not declaration:
            continue
        for name in bound_names:
            values = declarations.setdefault(name, [])
            if declaration not in values:
                values.append(declaration)
    return {name: tuple(values) for name, values in declarations.items()}


def _python_module_context_index(
    source_sha256: str,
    source_text: str,
) -> dict[str, tuple[str, ...]]:
    with _PYTHON_MODULE_CONTEXT_CACHE_LOCK:
        cached = _PYTHON_MODULE_CONTEXT_CACHE.get(source_sha256)
        if cached is not None:
            _PYTHON_MODULE_CONTEXT_CACHE.move_to_end(source_sha256)
            return cached
    built = _build_python_module_context_index(source_text)
    with _PYTHON_MODULE_CONTEXT_CACHE_LOCK:
        existing = _PYTHON_MODULE_CONTEXT_CACHE.get(source_sha256)
        if existing is not None:
            _PYTHON_MODULE_CONTEXT_CACHE.move_to_end(source_sha256)
            return existing
        _PYTHON_MODULE_CONTEXT_CACHE[source_sha256] = built
        while len(_PYTHON_MODULE_CONTEXT_CACHE) > _PYTHON_MODULE_CONTEXT_CACHE_MAX_FILES:
            _PYTHON_MODULE_CONTEXT_CACHE.popitem(last=False)
    return built


def _python_module_has_wildcard_import(source_text: str) -> bool:
    module = _parse_python_module_for_context(source_text)
    return bool(
        module is not None
        and any(
            isinstance(node, ast.ImportFrom)
            and any(alias.name == "*" for alias in node.names)
            for node in module.body
        )
    )


def _bounded_context(items: list[str], max_chars: int) -> str:
    selected: list[str] = []
    used = 0
    for item in items:
        cleaned = item.strip()
        if not cleaned:
            continue
        separator = 2 if selected else 0
        if used + separator + len(cleaned) > max_chars:
            continue
        selected.append(cleaned)
        used += separator + len(cleaned)
    return "\n\n".join(selected)


def _build_python_analysis_context(
    db: sqlite3.Connection,
    *,
    project_id: str,
    file_id: int,
    symbol_id: int,
    qualified_name: str,
    symbol_kind: str,
    task_source: str,
    source_text: str,
    source_sha256: str,
    max_chars: int | None = None,
) -> str:
    """Build a bounded, source-derived dependency slice for one Python function."""
    if FUNCTION_ANALYSIS_CONTEXT_CHARS <= 0:
        return ""
    referenced, direct_calls = _python_function_references(task_source, qualified_name)
    include_override_contracts = symbol_kind == "method"
    if not referenced and not direct_calls and not include_override_contracts:
        return ""
    module_items: list[str] = []
    seen_declarations: set[str] = set()
    included_names: set[str] = set()
    current_leaf = qualified_name.rsplit(".", 1)[-1]
    module_index = _python_module_context_index(source_sha256, source_text)
    for name in sorted(referenced):
        if name == current_leaf:
            continue
        for declaration in module_index.get(name, ()):
            if declaration not in seen_declarations:
                seen_declarations.add(declaration)
                module_items.append(declaration)
                included_names.add(name)

    symbol_items: list[str] = []
    resolved_rows = db.execute(
        """
        SELECT DISTINCT symbol.id, symbol.file_id, symbol.name, symbol.qualified_name,
               symbol.start_byte, symbol.end_byte, file.path, file.content
        FROM project_calls AS call
        JOIN project_symbols AS symbol ON symbol.id = call.resolved_symbol_id
        JOIN project_files AS file ON file.id = symbol.file_id
        WHERE call.project_id = ? AND call.caller_symbol_id = ?
          AND call.resolution_status = 'internal'
        ORDER BY file.path COLLATE NOCASE, symbol.start_byte, symbol.id
        LIMIT 40
        """,
        (project_id, symbol_id),
    ).fetchall()
    resolved_names = {str(row["name"]) for row in resolved_rows}
    candidate_names = sorted(
        ((referenced | direct_calls) - _BUILTIN_NAMES) - resolved_names
    )[:80]
    rows = list(resolved_rows)
    if include_override_contracts:
        rows.extend(
            db.execute(
                """
                SELECT symbol.id, symbol.file_id, symbol.name, symbol.qualified_name,
                       symbol.start_byte, symbol.end_byte, file.path, file.content
                FROM project_symbols AS symbol
                JOIN project_files AS file ON file.id = symbol.file_id
                WHERE symbol.project_id = ? AND symbol.id != ?
                  AND symbol.symbol_kind = 'method' AND symbol.name = ?
                ORDER BY file.path COLLATE NOCASE, symbol.start_byte, symbol.id
                LIMIT 24
                """,
                (project_id, symbol_id, current_leaf),
            ).fetchall()
        )
    if candidate_names:
        placeholders = ",".join("?" for _ in candidate_names)
        rows.extend(
            db.execute(
            f"""
            SELECT symbol.id, symbol.file_id, symbol.name, symbol.qualified_name,
                   symbol.start_byte, symbol.end_byte, file.path, file.content
            FROM project_symbols AS symbol
            JOIN project_files AS file ON file.id = symbol.file_id
            WHERE symbol.project_id = ? AND symbol.id != ?
              AND symbol.file_id = ?
              AND symbol.name IN ({placeholders})
            ORDER BY CASE WHEN symbol.file_id = ? THEN 0 ELSE 1 END,
                     file.path COLLATE NOCASE, symbol.start_byte, symbol.id
            LIMIT 40
            """,
            (project_id, symbol_id, file_id, *candidate_names, file_id),
            ).fetchall()
        )
    for row in rows:
        if int(row["file_id"]) == file_id and str(row["name"]) in included_names:
            continue
        if int(row["file_id"]) == file_id:
            text = source_text
        else:
            text, _encoding = decode_text_content(bytes(row["content"]))
            if text is None:
                continue
        encoded = text.encode("utf-8")
        start_byte = int(row["start_byte"])
        end_byte = int(row["end_byte"])
        if not 0 <= start_byte <= end_byte <= len(encoded):
            continue
        try:
            symbol_source = encoded[start_byte:end_byte].decode("utf-8")
        except UnicodeDecodeError:
            continue
        declaration = _python_symbol_declaration(
            symbol_source,
            str(row["qualified_name"]),
        )
        if not declaration:
            continue
        if declaration in seen_declarations:
            continue
        seen_declarations.add(declaration)
        symbol_items.append(f"# {row['path']}\n{declaration}")

    context_items: list[str] = []
    if module_items:
        context_items.append("# Referenced imports and module declarations")
        context_items.extend(module_items)
    if symbol_items:
        context_items.append("# Directly referenced project symbols")
        context_items.extend(symbol_items)
    return _bounded_context(context_items, FUNCTION_ANALYSIS_CONTEXT_CHARS if max_chars is None else max_chars)


def _contextual_cache_sha256(base_sha256: str, analysis_context: str) -> str:
    if not analysis_context:
        return base_sha256
    payload = (
        f"{_PYTHON_ANALYSIS_CONTEXT_VERSION}\0{base_sha256}\0{analysis_context}"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _semantic_analysis_context(analysis_context: str) -> str:
    """Ignore only target line offsets when looking up an issue-free semantic cache entry."""
    lines = []
    prefix = "# Source control-flow fact "
    for line in analysis_context.splitlines():
        if line.startswith(prefix):
            try:
                fact = json.loads(line[len(prefix):])
                fact.pop("relative_line", None)
                line = prefix + json.dumps(fact, ensure_ascii=True, separators=(",", ":"))
            except (TypeError, ValueError, AttributeError):
                pass
        lines.append(line)
    return "\n".join(lines)


def load_function_analysis_task(
    db: sqlite3.Connection,
    symbol_id: int,
    *,
    include_inferred: bool = False,
) -> FunctionAnalysisTask:
    row = db.execute(
        """
        SELECT symbol.id, symbol.project_id, symbol.file_id, symbol.symbol_kind,
               symbol.qualified_name, symbol.start_line, symbol.end_line,
               symbol.start_byte, symbol.end_byte, symbol.source_sha256,
               file.path, file.language, file.content, file.parser_source_sha256,
               project.user_id, project.main_file_path
        FROM project_symbols AS symbol
        JOIN project_files AS file ON file.id = symbol.file_id
        JOIN projects AS project ON project.id = symbol.project_id
        WHERE symbol.id = ? AND symbol.symbol_kind IN ('function', 'method')
        """,
        (symbol_id,),
    ).fetchone()
    if row is None:
        raise LookupError("Indexed function was not found")
    text, _encoding = decode_text_content(bytes(row["content"]))
    if text is None:
        raise StaleSymbolSource("Stored source can no longer be decoded")
    normalized = text.encode("utf-8")
    current_hash = hashlib.sha256(normalized).hexdigest()
    expected_hash = str(row["source_sha256"])
    parser_hash = str(row["parser_source_sha256"] or "")
    if current_hash != expected_hash or parser_hash != expected_hash:
        raise StaleSymbolSource("Stored source hash no longer matches the structural index")
    start_byte = int(row["start_byte"])
    end_byte = int(row["end_byte"])
    if start_byte < 0 or end_byte < start_byte or end_byte > len(normalized):
        raise StaleSymbolSource("Indexed function byte range is outside the stored source")
    try:
        source = normalized[start_byte:end_byte].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StaleSymbolSource("Indexed function range splits a UTF-8 character") from exc
    function_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    semantic_function_sha256 = _semantic_function_cache_sha256(
        str(row["language"]),
        source,
    )
    analysis_context = ""
    if FUNCTION_ANALYSIS_CONTEXT_CHARS > 0:
        context_items = resolved_context_items(
            db, str(row["project_id"]), symbol_id, include_inferred=include_inferred,
        )
        if str(row["language"]) != "python":
            context_items.extend(file_dependency_items(db, str(row["project_id"]), int(row["file_id"]), text))
        # Keep existing Python declarations usable by deterministic AST helpers.
        entry_point_context = (
            ["# Selected project entry point: " + json.dumps(row["main_file_path"])]
            if row["main_file_path"] else []
        )
        dependency_context = _bounded_context(context_items, FUNCTION_ANALYSIS_CONTEXT_CHARS // 2)
        flow_context = _bounded_context(
            python_control_flow_items(source, int(row["start_line"])) if str(row["language"]) == "python" else [],
            FUNCTION_ANALYSIS_CONTEXT_CHARS // 5,
        )
        priority_context = _bounded_context([*entry_point_context, dependency_context, flow_context], FUNCTION_ANALYSIS_CONTEXT_CHARS)
        remaining = max(0, FUNCTION_ANALYSIS_CONTEXT_CHARS - len(priority_context) - (2 if priority_context else 0))
        declarations = ""
        if str(row["language"]) == "python":
            declarations = _build_python_analysis_context(
                db, project_id=str(row["project_id"]), file_id=int(row["file_id"]), symbol_id=symbol_id,
                qualified_name=str(row["qualified_name"]), symbol_kind=str(row["symbol_kind"]),
                task_source=source, source_text=text,
                source_sha256=expected_hash, max_chars=remaining,
            )
        # Spend unused space on dependency items, retaining whole records.
        extras = [item for item in context_items if item not in dependency_context]
        analysis_context = _bounded_context([priority_context, declarations, *extras], FUNCTION_ANALYSIS_CONTEXT_CHARS)
    module_defined_names: tuple[str, ...] = ()
    has_wildcard_import = False
    cache_context = analysis_context
    if str(row["language"]) == "python":
        module_defined_names = tuple(
            sorted(
                name
                for name in _python_module_context_index(expected_hash, text)
                if name != "*"
            )
        )
        has_wildcard_import = _python_module_has_wildcard_import(text)
        binding_fingerprint = hashlib.sha256(
            json.dumps(
                [module_defined_names, has_wildcard_import],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        cache_context += f"\n# deterministic-module-bindings: {binding_fingerprint}"
    return FunctionAnalysisTask(
        symbol_id=int(row["id"]),
        project_id=str(row["project_id"]),
        user_id=int(row["user_id"]),
        file_id=int(row["file_id"]),
        file_path=str(row["path"]),
        language=str(row["language"]),
        symbol_kind=str(row["symbol_kind"]),
        qualified_name=str(row["qualified_name"]),
        start_line=int(row["start_line"]),
        end_line=int(row["end_line"]),
        source_sha256=expected_hash,
        function_sha256=function_sha256,
        source=source,
        semantic_function_sha256=semantic_function_sha256,
        analysis_context=analysis_context,
        include_inferred_context=include_inferred,
        cache_function_sha256=_contextual_cache_sha256(
            function_sha256,
            cache_context,
        ),
        semantic_cache_function_sha256=_contextual_cache_sha256(
            semantic_function_sha256,
            _semantic_analysis_context(cache_context),
        ),
        enclosing_scope_names=(
            _python_enclosing_scope_names(
                text,
                str(row["qualified_name"]),
                int(row["start_line"]),
            )
            if str(row["language"]) == "python"
            else ()
        ),
        module_defined_names=module_defined_names,
        has_wildcard_import=has_wildcard_import,
    )


def filter_source_proven_false_issues(
    db: sqlite3.Connection,
    task: FunctionAnalysisTask,
    result: analysis_engine.FunctionAnalysisResult,
) -> analysis_engine.FunctionAnalysisResult:
    """Apply universal proof checks, then language-specific contradiction checks."""
    row = db.execute(
        """
        SELECT content, parser_status, parser_diagnostics_json, structure_status
        FROM project_files WHERE id = ?
        """,
        (task.file_id,),
    ).fetchone()
    source_text = task.source
    parser_status = ""
    structure_status = ""
    diagnostics: list[dict[str, object]] = []
    if row is not None:
        decoded, _encoding = decode_text_content(bytes(row["content"]))
        if decoded is not None:
            source_text = decoded
        parser_status = str(row["parser_status"] or "")
        structure_status = str(row["structure_status"] or "")
        diagnostics = _parser_diagnostics(row["parser_diagnostics_json"])

    parser_issues = _parser_syntax_issues(task, diagnostics)
    rule_issues = [analysis_engine.FunctionIssue(**item) for item in javascript_null_member_issues(task.source, task.language, task.start_line)]
    candidate_issues = [*result.issues, *parser_issues, *rule_issues]
    kept: list[analysis_engine.FunctionIssue] = []
    changed = bool(parser_issues or rule_issues)
    for issue in candidate_issues:
        verified = _verified_model_issue(
            db,
            task,
            issue,
            structure_status=structure_status,
            diagnostics=diagnostics,
        )
        if verified is None:
            changed = True
            continue
        if verified is not issue:
            changed = True
            issue = verified
        text = f"{issue.title}\n{issue.description}"
        if _issue_is_speculative_model_noise(issue):
            changed = True
            continue
        if _model_variable_flow_claim_is_contradicted(task, issue):
            changed = True
            continue
        if (
            task.language == "python"
            and _issue_is_source_proven_noise(issue, task, result, source_text)
        ):
            changed = True
            continue
        if (
            task.language == "python"
            and
            issue.severity in {"error", "warning", "unsafe"}
            and _UNDEFINED_ISSUE_PATTERN.search(text)
        ):
            names = _issue_referenced_names(issue)
            resolves = (
                names
                and all(
                    _python_source_resolves_name(source_text, task, name)
                    for name in names
                )
            )
            if resolves and (
                issue.provenance in {"model", "cache"}
                or issue.failure_type == "NameError"
            ):
                changed = True
                continue
        normalized = _normalize_verified_model_severity(issue, diagnostics)
        if normalized is not issue:
            changed = True
            issue = normalized
        demoted = _demote_unanchored_model_error(issue)
        if demoted is not issue:
            changed = True
            issue = demoted
        kept.append(issue)
    kept, deduplicated = _deduplicate_overlapping_issues(kept)
    changed = changed or deduplicated
    syntax_valid = result.syntax_valid
    if parser_status in {"parsed", "syntax_error"}:
        syntax_valid = not bool(parser_issues)
        changed = changed or syntax_valid != result.syntax_valid
    updates: dict[str, object] = {"issues": kept, "syntax_valid": syntax_valid}
    notes = list(result.validation_notes)
    original_model_count = sum(issue.provenance in {"model", "cache"} for issue in result.issues)
    kept_model_count = sum(issue.provenance in {"model", "cache"} for issue in kept)
    if original_model_count > kept_model_count:
        notes.append(f"Excluded {original_model_count - kept_model_count} model finding(s) after source verification and deduplication.")
        changed = True
    if result.analysis_method == "model" and syntax_valid != result.syntax_valid:
        notes.extend([
            "The local parser contradicted the model's syntax assessment; semantic review is incomplete.",
            "Unverified model summary: " + result.summary,
        ])
        updates.update(
            summary="Local parsing contradicted the model's syntax assessment. The behavior summary requires a fresh review.",
            review_status="partial", confidence=min(result.confidence, 0.5),
        )
    updates["validation_notes"] = notes[:100]
    return (
        result.model_copy(update=updates)
        if changed
        else result
    )


def split_function_source(
    source: str,
    start_line: int,
    *,
    chunk_chars: int,
) -> list[FunctionSourceChunk]:
    """Split source into contiguous bounded fragments while retaining file line numbers."""
    if chunk_chars < 1:
        raise ValueError("Function chunk size must be positive")
    raw_chunks: list[tuple[int, int, str]] = []
    buffer = ""
    buffer_start = start_line
    buffer_end = start_line
    lines = source.splitlines(keepends=True) or [source]
    for offset, original_line in enumerate(lines):
        absolute_line = start_line + offset
        remaining = original_line
        if not remaining and not buffer:
            buffer_start = absolute_line
            buffer_end = absolute_line
        while remaining:
            if not buffer:
                buffer_start = absolute_line
            room = chunk_chars - len(buffer)
            take = remaining[:room]
            buffer += take
            buffer_end = absolute_line
            remaining = remaining[len(take) :]
            if len(buffer) == chunk_chars:
                raw_chunks.append((buffer_start, buffer_end, buffer))
                buffer = ""
    if buffer or not raw_chunks:
        raw_chunks.append((buffer_start, buffer_end, buffer))
    total = len(raw_chunks)
    return [
        FunctionSourceChunk(index, total, chunk_start, chunk_end, chunk_source)
        for index, (chunk_start, chunk_end, chunk_source) in enumerate(raw_chunks, 1)
    ]


def _unique_strings(values: list[str], limit: int) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)
        if len(unique) == limit:
            break
    return unique


def _annotation_text(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.strip()
    try:
        return ast.unparse(node)
    except Exception:
        return None


def _parseable_python_fragment(source: str) -> str:
    """Normalize extracted fragments whose decorator and def indentation differ."""
    dedented = textwrap.dedent(source)
    lines = dedented.splitlines()
    definition_indent: int | None = None
    for line in lines:
        stripped = line.lstrip(" \t")
        if stripped.startswith(("def ", "async def ", "class ")):
            definition_indent = len(line) - len(stripped)
            break
    if not definition_indent:
        return dedented
    normalized: list[str] = []
    for line in lines:
        stripped = line.lstrip(" \t")
        indent = len(line) - len(stripped)
        if stripped and indent >= definition_indent:
            normalized.append(line[definition_indent:])
        else:
            normalized.append(line)
    return "\n".join(normalized)


def _literal_return_type(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant):
        if node.value is None:
            return "None"
        if isinstance(node.value, bool):
            return "bool"
        if isinstance(node.value, int):
            return "int"
        if isinstance(node.value, float):
            return "float"
        if isinstance(node.value, str):
            return "str"
    if isinstance(node, ast.List | ast.ListComp):
        return "list"
    if isinstance(node, ast.Dict | ast.DictComp):
        return "dict"
    if isinstance(node, ast.Set | ast.SetComp):
        return "set"
    if isinstance(node, ast.Tuple):
        return "tuple"
    if isinstance(node, ast.GeneratorExp):
        return "generator"
    if isinstance(node, ast.JoinedStr):
        return "str"
    return None


_STATIC_RETURN_CALL_TYPES = {
    "bool": "bool", "bytearray": "bytearray", "bytes": "bytes",
    "complex": "complex", "dict": "dict", "float": "float",
    "frozenset": "frozenset", "int": "int", "list": "list",
    "len": "int", "set": "set", "str": "str", "tuple": "tuple",
}
_STATIC_RETURN_CONTRACT_TYPES = {
    "bool", "bytearray", "bytes", "complex", "dict", "float",
    "frozenset", "int", "list", "null", "set", "string", "tuple",
}


def _python_return_shadowed_names(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    analysis_context: str,
) -> set[str]:
    shadowed_names = _python_scope_bindings(function)[0]
    try:
        context_module = ast.parse(analysis_context)
    except SyntaxError:
        # Without a parseable module slice, a global binding cannot be ruled out.
        shadowed_names.update(_STATIC_RETURN_CALL_TYPES)
    else:
        for statement in context_module.body:
            shadowed_names.update(_statement_bound_names(statement))
    return shadowed_names


def _static_python_return_types(
    node: ast.AST,
    parameters: list[analysis_engine.FunctionParameterContract],
    task: FunctionAnalysisTask,
    *,
    shadowed_names: set[str] | None = None,
) -> list[str] | None:
    """Infer only return-expression types established by local syntax/contracts."""
    literal = _literal_return_type(node)
    if literal:
        return [literal]
    parameter_by_name = {parameter.name: parameter for parameter in parameters}
    if isinstance(node, ast.Name):
        if node.id == "self" and task.symbol_kind == "method":
            return [task.qualified_name.rsplit(".", 1)[0].rsplit(".", 1)[-1]]
        if node.id == "cls" and task.symbol_kind == "method":
            class_name = task.qualified_name.rsplit(".", 1)[0].rsplit(".", 1)[-1]
            return [f"type[{class_name}]"]
        parameter = parameter_by_name.get(node.id)
        if parameter is not None and not any(
            re.fullmatch(r"(?:typing\.)?(?:any|object|unknown|dynamic)", value.strip(), re.I)
            for value in parameter.accepted_types
        ):
            return list(parameter.accepted_types) or None
        return None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if (node.func.id in _STATIC_RETURN_CALL_TYPES
                and node.func.id not in (shadowed_names or set())):
            return [_STATIC_RETURN_CALL_TYPES[node.func.id]]
        return None
    if isinstance(node, ast.IfExp):
        branches = [
            _static_python_return_types(part, parameters, task, shadowed_names=shadowed_names)
            for part in (node.body, node.orelse)
        ]
        return (
            _unique_strings([item for branch in branches if branch for item in branch], 12)
            if all(branches) else None
        )
    if isinstance(node, ast.BoolOp):
        operands = [
            _static_python_return_types(part, parameters, task, shadowed_names=shadowed_names)
            for part in node.values
        ]
        return (
            _unique_strings([item for operand in operands if operand for item in operand], 12)
            if all(operands) else None
        )
    if isinstance(node, ast.Compare) or (
        isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)
    ):
        return ["bool"]
    if isinstance(node, ast.UnaryOp):
        return _static_python_return_types(
            node.operand, parameters, task, shadowed_names=shadowed_names
        )
    return None


def _python_statements_guarantee_exit(statements: list[ast.stmt]) -> bool:
    """Conservatively prove that an ordinary Python body cannot fall through."""
    for statement in statements:
        if isinstance(statement, ast.Return | ast.Raise):
            return True
        if isinstance(statement, ast.If) and statement.orelse:
            if (
                _python_statements_guarantee_exit(statement.body)
                and _python_statements_guarantee_exit(statement.orelse)
            ):
                return True
    return False


def _decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _decorator_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return ""


def _has_decorator(function: ast.FunctionDef | ast.AsyncFunctionDef, names: set[str]) -> bool:
    return any(_decorator_name(decorator) in names for decorator in function.decorator_list)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _python_context_signatures(
    analysis_context: str,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return only unambiguous callable signatures from indexed project context."""
    if not analysis_context.strip():
        return {}
    try:
        module = ast.parse(analysis_context)
    except SyntaxError:
        return {}
    candidates: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for node in module.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            candidates.setdefault(node.name, []).append(node)
    return {
        name: values[0]
        for name, values in candidates.items()
        if len(values) == 1
    }


def _python_local_signatures(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        statement.name: statement
        for statement in function.body
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _python_call_contract_problems(
    call: ast.Call,
    signature: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[tuple[str, str]]:
    """Prove Python call-binding failures from one indexed source signature."""
    allowed_decorators = {"staticmethod", "classmethod"}
    decorators = {_decorator_name(item).rsplit(".", 1)[-1] for item in signature.decorator_list}
    if decorators - allowed_decorators:
        return []
    if any(isinstance(argument, ast.Starred) for argument in call.args):
        return []
    if any(keyword.arg is None for keyword in call.keywords):
        return []

    positional_nodes = [*signature.args.posonlyargs, *signature.args.args]
    positional_default_start = len(positional_nodes) - len(signature.args.defaults)
    positional: list[tuple[str, bool, bool]] = [
        (argument.arg, index < positional_default_start, index < len(signature.args.posonlyargs))
        for index, argument in enumerate(positional_nodes)
    ]
    if (
        positional
        and positional[0][0] in {"self", "cls"}
        and (isinstance(call.func, ast.Attribute) or positional[0][0] == "cls")
    ):
        positional = positional[1:]

    keyword_only = {
        argument.arg: default is None
        for argument, default in zip(signature.args.kwonlyargs, signature.args.kw_defaults)
    }
    positional_by_name = {name: (required, positional_only) for name, required, positional_only in positional}
    assigned: set[str] = set()
    problems: list[tuple[str, str]] = []
    for index, _argument in enumerate(call.args):
        if index < len(positional):
            assigned.add(positional[index][0])
        elif signature.args.vararg is None:
            problems.append(
                (
                    "Too many positional arguments",
                    "The indexed callee signature accepts fewer positional arguments.",
                )
            )
            break

    for keyword in call.keywords:
        name = str(keyword.arg)
        positional_contract = positional_by_name.get(name)
        if positional_contract is not None and not positional_contract[1]:
            if name in assigned:
                problems.append(
                    (
                        "Duplicate call argument",
                        f"Argument `{name}` is supplied both positionally and by keyword.",
                    )
                )
            assigned.add(name)
        elif name in keyword_only:
            if name in assigned:
                problems.append(
                    (
                        "Duplicate call argument",
                        f"Argument `{name}` is supplied more than once.",
                    )
                )
            assigned.add(name)
        elif signature.args.kwarg is None:
            problems.append(
                (
                    "Unexpected keyword argument",
                    f"The indexed callee signature has no keyword parameter named `{name}`.",
                )
            )

    missing = [
        name
        for name, required, _positional_only in positional
        if required and name not in assigned
    ]
    missing.extend(
        name for name, required in keyword_only.items() if required and name not in assigned
    )
    if missing:
        rendered = ", ".join(f"`{name}`" for name in missing)
        problems.append(
            (
                "Missing required call arguments",
                f"The indexed callee signature requires {rendered}.",
            )
        )
    return problems


def _is_annotation_node(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    child = node
    parent = parents.get(child)
    while parent is not None:
        if isinstance(parent, ast.arg) and parent.annotation is child:
            return True
        if isinstance(parent, ast.AnnAssign) and parent.annotation is child:
            return True
        if isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef) and parent.returns is child:
            return True
        child = parent
        parent = parents.get(parent)
    return False


def _enclosing_statement(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.stmt | None:
    current: ast.AST | None = node
    while current is not None and not isinstance(current, ast.stmt):
        current = parents.get(current)
    return current if isinstance(current, ast.stmt) else None


def _node_contains(container: ast.AST, target: ast.AST) -> bool:
    return any(child is target for child in ast.walk(container))


def _test_proves_name_available(test: ast.AST, name: str, target: ast.AST) -> bool:
    if isinstance(test, ast.Call) and _call_name(test.func) == "isinstance":
        return bool(test.args and isinstance(test.args[0], ast.Name) and test.args[0].id == name)
    if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == name:
        for operator, comparator in zip(test.ops, test.comparators):
            if isinstance(comparator, ast.Constant) and comparator.value is None:
                if isinstance(operator, ast.IsNot):
                    return True
    if isinstance(test, ast.Name) and test.id == name:
        return True
    if isinstance(test, ast.BoolOp):
        values = list(test.values)
        if isinstance(test.op, ast.And):
            for value in values:
                if value is target or _node_contains(value, target):
                    break
                if _test_proves_name_available(value, name, target):
                    return True
            return any(_test_proves_name_available(value, name, target) for value in values)
        if isinstance(test.op, ast.Or):
            for value in values:
                if value is target or _node_contains(value, target):
                    break
                if _test_rejects_none(value, name):
                    return True
    return False


def _test_rejects_none(test: ast.AST, name: str) -> bool:
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and isinstance(test.operand, ast.Name):
        return test.operand.id == name
    if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == name:
        for operator, comparator in zip(test.ops, test.comparators):
            if isinstance(comparator, ast.Constant) and comparator.value is None:
                if isinstance(operator, ast.Is):
                    return True
    return False


def _statement_always_exits(statement: ast.stmt) -> bool:
    if isinstance(statement, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
        return True
    if isinstance(statement, ast.If):
        return bool(statement.body and statement.orelse) and all(
            _statement_always_exits(item) for item in (*statement.body, *statement.orelse)
        )
    if isinstance(statement, ast.With | ast.AsyncWith):
        return bool(statement.body) and all(_statement_always_exits(item) for item in statement.body)
    return False


def _name_is_guarded_before_statement(
    body: list[ast.stmt],
    statement: ast.stmt,
    name: str,
) -> bool:
    for item in body:
        if item is statement:
            return False
        if isinstance(item, ast.If) and _test_rejects_none(item.test, name):
            if item.body and all(_statement_always_exits(child) for child in item.body):
                return True
        if isinstance(item, ast.If) and _test_proves_name_available(item.test, name, item.test):
            if item.orelse and all(_statement_always_exits(child) for child in item.orelse):
                return True
    return False


def _attribute_access_is_guarded(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    node: ast.Attribute,
    name: str,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    parent = parents.get(node)
    while parent is not None:
        if isinstance(parent, ast.BoolOp) and _test_proves_name_available(
            parent, name, node
        ):
            return True
        if isinstance(parent, ast.IfExp) and _node_contains(parent.body, node):
            if _test_proves_name_available(parent.test, name, node):
                return True
        if isinstance(parent, ast.If) and node in ast.walk(parent.test):
            if _test_proves_name_available(parent.test, name, node):
                return True
        if isinstance(parent, ast.If) and any(_node_contains(child, node) for child in parent.body):
            if _test_proves_name_available(parent.test, name, node):
                return True
        parent = parents.get(parent)

    statement = _enclosing_statement(node, parents)
    if statement is None:
        return False
    owner = parents.get(statement)
    while owner is not None:
        body = getattr(owner, "body", None)
        if isinstance(body, list) and _name_is_guarded_before_statement(body, statement, name):
            return True
        statement = owner if isinstance(owner, ast.stmt) else statement
        owner = parents.get(owner)
    return _name_is_guarded_before_statement(function.body, statement, name)


def _expression_is_definitely_non_none(node: ast.AST) -> bool:
    return (
        isinstance(
            node,
            ast.Dict | ast.List | ast.Set | ast.Tuple | ast.Lambda | ast.JoinedStr,
        )
        or isinstance(node, ast.Constant) and node.value is not None
        or isinstance(node, ast.Attribute | ast.Call)
    )


def _assignment_normalizes_optional_name(statement: ast.stmt, name: str) -> bool:
    value: ast.AST | None = None
    if isinstance(statement, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == name
        for target in statement.targets
    ):
        value = statement.value
    elif (
        isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
        and statement.target.id == name
    ):
        value = statement.value
    if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or):
        return bool(value.values) and _expression_is_definitely_non_none(value.values[-1])
    if (
        isinstance(value, ast.IfExp)
        and _test_rejects_none(value.test, name)
        and isinstance(value.orelse, ast.Name)
        and value.orelse.id == name
    ):
        return _expression_is_definitely_non_none(value.body)
    return False


def _optional_name_is_normalized_before_access(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    node: ast.Attribute,
    name: str,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    statement = _enclosing_statement(node, parents)
    if statement is None:
        return False
    child: ast.AST = statement
    owner = parents.get(child)
    while owner is not None:
        body = getattr(owner, "body", None)
        if isinstance(body, list):
            containing = next(
                (
                    item
                    for item in body
                    if item is child or _node_contains(item, child)
                ),
                None,
            )
            if containing is not None and any(
                _assignment_normalizes_optional_name(item, name)
                for item in body[: body.index(containing)]
            ):
                return True
        child = owner
        owner = parents.get(owner)
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            break
    return any(
        _assignment_normalizes_optional_name(item, name)
        for item in function.body
        if int(getattr(item, "lineno", 0) or 0)
        < int(getattr(node, "lineno", 0) or 0)
    )


def _assignment_values(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, list[ast.AST]]:
    values: dict[str, list[ast.AST]] = {}
    for node in _python_function_scope_nodes(function):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            values.setdefault(node.target.id, []).append(node.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"append", "add"}
            and isinstance(node.func.value, ast.Name)
            and len(node.args) == 1
        ):
            values.setdefault(node.func.value.id, []).append(node.args[0])
    return values


class _PythonCurrentScopeVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.nodes: list[ast.AST] = []

    def generic_visit(self, node: ast.AST) -> None:
        self.nodes.append(node)
        super().generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.nodes.append(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.nodes.append(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.nodes.append(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.nodes.append(node)


def _python_function_scope_nodes(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[ast.AST, ...]:
    visitor = _PythonCurrentScopeVisitor()
    for statement in function.body:
        visitor.visit(statement)
    return tuple(visitor.nodes)


def _sql_fragment_is_static(
    node: ast.AST,
    assignments: dict[str, list[ast.AST]],
    seen: set[str] | None = None,
) -> bool:
    """Recognize SQL structure whose output characters are all source constants."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str | int | float | bool | type(None))
    if isinstance(node, ast.FormattedValue):
        return _sql_fragment_is_static(node.value, assignments, seen)
    if isinstance(node, ast.JoinedStr):
        return all(_sql_fragment_is_static(value, assignments, seen) for value in node.values)
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        return all(_sql_fragment_is_static(value, assignments, seen) for value in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            key is not None
            and _sql_fragment_is_static(key, assignments, seen)
            and _sql_fragment_is_static(value, assignments, seen)
            for key, value in zip(node.keys, node.values)
        )
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        values = assignments.get(node.value.id, [])
        return len(values) == 1 and isinstance(values[0], ast.Dict) and all(
            _sql_fragment_is_static(value, assignments, seen)
            for value in values[0].values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _sql_fragment_is_static(node.left, assignments, seen) and _sql_fragment_is_static(
            node.right, assignments, seen
        )
    if isinstance(node, ast.IfExp):
        return _sql_fragment_is_static(node.body, assignments, seen) and _sql_fragment_is_static(
            node.orelse, assignments, seen
        )
    if isinstance(node, ast.Name):
        visited = set(seen or ())
        if node.id in visited:
            return False
        values = assignments.get(node.id, [])
        if not values:
            return False
        visited.add(node.id)
        return all(_sql_fragment_is_static(value, assignments, visited) for value in values)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and _sql_fragment_is_static(node.func.value, assignments, seen)
        and len(node.args) == 1
        and not node.keywords
    ):
        iterable = node.args[0]
        if isinstance(iterable, ast.GeneratorExp | ast.ListComp | ast.SetComp):
            return _sql_fragment_is_static(iterable.elt, assignments, seen)
        if isinstance(iterable, ast.Tuple | ast.List | ast.Set):
            return all(_sql_fragment_is_static(item, assignments, seen) for item in iterable.elts)
    return False


def _expression_depends_on_parameters(
    node: ast.AST,
    parameter_names: set[str],
    assignments: dict[str, list[ast.AST]],
    seen: set[str] | None = None,
) -> bool:
    if isinstance(node, ast.Call):
        name = _call_name(node.func)
        if name not in {"str", "bytes", "repr", "format"}:
            # An indexed helper may validate or constrain the value. Its own body is analysed
            # separately, so do not assume taint crosses an arbitrary call boundary.
            return False
    if isinstance(node, ast.Name):
        if node.id in parameter_names:
            return True
        visited = set(seen or ())
        if node.id in visited:
            return False
        visited.add(node.id)
        return any(
            _expression_depends_on_parameters(value, parameter_names, assignments, visited)
            for value in assignments.get(node.id, [])
        )
    return any(
        _expression_depends_on_parameters(child, parameter_names, assignments, seen)
        for child in ast.iter_child_nodes(node)
    )


def _sql_text_capable_parameters(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> set[str]:
    """Return parameters whose contract can carry attacker-controlled SQL characters."""
    result: set[str] = set()
    for argument in (
        *function.args.posonlyargs,
        *function.args.args,
        *function.args.kwonlyargs,
    ):
        annotation = re.sub(
            r"\s+",
            "",
            _annotation_text(argument.annotation) or "",
        ).casefold()
        safe_scalar = bool(annotation) and all(
            part in {"int", "float", "bool", "none"}
            for part in annotation.replace("optional[", "").replace("]", "").split("|")
        )
        if not safe_scalar:
            result.add(argument.arg)
    if function.args.vararg is not None:
        result.add(function.args.vararg.arg)
    if function.args.kwarg is not None:
        result.add(function.args.kwarg.arg)
    return result


def _formatted_value_is_quoted(node: ast.JoinedStr, index: int) -> bool:
    before = node.values[index - 1] if index > 0 else None
    after = node.values[index + 1] if index + 1 < len(node.values) else None
    before_text = before.value if isinstance(before, ast.Constant) and isinstance(before.value, str) else ""
    after_text = after.value if isinstance(after, ast.Constant) and isinstance(after.value, str) else ""
    return before_text.endswith(("'", '"')) and after_text.startswith(("'", '"'))


def _sql_fstring_is_suspicious(
    node: ast.JoinedStr,
    assignments: dict[str, list[ast.AST]],
    parameter_names: set[str],
) -> bool:
    for index, value in enumerate(node.values):
        if not isinstance(value, ast.FormattedValue):
            continue
        if _sql_fragment_is_static(value.value, assignments):
            continue
        if _formatted_value_is_quoted(node, index) or _expression_depends_on_parameters(
            value.value,
            parameter_names,
            assignments,
        ):
            return True
    return False


_PYTHON_PREDEFINED_GLOBAL_NAMES = {
    "__annotations__",
    "__builtins__",
    "__cached__",
    "__debug__",
    "__doc__",
    "__file__",
    "__loader__",
    "__name__",
    "__package__",
    "__spec__",
}


def _target_names(target: ast.AST | None) -> set[str]:
    if target is None:
        return set()
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, ast.Tuple | ast.List):
        return {
            name
            for item in target.elts
            for name in _target_names(item)
        }
    return set()


def _pattern_names(pattern: ast.pattern | None) -> set[str]:
    if pattern is None:
        return set()
    names: set[str] = set()
    for node in ast.walk(pattern):
        if isinstance(node, ast.MatchAs) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchStar) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
    return names


class _PythonScopeBindingsVisitor(ast.NodeVisitor):
    """Collect compiler-local bindings without crossing a nested Python scope."""

    def __init__(self) -> None:
        self.bound: set[str] = set()
        self.global_names: set[str] = set()
        self.nonlocal_names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store | ast.Del):
            self.bound.add(node.id)

    def visit_Global(self, node: ast.Global) -> None:
        self.global_names.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.nonlocal_names.update(node.names)

    def visit_Import(self, node: ast.Import) -> None:
        self.bound.update(
            alias.asname or alias.name.split(".", 1)[0]
            for alias in node.names
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.bound.update(alias.asname or alias.name for alias in node.names)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.bound.add(node.name)
        if node.type is not None:
            self.visit(node.type)
        for statement in node.body:
            self.visit(statement)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        for case in node.cases:
            self.bound.update(_pattern_names(case.pattern))
            if case.guard is not None:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)

    def _visit_definition_expressions(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self.bound.add(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        for argument in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ):
            if argument.annotation is not None:
                self.visit(argument.annotation)
        if node.args.vararg is not None and node.args.vararg.annotation is not None:
            self.visit(node.args.vararg.annotation)
        if node.args.kwarg is not None and node.args.kwarg.annotation is not None:
            self.visit(node.args.kwarg.annotation)
        if node.returns is not None:
            self.visit(node.returns)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_definition_expressions(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_definition_expressions(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.bound.add(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)

    def _visit_comprehension(self, node: ast.AST) -> None:
        generators = getattr(node, "generators", ())
        for generator in generators:
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_DictComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension


def _python_scope_bindings(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[set[str], set[str], set[str]]:
    visitor = _PythonScopeBindingsVisitor()
    for statement in function.body:
        visitor.visit(statement)
    parameters = {
        argument.arg
        for argument in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        )
    }
    if function.args.vararg is not None:
        parameters.add(function.args.vararg.arg)
    if function.args.kwarg is not None:
        parameters.add(function.args.kwarg.arg)
    locals_ = (visitor.bound | parameters) - visitor.global_names - visitor.nonlocal_names
    return locals_, visitor.global_names, visitor.nonlocal_names


def _truthy_named_expression_targets(node: ast.AST) -> set[str]:
    """Return assignment-expression targets guaranteed when an expression is truthy."""
    if isinstance(node, ast.NamedExpr):
        return _target_names(node.target) | _truthy_named_expression_targets(node.value)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return set().union(
                *(_truthy_named_expression_targets(value) for value in node.values)
            )
        return set()
    if isinstance(node, ast.IfExp):
        return set()
    return set().union(
        *(_truthy_named_expression_targets(child) for child in ast.iter_child_nodes(node)),
        set(),
    )


class _PythonDefiniteAssignmentAnalyzer:
    """Forward, scope-aware definite-assignment analysis for one Python function."""

    def __init__(
        self,
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        known_external: set[str],
    ) -> None:
        self.function = function
        self.local_names, self.global_names, self.nonlocal_names = _python_scope_bindings(
            function
        )
        self.known_external = known_external | self.nonlocal_names
        self.failures: list[tuple[ast.Name, str, str]] = []
        self._seen: set[tuple[str, int, int, str]] = set()
        self._break_state_stack: list[list[set[str]]] = []

    def analyze(self) -> list[tuple[ast.Name, str, str]]:
        initial = {
            argument.arg
            for argument in (
                *self.function.args.posonlyargs,
                *self.function.args.args,
                *self.function.args.kwonlyargs,
            )
        }
        if self.function.args.vararg is not None:
            initial.add(self.function.args.vararg.arg)
        if self.function.args.kwarg is not None:
            initial.add(self.function.args.kwarg.arg)
        self._statements(self.function.body, initial)
        return self.failures

    def _record_load(self, node: ast.Name, state: set[str]) -> None:
        name = node.id
        if name in self.local_names:
            if name in state:
                return
            failure_type = "UnboundLocalError"
            description = f"Local name `{name}` is read on a path where it has not been assigned."
        elif name in self.known_external or name in _BUILTIN_NAMES:
            return
        else:
            failure_type = "NameError"
            description = f"Name `{name}` is read but no definition is visible in the source context."
        key = (
            name,
            int(getattr(node, "lineno", 0) or 0),
            int(getattr(node, "col_offset", 0) or 0),
            failure_type,
        )
        if key not in self._seen:
            self._seen.add(key)
            self.failures.append((node, failure_type, description))

    def _expression(
        self,
        node: ast.AST | None,
        state: set[str],
        *,
        shadowed: set[str] | None = None,
    ) -> set[str]:
        if node is None:
            return set(state)
        shadowed = shadowed or set()
        current = set(state)
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load) and node.id not in shadowed:
                self._record_load(node, current)
            return current
        if isinstance(node, ast.NamedExpr):
            current = self._expression(node.value, current, shadowed=shadowed)
            current.update(_target_names(node.target))
            return current
        if isinstance(node, ast.Lambda):
            for default in (*node.args.defaults, *node.args.kw_defaults):
                current = self._expression(default, current, shadowed=shadowed)
            return current
        if isinstance(node, ast.IfExp):
            tested = self._expression(node.test, current, shadowed=shadowed)
            body_state = self._expression(node.body, tested, shadowed=shadowed)
            else_state = self._expression(node.orelse, tested, shadowed=shadowed)
            return body_state & else_state
        if isinstance(node, ast.BoolOp):
            if not node.values:
                return current
            first_state = self._expression(node.values[0], current, shadowed=shadowed)
            running = set(first_state)
            for value in node.values[1:]:
                running = self._expression(value, running, shadowed=shadowed)
            return first_state
        if isinstance(node, ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp):
            comprehension_state = set(current)
            comprehension_names = set(shadowed)
            for generator in node.generators:
                comprehension_state = self._expression(
                    generator.iter,
                    comprehension_state,
                    shadowed=comprehension_names,
                )
                bound = _target_names(generator.target)
                comprehension_state.update(bound)
                comprehension_names.update(bound)
                for condition in generator.ifs:
                    comprehension_state = self._expression(
                        condition,
                        comprehension_state,
                        shadowed=comprehension_names,
                    )
                    comprehension_state.update(
                        _truthy_named_expression_targets(condition)
                    )
            if isinstance(node, ast.DictComp):
                self._expression(node.key, comprehension_state, shadowed=comprehension_names)
                self._expression(node.value, comprehension_state, shadowed=comprehension_names)
            else:
                self._expression(node.elt, comprehension_state, shadowed=comprehension_names)
            return current
        for child in ast.iter_child_nodes(node):
            current = self._expression(child, current, shadowed=shadowed)
        return current

    def _target(self, target: ast.AST, state: set[str], *, delete: bool = False) -> set[str]:
        current = set(state)
        if isinstance(target, ast.Name):
            if delete:
                self._record_load(target, current)
                current.discard(target.id)
            else:
                current.add(target.id)
            return current
        if isinstance(target, ast.Starred):
            return self._target(target.value, current, delete=delete)
        if isinstance(target, ast.Tuple | ast.List):
            for item in target.elts:
                current = self._target(item, current, delete=delete)
            return current
        if isinstance(target, ast.Attribute):
            return self._expression(target.value, current)
        if isinstance(target, ast.Subscript):
            current = self._expression(target.value, current)
            return self._expression(target.slice, current)
        return self._expression(target, current)

    @staticmethod
    def _merge(
        branches: list[tuple[set[str], bool]],
    ) -> tuple[set[str], bool]:
        continuing = [state for state, falls_through in branches if falls_through]
        if not continuing:
            terminal = [state for state, _falls_through in branches]
            return (set.intersection(*terminal) if terminal else set()), False
        return set.intersection(*continuing), True

    def _statements(
        self,
        statements: list[ast.stmt],
        state: set[str],
    ) -> tuple[set[str], bool]:
        current = set(state)
        falls_through = True
        for statement in statements:
            if not falls_through:
                break
            current, falls_through = self._statement(statement, current)
        return current, falls_through

    def _definition_expressions(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        state: set[str],
    ) -> set[str]:
        current = set(state)
        for decorator in node.decorator_list:
            current = self._expression(decorator, current)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            current = self._expression(default, current)
        return current

    def _statement(self, statement: ast.stmt, state: set[str]) -> tuple[set[str], bool]:
        current = set(state)
        if isinstance(statement, ast.Expr):
            return self._expression(statement.value, current), True
        if isinstance(statement, ast.Assign):
            current = self._expression(statement.value, current)
            for target in statement.targets:
                current = self._target(target, current)
            return current, True
        if isinstance(statement, ast.AnnAssign):
            if statement.value is not None:
                current = self._expression(statement.value, current)
                current = self._target(statement.target, current)
            elif not isinstance(statement.target, ast.Name):
                current = self._target(statement.target, current)
            return current, True
        if isinstance(statement, ast.AugAssign):
            if isinstance(statement.target, ast.Name):
                self._record_load(statement.target, current)
            else:
                current = self._target(statement.target, current)
            current = self._expression(statement.value, current)
            return self._target(statement.target, current), True
        if isinstance(statement, ast.Delete):
            for target in statement.targets:
                current = self._target(target, current, delete=True)
            return current, True
        if isinstance(statement, ast.If):
            tested = self._expression(statement.test, current)
            body = self._statements(statement.body, tested)
            alternate = self._statements(statement.orelse, tested) if statement.orelse else (tested, True)
            return self._merge([body, alternate])
        if isinstance(statement, ast.While):
            tested = self._expression(statement.test, current)
            self._break_state_stack.append([])
            self._statements(statement.body, tested)
            break_states = self._break_state_stack.pop()
            natural = (
                self._statements(statement.orelse, tested)
                if statement.orelse
                else (tested, True)
            )
            return self._merge(
                [natural, *((break_state, True) for break_state in break_states)]
            )
        if isinstance(statement, ast.For | ast.AsyncFor):
            iterated = self._expression(statement.iter, current)
            body_entry = self._target(statement.target, iterated)
            self._break_state_stack.append([])
            self._statements(statement.body, body_entry)
            break_states = self._break_state_stack.pop()
            natural = (
                self._statements(statement.orelse, iterated)
                if statement.orelse
                else (iterated, True)
            )
            return self._merge(
                [natural, *((break_state, True) for break_state in break_states)]
            )
        if isinstance(statement, ast.With | ast.AsyncWith):
            for item in statement.items:
                current = self._expression(item.context_expr, current)
                if item.optional_vars is not None:
                    current = self._target(item.optional_vars, current)
            return self._statements(statement.body, current)
        if isinstance(statement, ast.Try | ast.TryStar):
            body_state, body_falls = self._statements(statement.body, current)
            normal = (
                self._statements(statement.orelse, body_state)
                if body_falls and statement.orelse
                else (body_state, body_falls)
            )
            branches = [normal]
            terminal_states = [body_state]
            for handler in statement.handlers:
                handler_entry = self._expression(handler.type, current)
                if handler.name:
                    handler_entry.add(handler.name)
                handler_state, handler_falls = self._statements(handler.body, handler_entry)
                if handler.name:
                    handler_state.discard(handler.name)
                branches.append((handler_state, handler_falls))
                terminal_states.append(handler_state)
            merged, merged_falls = self._merge(branches)
            if statement.finalbody:
                final_entry = set.intersection(current, *terminal_states)
                self._statements(statement.finalbody, final_entry)
                final_state, final_falls = self._statements(statement.finalbody, merged)
                return final_state, merged_falls and final_falls
            return merged, merged_falls
        if isinstance(statement, ast.Match):
            subject_state = self._expression(statement.subject, current)
            branches: list[tuple[set[str], bool]] = [(subject_state, True)]
            for case in statement.cases:
                case_state = subject_state | _pattern_names(case.pattern)
                case_state = self._expression(case.guard, case_state)
                branches.append(self._statements(case.body, case_state))
            return self._merge(branches)
        if isinstance(statement, ast.Import):
            current.update(alias.asname or alias.name.split(".", 1)[0] for alias in statement.names)
            return current, True
        if isinstance(statement, ast.ImportFrom):
            current.update(alias.asname or alias.name for alias in statement.names)
            return current, True
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            current = self._definition_expressions(statement, current)
            current.add(statement.name)
            return current, True
        if isinstance(statement, ast.ClassDef):
            for decorator in statement.decorator_list:
                current = self._expression(decorator, current)
            for base in statement.bases:
                current = self._expression(base, current)
            for keyword in statement.keywords:
                current = self._expression(keyword.value, current)
            current.add(statement.name)
            return current, True
        if isinstance(statement, ast.Return):
            return self._expression(statement.value, current), False
        if isinstance(statement, ast.Raise):
            current = self._expression(statement.exc, current)
            return self._expression(statement.cause, current), False
        if isinstance(statement, ast.Assert):
            current = self._expression(statement.test, current)
            return self._expression(statement.msg, current), True
        if isinstance(statement, ast.Global | ast.Nonlocal | ast.Pass):
            return current, True
        if isinstance(statement, ast.Break):
            if self._break_state_stack:
                self._break_state_stack[-1].append(set(current))
            return current, False
        if isinstance(statement, ast.Continue):
            return current, False
        for child in ast.iter_child_nodes(statement):
            if isinstance(child, ast.expr):
                current = self._expression(child, current)
        return current, True


def _python_context_defined_names(task: FunctionAnalysisTask) -> set[str]:
    names = set(_PYTHON_PREDEFINED_GLOBAL_NAMES) | set(_BUILTIN_NAMES)
    names.update(task.enclosing_scope_names)
    names.update(task.module_defined_names)
    if task.has_wildcard_import:
        # Star imports can provide any otherwise-unresolved global. Local names
        # still go through Python's definite-assignment rules below.
        try:
            module = ast.parse(_parseable_python_fragment(task.source))
        except (SyntaxError, ValueError, RecursionError):
            module = None
        if module is not None:
            names.update(
                node.id
                for node in ast.walk(module)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
            )
    names.add(task.qualified_name.rsplit(".", 1)[-1])
    names.update(_python_context_signatures(task.analysis_context))
    for match in re.finditer(
        r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b|"
        r"^\s*(?:from\s+[\w.]+\s+import|import)\s+([A-Za-z_][A-Za-z0-9_]*)\b|"
        r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=\n]+)?=",
        task.analysis_context,
        re.MULTILINE,
    ):
        names.update(value for value in match.groups() if value)
    for omitted in re.findall(
        r"(?m)^# module declaration for (.+?) omitted after \d+ characters$",
        task.analysis_context,
    ):
        names.update(
            value.strip()
            for value in omitted.split(",")
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value.strip())
        )
    return names


def _python_enclosing_scope_names(
    source_text: str,
    qualified_name: str,
    start_line: int,
) -> tuple[str, ...]:
    """Return names available through enclosing Python function closures."""
    try:
        module = ast.parse(source_text)
    except (SyntaxError, ValueError, RecursionError):
        return ()
    leaf = qualified_name.rsplit(".", 1)[-1]
    candidates = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == leaf
        and int(getattr(node, "lineno", 0) or 0) <= start_line
        and int(getattr(node, "end_lineno", 0) or 0) >= start_line
    ]
    if not candidates:
        return ()
    target = min(
        candidates,
        key=lambda node: int(getattr(node, "end_lineno", start_line) or start_line)
        - int(getattr(node, "lineno", start_line) or start_line),
    )
    parents = {
        child: parent
        for parent in ast.walk(module)
        for child in ast.iter_child_nodes(parent)
    }
    names: set[str] = set()
    current = parents.get(target)
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
            local_names, _global_names, _nonlocal_names = _python_scope_bindings(current)
            names.update(local_names)
        current = parents.get(current)
    return tuple(sorted(names))


def _minimum_length_proven_by_test(test: ast.AST, name: str) -> int | None:
    if isinstance(test, ast.Name) and test.id == name:
        return 1
    if isinstance(test, ast.Call) and _call_name(test.func) == "bool":
        if test.args and isinstance(test.args[0], ast.Name) and test.args[0].id == name:
            return 1
    if isinstance(test, ast.Compare) and len(test.ops) == len(test.comparators) == 1:
        left = test.left
        comparator = test.comparators[0]
        if (
            isinstance(left, ast.Call)
            and _call_name(left.func) == "len"
            and left.args
            and isinstance(left.args[0], ast.Name)
            and left.args[0].id == name
            and isinstance(comparator, ast.Constant)
            and isinstance(comparator.value, int)
        ):
            value = max(0, comparator.value)
            operator = test.ops[0]
            if isinstance(operator, ast.Gt):
                return value + 1
            if isinstance(operator, ast.GtE):
                return value
            if isinstance(operator, ast.NotEq) and value == 0:
                return 1
    if isinstance(test, ast.BoolOp):
        values = [
            value
            for item in test.values
            if (value := _minimum_length_proven_by_test(item, name)) is not None
        ]
        if not values:
            return None
        if isinstance(test.op, ast.And):
            return max(values)
        if len(values) == len(test.values):
            return min(values)
    return None


def _dominating_minimum_length(
    node: ast.AST,
    name: str,
    parents: dict[ast.AST, ast.AST],
) -> int | None:
    child = node
    parent = parents.get(child)
    while parent is not None:
        if isinstance(parent, ast.IfExp) and _node_contains(parent.body, child):
            return _minimum_length_proven_by_test(parent.test, name)
        if isinstance(parent, ast.If) and any(
            _node_contains(statement, child) for statement in parent.body
        ):
            return _minimum_length_proven_by_test(parent.test, name)
        if isinstance(parent, ast.BoolOp) and isinstance(parent.op, ast.And):
            guaranteed: list[int] = []
            for value in parent.values:
                if value is child or _node_contains(value, child):
                    break
                minimum = _minimum_length_proven_by_test(value, name)
                if minimum is not None:
                    guaranteed.append(minimum)
            if guaranteed:
                return max(guaranteed)
        child = parent
        parent = parents.get(parent)
    return None


def _name_has_sequence_contract(
    name: str,
    assignments: dict[str, list[ast.AST]],
    annotations: dict[str, str],
) -> bool:
    annotation = annotations.get(name, "").casefold()
    if any(
        marker in annotation
        for marker in ("list", "tuple", "sequence", "str", "bytes", "range")
    ):
        return True
    values = assignments.get(name, [])
    if len(values) != 1:
        return False
    value = values[0]
    if isinstance(value, ast.List | ast.Tuple) or (
        isinstance(value, ast.Constant) and isinstance(value.value, str | bytes)
    ):
        return True
    if isinstance(value, ast.Call):
        call_name = _call_name(value.func)
        return call_name in {"list", "tuple", "str", "bytes", "range"} or call_name.endswith(
            (".split", ".splitlines")
        )
    return isinstance(value, ast.Attribute) and value.attr == "parts"


def _constant_constraint(test: ast.AST) -> tuple[str, set[object]] | None:
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return None
    try:
        subject = ast.unparse(test.left)
    except Exception:
        return None
    operator = test.ops[0]
    comparator = test.comparators[0]
    if isinstance(operator, ast.Eq | ast.Is) and isinstance(comparator, ast.Constant):
        return subject, {comparator.value}
    if isinstance(operator, ast.In) and isinstance(comparator, ast.Set | ast.Tuple | ast.List):
        values = {
            item.value
            for item in comparator.elts
            if isinstance(item, ast.Constant)
        }
        if len(values) == len(comparator.elts):
            return subject, values
    return None


def _true_branch_constraints(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> dict[str, set[object]]:
    constraints: dict[str, set[object]] = {}
    child = node
    parent = parents.get(child)
    while parent is not None:
        test: ast.AST | None = None
        if isinstance(parent, ast.If) and any(
            statement is child or _node_contains(statement, child)
            for statement in parent.body
        ):
            test = parent.test
        elif isinstance(parent, ast.IfExp) and _node_contains(parent.body, child):
            test = parent.test
        if test is not None:
            values = [test]
            if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
                values = list(test.values)
            for value in values:
                constraint = _constant_constraint(value)
                if constraint is None:
                    constraints.setdefault(f"<predicate:{id(value)}>", set())
                    continue
                subject, allowed = constraint
                constraints[subject] = (
                    constraints[subject] & allowed
                    if subject in constraints
                    else set(allowed)
                )
        child = parent
        parent = parents.get(parent)
    return constraints


def _node_belongs_to_function(
    node: ast.AST,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    current: ast.AST | None = node
    while current is not None:
        current = parents.get(current)
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
            return current is function
    return False


def _statement_directly_binds_name(statement: ast.stmt, name: str) -> bool:
    if isinstance(statement, ast.Assign):
        return any(name in _target_names(target) for target in statement.targets)
    if isinstance(statement, ast.AnnAssign):
        return statement.value is not None and name in _target_names(statement.target)
    if isinstance(statement, ast.Import):
        return name in {
            alias.asname or alias.name.split(".", 1)[0]
            for alias in statement.names
        }
    if isinstance(statement, ast.ImportFrom):
        return name in {alias.asname or alias.name for alias in statement.names}
    return isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and statement.name == name


def _name_bound_before_statement_in_block(
    statement: ast.stmt,
    name: str,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    child: ast.AST = statement
    owner = parents.get(child)
    while owner is not None:
        body = getattr(owner, "body", None)
        if isinstance(body, list):
            containing = next(
                (
                    item
                    for item in body
                    if item is child or _node_contains(item, child)
                ),
                None,
            )
            if containing is not None:
                for prior in body[: body.index(containing)]:
                    if _statement_directly_binds_name(prior, name):
                        return True
        child = owner
        owner = parents.get(owner)
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            break
    return False


def _unbound_load_has_correlated_assignment(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    node: ast.Name,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    """Recognize source predicates that guarantee an otherwise branch-local assignment."""
    use_constraints = _true_branch_constraints(node, parents)
    assignments: list[ast.stmt] = []
    for candidate in ast.walk(function):
        if not isinstance(candidate, ast.Assign | ast.AnnAssign):
            continue
        if not _node_belongs_to_function(candidate, function, parents):
            continue
        targets = candidate.targets if isinstance(candidate, ast.Assign) else [candidate.target]
        if any(node.id in _target_names(target) for target in targets):
            assignments.append(candidate)
    for assignment in assignments:
        assignment_constraints = _true_branch_constraints(assignment, parents)
        if assignment_constraints and all(
            subject in use_constraints and use_constraints[subject] <= allowed
            for subject, allowed in assignment_constraints.items()
        ):
            return True

    flag_names: set[str] = set()
    child: ast.AST = node
    parent = parents.get(child)
    while parent is not None:
        in_true_branch = (
            isinstance(parent, ast.If)
            and any(
                statement is child or _node_contains(statement, child)
                for statement in parent.body
            )
        ) or (isinstance(parent, ast.IfExp) and _node_contains(parent.body, child))
        if in_true_branch:
            if isinstance(parent.test, ast.Name):
                flag_names.add(parent.test.id)
        child = parent
        parent = parents.get(parent)
    for flag_name in flag_names:
        true_assignments = [
            candidate
            for candidate in ast.walk(function)
            if isinstance(candidate, ast.Assign)
            and _node_belongs_to_function(candidate, function, parents)
            and any(flag_name in _target_names(target) for target in candidate.targets)
            and isinstance(candidate.value, ast.Constant)
            and candidate.value.value is True
        ]
        if true_assignments and all(
            _name_bound_before_statement_in_block(assignment, node.id, parents)
            for assignment in true_assignments
        ):
            return True
    return False


def _name_has_earlier_binding(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    node: ast.Name,
) -> bool:
    line = int(getattr(node, "lineno", 0) or 0)
    return any(
        isinstance(candidate, ast.Name)
        and isinstance(candidate.ctx, ast.Store)
        and candidate.id == node.id
        and int(getattr(candidate, "lineno", 0) or 0) < line
        for candidate in _python_function_scope_nodes(function)
    )


_BOUNDARY_FUNCTION_PATTERN = re.compile(
    r"(?:^|_)(?:worker|handler|callback|emit|lifespan|serve|background|batch)(?:_|$)",
    re.IGNORECASE,
)
_BOUNDARY_REPORT_CALL_PATTERN = re.compile(
    r"(?:^|\.)_*(?:exception|error|warning|critical|put|set_exception|set_result|"
    r"(?:log|report|record|notify|mark|reset)_[A-Za-z0-9_]+)$",
    re.IGNORECASE,
)


def _statements_always_raise(statements: list[ast.stmt]) -> bool:
    if not statements:
        return False
    final = statements[-1]
    if isinstance(final, ast.Raise):
        return True
    if isinstance(final, ast.If):
        return bool(final.orelse) and _statements_always_raise(
            final.body
        ) and _statements_always_raise(final.orelse)
    return False


def _handler_scope_nodes(handler: ast.ExceptHandler):
    stack: list[ast.AST] = list(reversed(handler.body))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _handler_has_control_exit(handler: ast.ExceptHandler) -> bool:
    return any(
        isinstance(node, ast.Return | ast.Continue | ast.Break)
        for node in _handler_scope_nodes(handler)
    )


def _handler_reports_boundary(handler: ast.ExceptHandler) -> bool:
    return any(
        isinstance(node, ast.Call)
        and bool(_BOUNDARY_REPORT_CALL_PATTERN.search(_call_name(node.func)))
        for node in _handler_scope_nodes(handler)
    )


def _handler_forwards_exception(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    handler: ast.ExceptHandler,
) -> bool:
    if not handler.name:
        return False
    forwarded_names: set[str] = set()
    for node in _handler_scope_nodes(handler):
        if (
            isinstance(node, ast.Assign | ast.AnnAssign)
            and isinstance(node.value, ast.Name)
            and node.value.id == handler.name
        ):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Attribute | ast.Subscript) for target in targets):
                return True
            forwarded_names.update(
                name
                for target in targets
                for name in _target_names(target)
            )
        if not isinstance(node, ast.Call):
            continue
        leaf = _call_name(node.func).rsplit(".", 1)[-1].casefold()
        if leaf not in {"append", "put", "put_nowait", "set_exception"}:
            continue
        if any(
            isinstance(argument, ast.Name) and argument.id == handler.name
            for argument in node.args
        ):
            return True
    handler_end = int(getattr(handler, "end_lineno", 0) or 0)
    return bool(forwarded_names) and any(
        isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in forwarded_names
        and int(getattr(node, "lineno", 0) or 0) > handler_end
        for node in _python_function_scope_nodes(function)
    )


def _handler_updates_fallback_state(handler: ast.ExceptHandler) -> bool:
    return any(
        isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign)
        and any(
            isinstance(target, ast.Attribute | ast.Subscript)
            for target in (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
            )
        )
        for node in _handler_scope_nodes(handler)
    )


def _handler_is_ast_render_fallback(
    handler: ast.ExceptHandler,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    owner = parents.get(handler)
    if not isinstance(owner, ast.Try | ast.TryStar):
        return False
    calls = [
        node
        for statement in owner.body
        for node in ast.walk(statement)
        if isinstance(node, ast.Call)
    ]
    return bool(calls) and all(
        _call_name(node.func) == "ast.unparse"
        for node in calls
    )


def _broad_handler_is_intentional_boundary(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    handler: ast.ExceptHandler,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    """Recognize structural exception boundaries without guessing from caught error types."""
    if _statements_always_raise(handler.body):
        return True
    if _handler_is_ast_render_fallback(handler, parents):
        return True

    reports = _handler_reports_boundary(handler)
    forwards = _handler_forwards_exception(function, handler)
    if forwards:
        return True

    ancestor = parents.get(handler)
    while ancestor is not None and ancestor is not function:
        if isinstance(ancestor, ast.For | ast.AsyncFor | ast.While):
            if reports or any(
                isinstance(node, ast.Continue)
                for node in _handler_scope_nodes(handler)
            ):
                return True
        ancestor = parents.get(ancestor)

    if reports and _handler_has_control_exit(handler):
        return True
    if _BOUNDARY_FUNCTION_PATTERN.search(function.name):
        return reports or _handler_updates_fallback_state(handler)
    return False


def _deterministic_python_issues(
    task: FunctionAnalysisTask,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    parents: dict[ast.AST, ast.AST],
) -> list[analysis_engine.FunctionIssue]:
    issues: list[analysis_engine.FunctionIssue] = []
    seen: set[tuple[str, int | None, str]] = set()

    def add(
        severity: str,
        category: str,
        title: str,
        description: str,
        node: ast.AST | None = None,
        *,
        failure_type: str | None = None,
        trigger: str | None = None,
    ) -> None:
        line = _absolute_line(task, node)
        key = (title, line, description)
        if key in seen:
            return
        seen.add(key)
        evidence = None
        relative_line = getattr(node, "lineno", None)
        source_lines = task.source.splitlines()
        if isinstance(relative_line, int) and 1 <= relative_line <= len(source_lines):
            evidence = source_lines[relative_line - 1].strip() or None
        issues.append(
            _issue(
                severity=severity,
                category=category,
                title=title,
                description=description,
                line=line,
                evidence=evidence if failure_type and trigger else None,
                failure_type=failure_type,
                trigger=trigger,
            )
        )

    parameter_names = {
        arg.arg
        for arg in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        )
    }
    if function.args.vararg is not None:
        parameter_names.add(function.args.vararg.arg)
    if function.args.kwarg is not None:
        parameter_names.add(function.args.kwarg.arg)
    assignments = _assignment_values(function)
    context_signatures = _python_context_signatures(task.analysis_context)
    local_signatures = _python_local_signatures(function)
    local_scope_names, _global_scope_names, _nonlocal_scope_names = _python_scope_bindings(
        function
    )
    scope_nodes = _python_function_scope_nodes(function)
    sql_parameter_names = _sql_text_capable_parameters(function)
    name_annotations = {
        argument.arg: (_annotation_text(argument.annotation) or "")
        for argument in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        )
    }

    for default in (*function.args.defaults, *[item for item in function.args.kw_defaults if item is not None]):
        if isinstance(default, ast.List | ast.Dict | ast.Set):
            add(
                "warning",
                "runtime",
                "Mutable default argument",
                "A mutable default value is shared across calls and can leak state between invocations.",
                default,
                failure_type="Shared mutable state",
                trigger="A call mutates the default and a later call reuses it.",
            )

    scope_analyzer = _PythonDefiniteAssignmentAnalyzer(
        function,
        _python_context_defined_names(task),
    )
    for node, failure_type, description in scope_analyzer.analyze():
        if failure_type == "UnboundLocalError" and _unbound_load_has_correlated_assignment(
            function,
            node,
            parents,
        ):
            continue
        add(
            (
                "unsafe"
                if failure_type == "UnboundLocalError"
                and _name_has_earlier_binding(function, node)
                else "error"
            ),
            "type",
            (
                "Local variable may be used before assignment"
                if failure_type == "UnboundLocalError"
                else "Possibly undefined variable"
            ),
            description,
            node,
            failure_type=failure_type,
            trigger="Execution reaches this name load along the reported control-flow path.",
        )

    optional_parameters = {
        arg.arg
        for arg in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
        if (annotation := _annotation_text(arg.annotation))
        and ("None" in annotation or "Optional" in annotation)
    }
    row_parameters = {
        arg.arg
        for arg in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
        if (annotation := _annotation_text(arg.annotation))
        and re.fullmatch(
            r"(?:sqlite3\.)?Row(?:\s*\|\s*None)?|Optional\[(?:sqlite3\.)?Row\]",
            annotation,
        )
    }
    sql_fstring_variables: set[str] = set()
    for node in scope_nodes:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.JoinedStr):
            if _sql_fstring_is_suspicious(node.value, assignments, sql_parameter_names):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        sql_fstring_variables.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and isinstance(node.value, ast.JoinedStr):
            if _sql_fstring_is_suspicious(node.value, assignments, sql_parameter_names):
                sql_fstring_variables.add(node.target.id)

    for node in scope_nodes:
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, int)
            and not isinstance(node.slice.value, bool)
            and _name_has_sequence_contract(
                node.value.id,
                assignments,
                name_annotations,
            )
        ):
            minimum = _dominating_minimum_length(node, node.value.id, parents)
            required = node.slice.value + 1 if node.slice.value >= 0 else abs(node.slice.value)
            if minimum is not None and minimum < required:
                add(
                    "unsafe",
                    "runtime",
                    "Index may exceed guarded sequence",
                    (
                        f"The guard proves only {minimum} item(s), but index "
                        f"{node.slice.value} requires at least {required}."
                    ),
                    node,
                    failure_type="IndexError",
                    trigger=(
                        f"`{node.value.id}` satisfies the guard with fewer than "
                        f"{required} items."
                    ),
                )

    for node in scope_nodes:
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in optional_parameters:
                if not (
                    _attribute_access_is_guarded(function, node, node.value.id, parents)
                    or _optional_name_is_normalized_before_access(
                        function, node, node.value.id, parents
                    )
                ):
                    add(
                        "unsafe",
                        "runtime",
                        "Possible None dereference",
                        f"Parameter `{node.value.id}` may be None before accessing `{node.attr}`.",
                        node,
                        failure_type="AttributeError",
                        trigger=f"Parameter `{node.value.id}` is None on this path.",
                    )
            if node.value.id in row_parameters:
                add(
                    "error",
                    "type",
                    "Invalid sqlite3.Row attribute access",
                    f"`sqlite3.Row` values should be accessed by key, not attribute `{node.attr}`.",
                    node,
                    failure_type="AttributeError",
                    trigger="Execution evaluates this attribute access on a sqlite3.Row value.",
                )

    for node in scope_nodes:
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            signature = None
            if isinstance(node.func, ast.Name):
                signature = local_signatures.get(name)
                if signature is None and name not in local_scope_names:
                    signature = context_signatures.get(name)
            if signature is not None:
                for title, description in _python_call_contract_problems(node, signature):
                    add(
                        "error",
                        "type",
                        title,
                        description,
                        node,
                        failure_type="TypeError",
                        trigger="This call is evaluated with the shown arguments.",
                    )
            if (
                name.endswith(".execute")
                and node.args
                and isinstance(node.args[0], ast.JoinedStr)
                and _sql_fstring_is_suspicious(
                    node.args[0],
                    assignments,
                    sql_parameter_names,
                )
            ):
                add(
                    "unsafe",
                    "security",
                    "SQL built with f-string",
                    "SQL passed to execute() contains non-static interpolation; use bound parameters instead.",
                    node,
                    failure_type="SQL injection",
                    trigger="An interpolated value contains attacker-controlled SQL syntax.",
                )
            if (
                name.endswith(".execute")
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in sql_fstring_variables
            ):
                add(
                    "unsafe",
                    "security",
                    "SQL built with f-string",
                    "SQL passed to execute() is built from an interpolated SQL string; use bound parameters instead.",
                    node,
                    failure_type="SQL injection",
                    trigger="An interpolated value contains attacker-controlled SQL syntax.",
                )
            if name == "open":
                parent = parents.get(node)
                in_with = False
                while parent is not None:
                    if isinstance(parent, ast.With):
                        in_with = True
                        break
                    if isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef):
                        break
                    parent = parents.get(parent)
                if not in_with:
                    add(
                        "warning",
                        "resource",
                        "File opened without context manager",
                        "A file handle opened without `with` may leak if an exception occurs before close.",
                        node,
                        failure_type="Resource leak",
                        trigger="An exception exits the function before the handle is closed.",
                    )

            if isinstance(function, ast.AsyncFunctionDef) and name.endswith("_async"):
                parent = parents.get(node)
                if not isinstance(parent, ast.Await):
                    add(
                        "warning",
                        "runtime",
                        "Async call is not awaited",
                        f"Call to `{name}` in an async function is not awaited.",
                        node,
                        failure_type="Unawaited coroutine",
                        trigger="Execution reaches this call without awaiting or returning its coroutine.",
                    )

    for node in scope_nodes:
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                catches_broad = True
            elif isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}:
                catches_broad = True
            else:
                catches_broad = False
            if catches_broad and not _broad_handler_is_intentional_boundary(
                function,
                node,
                parents,
            ):
                add(
                    "warning",
                    "maintainability",
                    "Broad exception handler",
                    "A broad exception handler can hide malformed input and unrelated programming errors.",
                    node,
                    failure_type="Suppressed unrelated exception",
                    trigger="An unexpected exception is caught by this broad handler.",
                )

    return_annotation = _annotation_text(function.returns)
    if (
        return_annotation
        and normalize_type(return_annotation) in _STATIC_RETURN_CONTRACT_TYPES
    ):
        shadowed_names = _python_return_shadowed_names(
            function, task.analysis_context
        )
        for node in scope_nodes:
            if isinstance(node, ast.Return) and node.value is not None:
                inferred_types = _static_python_return_types(
                    node.value,
                    [],
                    task,
                    shadowed_names=shadowed_names,
                )
                if inferred_types and types_compatible(
                    tuple(inferred_types), (return_annotation,)
                ) is False:
                    actual_type = " | ".join(inferred_types)
                    add(
                        "error",
                        "type",
                        "Return type does not match annotation",
                        f"Annotated return type is `{return_annotation}` but this path returns `{actual_type}`.",
                        node,
                        failure_type="Return contract violation",
                        trigger="Execution returns the evidenced expression.",
                    )

    return issues


def _parameter_contract(
    name: str,
    kind: str,
    required: bool,
    annotation: ast.AST | None,
    default: ast.AST | None,
    existing: analysis_engine.FunctionParameterContract | None = None,
) -> analysis_engine.FunctionParameterContract:
    accepted_types = (
        [_annotation_text(annotation)]
        if _annotation_text(annotation)
        else (existing.accepted_types if existing is not None else ["unknown"])
    )
    default_description = None
    if default is not None:
        default_description = "Default value is present."
        try:
            default_description = f"Default: {ast.unparse(default)}"
        except Exception:
            pass
    return analysis_engine.FunctionParameterContract(
        name=name,
        kind=kind,
        required=required,
        accepted_types=accepted_types,
        default_description=default_description,
        description=existing.description if existing is not None else f"{name} parameter.",
    )


def deterministic_python_contract(
    task: FunctionAnalysisTask,
    result: analysis_engine.FunctionAnalysisResult,
) -> analysis_engine.FunctionAnalysisResult:
    """Overlay source-derived function contracts on top of model prose."""
    if task.language == "typescript":
        signature = typescript_signature(task.source)
        if signature is None:
            return result
        parameters, return_type = signature
        if result.source_facts and result.source_facts.get("source_sha256") == hashlib.sha256(task.source.encode("utf-8")).hexdigest():
            # The strict semantic merge already retained the declared signature.
            return result
        updates: dict[str, object] = {
            "parameters": [analysis_engine.FunctionParameterContract(**parameter) for parameter in parameters],
        }
        if return_type:
            has_value = return_type not in {"void", "never"}
            updates["returns"] = analysis_engine.FunctionReturnContract(
                may_return_value=has_value,
                possible_types=[analysis_engine.FunctionReturnType(type=return_type, description="Source-declared TypeScript return type.")] if has_value else [],
                nullable=has_value and bool(re.search(r"\b(?:null|undefined)\b", return_type)),
                description="Source-declared TypeScript return contract; implementation behavior still requires review.",
            )
        return result.model_copy(update=updates)
    if task.language != "python":
        return result
    try:
        module = ast.parse(_parseable_python_fragment(task.source))
    except SyntaxError as exc:
        line = task.start_line + (exc.lineno or 1) - 1
        issue = _issue(
            severity="error",
            category="syntax",
            title="Python syntax error",
            description=exc.msg or "The function contains invalid Python syntax.",
            line=line,
        )
        return result.model_copy(
            update={
                "syntax_valid": False,
                "issues": [*result.issues, issue][:50],
            }
        )
    parents = {
        child: parent
        for parent in ast.walk(module)
        for child in ast.iter_child_nodes(parent)
    }
    leaf_name = task.qualified_name.rsplit(".", 1)[-1]
    function = next(
        (
            node
            for node in ast.walk(module)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == leaf_name
        ),
        None,
    )
    if function is None:
        function = next(
            (
                node
                for node in ast.walk(module)
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            ),
            None,
        )
    if function is None:
        return result

    args = function.args
    parameters: list[analysis_engine.FunctionParameterContract] = []
    existing_parameters = {parameter.name: parameter for parameter in result.parameters}
    positional = [*args.posonlyargs, *args.args]
    positional_defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    for index, (arg, default) in enumerate(zip(positional, positional_defaults)):
        kind = "positional_only" if index < len(args.posonlyargs) else "positional_or_keyword"
        if index == 0 and arg.arg in {"self", "cls"} and "." in task.qualified_name:
            kind = "receiver"
        parameters.append(
            _parameter_contract(
                arg.arg,
                kind,
                default is None,
                arg.annotation,
                default,
                existing_parameters.get(arg.arg),
            )
        )
    if args.vararg is not None:
        parameters.append(
            _parameter_contract(
                args.vararg.arg,
                "variadic_positional",
                False,
                args.vararg.annotation,
                None,
                existing_parameters.get(args.vararg.arg),
            )
        )
    keyword_defaults = list(args.kw_defaults)
    for arg, default in zip(args.kwonlyargs, keyword_defaults):
        parameters.append(
            _parameter_contract(
                arg.arg,
                "keyword_only",
                default is None,
                arg.annotation,
                default,
                existing_parameters.get(arg.arg),
            )
        )
    if args.kwarg is not None:
        parameters.append(
            _parameter_contract(
                args.kwarg.arg,
                "variadic_keyword",
                False,
                args.kwarg.annotation,
                None,
                existing_parameters.get(args.kwarg.arg),
            )
        )

    return_annotation = _annotation_text(function.returns)
    possible_return_types: list[analysis_engine.FunctionReturnType] = []
    nullable = False
    if return_annotation:
        if return_annotation in {"None", "NoReturn"}:
            may_return_value = False
        else:
            may_return_value = True
            nullable = "None" in return_annotation or "Optional" in return_annotation
            possible_return_types.append(
                analysis_engine.FunctionReturnType(
                    type=return_annotation,
                    description=f"Annotated return type {return_annotation}.",
                )
            )
    else:
        inferred: list[str] = []
        saw_value_return = False
        saw_value_yield = False
        shadowed_names = _python_return_shadowed_names(function, task.analysis_context)
        for node in _python_function_scope_nodes(function):
            if isinstance(node, ast.Return):
                if node.value is None or (
                    isinstance(node.value, ast.Constant) and node.value.value is None
                ):
                    nullable = True
                    continue
                saw_value_return = True
                inferred_types = _static_python_return_types(
                    node.value, parameters, task, shadowed_names=shadowed_names
                )
                if inferred_types:
                    inferred.extend(inferred_types)
            elif isinstance(node, ast.Yield | ast.YieldFrom):
                value = node.value if isinstance(node, ast.Yield) else node.value
                if value is None:
                    continue
                saw_value_yield = True
                inferred_types = _static_python_return_types(
                    value, parameters, task, shadowed_names=shadowed_names
                )
                if inferred_types:
                    inferred.extend(inferred_types)
        contextmanager = _has_decorator(function, {"contextmanager", "contextlib.contextmanager"})
        may_return_value = saw_value_return or saw_value_yield
        if inferred:
            return_type_names = _unique_strings(inferred, 12)
        elif may_return_value and result.returns.may_return_value:
            return_type_names = [item.type for item in result.returns.possible_types]
        else:
            return_type_names = ["unknown"] if may_return_value else []
        if contextmanager and saw_value_yield and not inferred:
            return_type_names = ["contextmanager"]
        for type_name in _unique_strings(return_type_names, 12):
            possible_return_types.append(
                analysis_engine.FunctionReturnType(
                    type=type_name,
                    description=f"Observed return type {type_name}.",
                )
            )

    returns = analysis_engine.FunctionReturnContract(
        may_return_value=may_return_value,
        possible_types=possible_return_types,
        nullable=nullable if may_return_value else False,
        description=(
            "Deterministic Python AST return contract."
            if may_return_value
            else "No value-returning return statement was found."
        ),
    )
    deterministic_issues = _deterministic_python_issues(task, function, parents)
    existing_issue_keys = {
        (
            issue.severity,
            issue.category,
            issue.title.casefold(),
            issue.start_line,
        )
        for issue in result.issues
    }
    issues = list(result.issues)
    for issue in deterministic_issues:
        key = (
            issue.severity,
            issue.category,
            issue.title.casefold(),
            issue.start_line,
        )
        if key not in existing_issue_keys and len(issues) < 50:
            issues.append(issue)
            existing_issue_keys.add(key)
    raised_errors = _unique_strings(
        [*result.raised_errors, *_explicit_raise_names(function)],
        30,
    )
    # A semantic review already distinguishes observed raise statements from
    # escaping exceptions and fills unknown returns. Do not undo that merge.
    if result.source_facts and result.source_facts.get("source_sha256") == hashlib.sha256(task.source.encode("utf-8")).hexdigest():
        returns = result.returns
        raised_errors = result.raised_errors
    return result.model_copy(
        update={
            "parameters": parameters,
            "returns": returns,
            "raised_errors": raised_errors,
            "issues": issues,
        }
    )


def _combined_description(prefix: str, values: list[str], max_length: int) -> str:
    text = prefix + " ".join(_unique_strings(values, len(values)))
    return text[:max_length].rstrip()


def merge_function_chunk_analyses(
    results: list[analysis_engine.FunctionAnalysisResult],
) -> analysis_engine.FunctionAnalysisResult:
    """Merge bounded fragment contracts without another model request."""
    if not results:
        raise ValueError("At least one function chunk result is required")
    parameters: dict[str, analysis_engine.FunctionParameterContract] = {}
    for result in results:
        for parameter in result.parameters:
            existing = parameters.get(parameter.name)
            if existing is None:
                if len(parameters) < 100:
                    parameters[parameter.name] = parameter
                continue
            accepted_types = _unique_strings(
                [*existing.accepted_types, *parameter.accepted_types], 12
            )
            parameters[parameter.name] = existing.model_copy(
                update={
                    "kind": (
                        parameter.kind if existing.kind == "unknown" else existing.kind
                    ),
                    "required": existing.required and parameter.required,
                    "accepted_types": accepted_types,
                    "default_description": (
                        existing.default_description or parameter.default_description
                    ),
                }
            )

    return_types: dict[str, analysis_engine.FunctionReturnType] = {}
    for result in results:
        for return_type in result.returns.possible_types:
            return_types.setdefault(return_type.type.casefold(), return_type)
            if len(return_types) == 12:
                break
    may_return_value = any(result.returns.may_return_value for result in results)
    possible_types = list(return_types.values()) if may_return_value else []
    nullable = may_return_value and any(result.returns.nullable for result in results)

    issues: list[analysis_engine.FunctionIssue] = []
    issue_keys: set[tuple[object, ...]] = set()
    for result in results:
        for issue in result.issues:
            key = (
                issue.severity,
                issue.category,
                issue.title.casefold(),
                issue.start_line,
                issue.end_line,
            )
            if key not in issue_keys and len(issues) < 50:
                issue_keys.add(key)
                issues.append(issue)

    return analysis_engine.FunctionAnalysisResult(
        contract_version=analysis_engine.FUNCTION_ANALYSIS_CONTRACT_VERSION,
        summary=_combined_description(
            f"Combined analysis of {len(results)} bounded fragments. ",
            [result.summary for result in results],
            2_000,
        ),
        syntax_valid=all(result.syntax_valid for result in results),
        parameters=list(parameters.values()),
        returns=analysis_engine.FunctionReturnContract(
            may_return_value=may_return_value,
            possible_types=possible_types,
            nullable=nullable,
            description=_combined_description(
                "Combined fragment results: ",
                [result.returns.description for result in results],
                1_500,
            ),
        ),
        raised_errors=_unique_strings(
            [value for result in results for value in result.raised_errors], 30
        ),
        side_effects=_unique_strings(
            [value for result in results for value in result.side_effects], 30
        ),
        issues=issues,
        confidence=min(result.confidence for result in results),
        analysis_method="model" if any(result.analysis_method == "model" for result in results) else results[0].analysis_method,
        review_status="failed" if any(result.review_status == "failed" for result in results) else "partial" if any(result.review_status == "partial" for result in results) else "complete",
        validation_notes=[note for result in results for note in result.validation_notes][:100],
    )


def _cache_key(
    task: FunctionAnalysisTask,
    *,
    function_sha256: str | None = None,
) -> tuple[object, ...]:
    return (
        task.user_id,
        task.language,
        function_sha256 or task.cache_function_sha256 or task.function_sha256,
        _cache_contract_version(),
        current_ollama_model(OLLAMA_MODEL),
    )


def _exception_chain_contains(exc: BaseException, needle: type[BaseException]) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, needle):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _is_malformed_model_json_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".casefold()
    return (
        _exception_chain_contains(exc, json.JSONDecodeError)
        or _exception_chain_contains(exc, analysis_engine.FunctionAnalysisResponseError)
        or "validationerror" in text
        or "function analysis response" in text
        or "malformed json" in text
        or "valid json" in text
        or "jsondecodeerror" in text
    )


def _fallback_function_analysis_result(
    task: FunctionAnalysisTask,
    exc: BaseException,
) -> analysis_engine.FunctionAnalysisResult:
    try:
        if task.language == "python":
            ast.parse(_parseable_python_fragment(task.source))
        syntax_valid = True
        issues: list[analysis_engine.FunctionIssue] = []
    except SyntaxError as syntax_error:
        syntax_valid = False
        line = task.start_line + max(int(syntax_error.lineno or 1) - 1, 0)
        issues = [
            _issue(
                severity="error",
                category="syntax",
                title="Syntax error",
                description=syntax_error.msg or "Python syntax error.",
                line=line,
                provenance="fallback",
            )
        ]
    return analysis_engine.FunctionAnalysisResult(
        contract_version=analysis_engine.FUNCTION_ANALYSIS_CONTRACT_VERSION,
        summary=(
            "Fallback static analysis used because the model response was invalid or incomplete: "
            f"{type(exc).__name__}: {str(exc)[:240]}"
        ),
        syntax_valid=syntax_valid,
        parameters=[],
        returns=analysis_engine.FunctionReturnContract(
            may_return_value=False,
            possible_types=[],
            nullable=False,
            description="Fallback result before deterministic source-derived return analysis.",
        ),
        raised_errors=[],
        side_effects=[],
        issues=issues,
        confidence=0.35,
        analysis_method="fallback",
        review_status="failed",
        validation_notes=[f"Model review failed: {type(exc).__name__}: {str(exc)[:600]}"] + (
            ["Fallback did not check syntax; the indexed language parser remains authoritative."]
            if task.language != "python" else []
        ),
        response_sha256=getattr(exc, "response_sha256", None),
    )


def _deterministic_seed_result() -> analysis_engine.FunctionAnalysisResult:
    return analysis_engine.FunctionAnalysisResult(
        contract_version=analysis_engine.FUNCTION_ANALYSIS_CONTRACT_VERSION,
        summary="Deterministic source analysis completed without an LLM call.",
        syntax_valid=True,
        parameters=[],
        returns=analysis_engine.FunctionReturnContract(
            may_return_value=False,
            possible_types=[],
            nullable=False,
            description="Deterministic seed before source-derived return analysis.",
        ),
        raised_errors=[],
        side_effects=[],
        issues=[],
        confidence=0.8,
        analysis_method="deterministic",
    )


def deterministic_completion_summary(
    task: FunctionAnalysisTask,
    result: analysis_engine.FunctionAnalysisResult,
) -> str:
    """Describe a locally completed contract using only parsed source facts."""
    return_type = (
        ", ".join(item.type for item in result.returns.possible_types)
        if result.returns.may_return_value else "no explicit value"
    )
    try:
        module = ast.parse(_parseable_python_fragment(task.source))
        function = _locate_task_function(task, module)
    except (SyntaxError, ValueError, RecursionError):
        function = None
    if function is not None:
        for statement in function.body:
            if not isinstance(statement, ast.If):
                continue
            raises = [node for node in statement.body if isinstance(node, ast.Raise)]
            error_names = _unique_strings(
                [
                    name
                    for node in raises
                    if node.exc is not None
                    and (name := (
                        _call_name(node.exc.func)
                        if isinstance(node.exc, ast.Call)
                        else _call_name(node.exc)
                    ))
                ],
                6,
            )
            if not error_names:
                continue
            condition = " ".join(ast.unparse(statement.test).split())[:220]
            return (
                f"{task.qualified_name} validates `{condition}` and raises "
                f"{', '.join(error_names)} when the check fails; it returns "
                f"{return_type} when valid."
            )[:1_000]
    return (
        "Deterministic Python AST analysis completed without an LLM call. "
        f"{task.qualified_name} returns {return_type}."
    )


def deterministic_python_analysis(task: FunctionAnalysisTask) -> analysis_engine.FunctionAnalysisResult:
    return deterministic_python_contract(task, _deterministic_seed_result())


def _locate_task_function(
    task: FunctionAnalysisTask,
    module: ast.Module,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    leaf_name = task.qualified_name.rsplit(".", 1)[-1]
    function = next(
        (
            node
            for node in ast.walk(module)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == leaf_name
        ),
        None,
    )
    if function is not None:
        return function
    return next(
        (
            node
            for node in ast.walk(module)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ),
        None,
    )


def deterministic_analysis_contract_is_complete(
    task: FunctionAnalysisTask,
    result: analysis_engine.FunctionAnalysisResult,
) -> bool:
    """Return true when deterministic AST facts are enough to avoid a model call."""
    if task.language != "python" or not result.syntax_valid or result.issues:
        return False
    for parameter in result.parameters:
        if parameter.kind == "receiver" or parameter.name in {"self", "cls"}:
            continue
        accepted = {item.casefold() for item in parameter.accepted_types}
        if not accepted or any(
            re.fullmatch(r"(?:typing\.)?(?:any|object|unknown|dynamic)", item.strip())
            for item in accepted
        ):
            return False
    if result.returns.may_return_value:
        possible_types = {item.type.casefold() for item in result.returns.possible_types}
        if not possible_types or any(
            re.fullmatch(r"(?:typing\.)?(?:any|object|unknown|dynamic)", item.strip())
            for item in possible_types
        ):
            return False
    try:
        module = ast.parse(_parseable_python_fragment(task.source))
    except SyntaxError:
        return False
    function = _locate_task_function(task, module)
    if function is None:
        return False
    if not function.body:
        return False
    scope_nodes = _python_function_scope_nodes(function)
    # Bound the local fast path before checking its supported operations below.
    branch_count = sum(
        isinstance(
            node,
            ast.If
            | ast.For
            | ast.AsyncFor
            | ast.While
            | ast.Try
            | ast.Match
            | ast.IfExp
            | ast.comprehension,
        )
        for node in scope_nodes
    )
    if len(scope_nodes) > 240 or branch_count > 18:
        return False
    dynamic_review_calls = {"eval", "exec", "compile", "__import__"}
    bound_names = _python_scope_bindings(function)[0]
    raised_call_ids = {
        id(node.exc)
        for node in scope_nodes
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
    }
    try:
        context_module = ast.parse(task.analysis_context)
    except SyntaxError:
        return False
    for statement in context_module.body:
        bound_names.update(_statement_bound_names(statement))
    for node in scope_nodes:
        if isinstance(node, ast.Call) and _call_name(node.func) in dynamic_review_calls:
            return False
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            # Type annotations alone do not cover I/O, helper behavior, or method
            # side effects. Keep only a small pure-operation fast path.
            pure_builtin = name in {"str", "int", "float", "bool", "len", "abs", "min", "max"} and name not in bound_names
            pure_string_method = (
                isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                and node.func.attr in {"strip", "lstrip", "rstrip", "lower", "upper", "casefold"}
                and any(parameter.name == node.func.value.id and parameter.accepted_types == ["str"]
                        for parameter in result.parameters)
            )
            called_object = (
                getattr(builtins, name, None)
                if isinstance(name, str) and "." not in name else None
            )
            raised_builtin_exception = (
                id(node) in raised_call_ids
                and isinstance(called_object, type)
                and issubclass(called_object, BaseException)
            )
            if not pure_builtin and not pure_string_method and not raised_builtin_exception:
                return False
    # Small typed branches with only the pure operations admitted above can be
    # checked locally. Loops, handlers and asynchronous behavior still need review.
    if branch_count > 4 or any(isinstance(node, ast.For | ast.AsyncFor | ast.While | ast.Try | ast.Match | ast.comprehension | ast.Await | ast.Yield | ast.YieldFrom | ast.With | ast.AsyncWith) for node in scope_nodes):
        return False
    return True


def deterministic_analysis_is_sufficient(
    result: analysis_engine.FunctionAnalysisResult,
) -> bool:
    """Invalid syntax blocks meaningful semantic review; other defects do not."""
    return not result.syntax_valid and any(
        issue.provenance == "deterministic"
        and issue.category == "syntax"
        and issue.severity == "error"
        and issue.proof == "source-v1"
        for issue in result.issues
    )


def _result_with_relative_issue_lines(
    task: FunctionAnalysisTask,
    result: analysis_engine.FunctionAnalysisResult,
) -> analysis_engine.FunctionAnalysisResult:
    issues = []
    for issue in result.issues:
        updates: dict[str, int | None] = {}
        for field in ("start_line", "end_line"):
            line = getattr(issue, field)
            if line is not None:
                if not task.start_line <= line <= task.end_line:
                    raise ValueError(
                        f"Analysis issue line {line} is outside "
                        f"{task.start_line}-{task.end_line}"
                    )
                updates[field] = line - task.start_line + 1
        issues.append(issue.model_copy(update=updates))
    return result.model_copy(update={"issues": issues})


def _result_with_absolute_issue_lines(
    task: FunctionAnalysisTask,
    result: analysis_engine.FunctionAnalysisResult,
) -> analysis_engine.FunctionAnalysisResult:
    line_count = task.end_line - task.start_line + 1
    issues = []
    for issue in result.issues:
        updates: dict[str, int | None] = {}
        for field in ("start_line", "end_line"):
            line = getattr(issue, field)
            if line is not None:
                if line > line_count:
                    raise ValueError(
                        f"Cached issue line {line} exceeds the {line_count}-line function"
                    )
                updates[field] = task.start_line + line - 1
        issues.append(issue.model_copy(update=updates))
    return result.model_copy(update={"issues": issues})


def _result_has_line_anchored_issues(
    result: analysis_engine.FunctionAnalysisResult,
) -> bool:
    return any(
        issue.start_line is not None or issue.end_line is not None
        for issue in result.issues
    )


def _load_cached_function_analysis_for_sha(
    db: sqlite3.Connection,
    task: FunctionAnalysisTask,
    function_sha256: str,
    *,
    allow_line_anchored_issues: bool,
) -> analysis_engine.FunctionAnalysisResult | None:
    """Return a validated cached result for one hash, rebased to this function."""
    row = db.execute(
        """
        SELECT response_json
        FROM function_analysis_cache
        WHERE user_id = ? AND language = ? AND function_sha256 = ?
          AND contract_version = ? AND model_name = ?
          AND line_basis = 'function_relative_v1'
        """,
        _cache_key(task, function_sha256=function_sha256),
    ).fetchone()
    if row is None:
        return None
    try:
        relative_result = analysis_engine.FunctionAnalysisResult.model_validate_json(
            str(row["response_json"])
        )
        if (relative_result.source_facts and relative_result.source_facts.get("source_sha256")
                != hashlib.sha256(task.source.encode("utf-8")).hexdigest()):
            # Semantic cache hashes can ignore comments/formatting; exact source
            # observations must be rebuilt when their source fingerprint changes.
            return None
        if (
            not allow_line_anchored_issues
            and _result_has_line_anchored_issues(relative_result)
        ):
            return None
        result = _result_with_absolute_issue_lines(task, relative_result)
        result = _result_with_issue_provenance(result, "cache")
    except (ValueError, TypeError):
        # A corrupt cache row must never turn into a failed project function.
        db.execute(
            """
            DELETE FROM function_analysis_cache
            WHERE user_id = ? AND language = ? AND function_sha256 = ?
              AND contract_version = ? AND model_name = ?
            """,
            _cache_key(task, function_sha256=function_sha256),
        )
        return None
    db.execute(
        """
        UPDATE function_analysis_cache
        SET use_count = use_count + 1, last_used_at = CURRENT_TIMESTAMP
        WHERE user_id = ? AND language = ? AND function_sha256 = ?
          AND contract_version = ? AND model_name = ?
        """,
        _cache_key(task, function_sha256=function_sha256),
    )
    return result


def load_cached_function_analysis(
    db: sqlite3.Connection,
    task: FunctionAnalysisTask,
) -> analysis_engine.FunctionAnalysisResult | None:
    """Return a validated cached result, rebased to this function's file lines."""
    exact_sha = task.cache_function_sha256 or task.function_sha256
    semantic_sha = (
        task.semantic_cache_function_sha256
        or task.semantic_function_sha256
        or exact_sha
    )
    if semantic_sha != exact_sha:
        cached = _load_cached_function_analysis_for_sha(
            db,
            task,
            semantic_sha,
            allow_line_anchored_issues=False,
        )
        if cached is not None:
            return cached
    return _load_cached_function_analysis_for_sha(
        db,
        task,
        exact_sha,
        allow_line_anchored_issues=True,
    )


def store_cached_function_analysis(
    db: sqlite3.Connection,
    task: FunctionAnalysisTask,
    result: analysis_engine.FunctionAnalysisResult,
) -> None:
    if result.review_status != "complete" or result.analysis_method == "fallback":
        return
    relative_result = _result_with_relative_issue_lines(task, result)
    exact_sha = task.cache_function_sha256 or task.function_sha256
    semantic_sha = (
        task.semantic_cache_function_sha256
        or task.semantic_function_sha256
        or exact_sha
    )
    cache_sha256 = (
        exact_sha
        if _result_has_line_anchored_issues(relative_result)
        else semantic_sha
    )
    db.execute(
        """
        INSERT INTO function_analysis_cache(
            user_id, language, function_sha256, contract_version, model_name,
            line_basis, response_json
        ) VALUES (?, ?, ?, ?, ?, 'function_relative_v1', ?)
        ON CONFLICT(user_id, language, function_sha256, contract_version, model_name)
        DO UPDATE SET response_json = excluded.response_json,
                      line_basis = excluded.line_basis,
                      updated_at = CURRENT_TIMESTAMP
        """,
        (
            *_cache_key(task, function_sha256=cache_sha256),
            relative_result.model_dump_json(),
        ),
    )


def persist_function_analysis(
    db: sqlite3.Connection,
    task: FunctionAnalysisTask,
    result: analysis_engine.FunctionAnalysisResult,
    *,
    cache_result: bool = True,
) -> None:
    current_task = load_function_analysis_task(db, task.symbol_id, include_inferred=task.include_inferred_context)
    if (
        current_task.source_sha256 != task.source_sha256
        or current_task.source != task.source
        or current_task.analysis_context != task.analysis_context
        or current_task.cache_function_sha256 != task.cache_function_sha256
    ):
        raise StaleSymbolSource(
            "Function or dependency context changed while Ollama was analysing it"
        )
    if cache_result:
        store_cached_function_analysis(db, task, result)
    db.execute("DELETE FROM project_symbol_parameters WHERE symbol_id = ?", (task.symbol_id,))
    db.execute("DELETE FROM project_symbol_return_types WHERE symbol_id = ?", (task.symbol_id,))
    db.execute("DELETE FROM project_symbol_issues WHERE symbol_id = ?", (task.symbol_id,))
    db.execute(
        """
        INSERT INTO project_symbol_analyses(
            symbol_id, project_id, file_id, contract_version, model_name,
            source_sha256, response_json, summary, syntax_valid,
            may_return_value, return_nullable, return_description,
            raised_errors_json, side_effects_json, confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol_id) DO UPDATE SET
            contract_version = excluded.contract_version,
            model_name = excluded.model_name,
            source_sha256 = excluded.source_sha256,
            response_json = excluded.response_json,
            summary = excluded.summary,
            syntax_valid = excluded.syntax_valid,
            may_return_value = excluded.may_return_value,
            return_nullable = excluded.return_nullable,
            return_description = excluded.return_description,
            raised_errors_json = excluded.raised_errors_json,
            side_effects_json = excluded.side_effects_json,
            confidence = excluded.confidence,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            task.symbol_id,
            task.project_id,
            task.file_id,
            result.contract_version,
            current_ollama_model(OLLAMA_MODEL),
            task.source_sha256,
            result.model_dump_json(),
            result.summary,
            int(result.syntax_valid),
            int(result.returns.may_return_value),
            int(result.returns.nullable),
            result.returns.description,
            json.dumps(result.raised_errors, separators=(",", ":")),
            json.dumps(result.side_effects, separators=(",", ":")),
            result.confidence,
        ),
    )
    db.executemany(
        """
        INSERT INTO project_symbol_parameters(
            symbol_id, ordinal, name, parameter_kind, required,
            accepted_types_json, default_description, description
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                task.symbol_id,
                ordinal,
                parameter.name,
                parameter.kind,
                int(parameter.required),
                json.dumps(parameter.accepted_types, separators=(",", ":")),
                parameter.default_description,
                parameter.description,
            )
            for ordinal, parameter in enumerate(result.parameters)
        ),
    )
    db.executemany(
        """
        INSERT INTO project_symbol_return_types(
            symbol_id, ordinal, type_name, description
        ) VALUES (?, ?, ?, ?)
        """,
        (
            (task.symbol_id, ordinal, item.type, item.description)
            for ordinal, item in enumerate(result.returns.possible_types)
        ),
    )
    db.executemany(
        """
        INSERT INTO project_symbol_issues(
            symbol_id, ordinal, severity, category, title, description,
            start_line, end_line, provenance, proof, evidence, failure_type,
            trigger, report_tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                task.symbol_id,
                ordinal,
                issue.severity,
                issue.category,
                issue.title,
                issue.description,
                issue.start_line,
                issue.end_line,
                issue.provenance,
                issue.proof,
                issue.evidence,
                issue.failure_type,
                issue.trigger,
                issue_report_tier(issue),
            )
            for ordinal, issue in enumerate(result.issues)
        ),
    )
    db.execute(
        """
        UPDATE project_symbols
        SET analysis_status = ?, analysis_attempt_count = analysis_attempt_count + 1,
            analysis_error = ?, analyzed_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            "completed" if result.review_status in {"complete", "partial"} else "failed",
            None if result.review_status in {"complete", "partial"} else ("Incomplete model review: " + "; ".join(result.validation_notes))[:1_000],
            task.symbol_id,
        ),
    )


def mark_symbol_analysis(
    db: sqlite3.Connection,
    symbol_id: int,
    status: str,
    error: str | None,
    *,
    increment_attempt: bool,
) -> None:
    db.execute(
        """
        UPDATE project_symbols
        SET analysis_status = ?,
            analysis_attempt_count = analysis_attempt_count + ?,
            analysis_error = ?, analyzed_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (status, int(increment_attempt), error, symbol_id),
    )


def refresh_project_function_analysis(
    db: sqlite3.Connection,
    project_id: str,
    *,
    forced_status: str | None = None,
) -> ProjectFunctionAnalysisSummary:
    counts = {
        str(row["analysis_status"]): int(row["count"])
        for row in db.execute(
            """
            SELECT analysis_status, COUNT(*) AS count
            FROM project_symbols
            WHERE project_id = ? AND symbol_kind IN ('function', 'method')
            GROUP BY analysis_status
            """,
            (project_id,),
        ).fetchall()
    }
    total = sum(counts.values())
    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0) + counts.get("stale", 0)
    skipped = counts.get("skipped", 0)
    pending = counts.get("pending", 0)
    processing = counts.get("processing", 0)
    if forced_status is not None:
        status = forced_status
    elif total == 0 or completed == total:
        status = "completed"
    elif processing:
        status = "running"
    elif pending == total:
        status = "pending"
    elif completed:
        status = "partial"
    elif failed + skipped == total:
        status = "failed"
    else:
        status = "partial"
    errors = [
        str(row["analysis_error"])
        for row in db.execute(
            """
            SELECT analysis_error FROM project_symbols
            WHERE project_id = ? AND analysis_error IS NOT NULL
            ORDER BY analyzed_at DESC, id DESC LIMIT 5
            """,
            (project_id,),
        ).fetchall()
    ]
    project_row = db.execute(
        """
        SELECT function_analysis_cache_hit_count,
               function_analysis_model_request_count,
               function_analysis_batch_request_count,
               function_analysis_deterministic_count,
               function_analysis_batch_fallback_count,
               function_analysis_batch_error
        FROM projects WHERE id = ?
        """,
        (project_id,),
    ).fetchone()
    cache_hits = (
        int(project_row["function_analysis_cache_hit_count"] or 0)
        if project_row
        else 0
    )
    model_requests = (
        int(project_row["function_analysis_model_request_count"] or 0)
        if project_row
        else 0
    )
    batch_requests = (
        int(project_row["function_analysis_batch_request_count"] or 0)
        if project_row
        else 0
    )
    deterministic_count = (
        int(project_row["function_analysis_deterministic_count"] or 0)
        if project_row
        else 0
    )
    batch_fallback_count = (
        int(project_row["function_analysis_batch_fallback_count"] or 0)
        if project_row
        else 0
    )
    batch_error = (
        str(project_row["function_analysis_batch_error"])
        if project_row and project_row["function_analysis_batch_error"]
        else None
    )
    db.execute(
        """
        UPDATE projects
        SET function_analysis_status = ?, function_analysis_total_count = ?,
            function_analysis_completed_count = ?, function_analysis_failed_count = ?,
            function_analysis_skipped_count = ?, function_analysis_error = ?,
            function_analysis_updated_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            status,
            total,
            completed,
            failed,
            skipped,
            "; ".join(errors)[:2_000] or None,
            project_id,
        ),
    )
    return ProjectFunctionAnalysisSummary(
        status,
        total,
        completed,
        failed,
        skipped,
        cache_hits,
        model_requests,
        batch_requests,
        deterministic_count,
        batch_fallback_count,
        batch_error,
    )


def _task_may_have_cached_analysis(
    db: sqlite3.Connection,
    task: FunctionAnalysisTask,
) -> bool:
    exact_sha = task.cache_function_sha256 or task.function_sha256
    semantic_sha = (
        task.semantic_cache_function_sha256
        or task.semantic_function_sha256
        or exact_sha
    )
    for function_sha256 in dict.fromkeys((semantic_sha, exact_sha)):
        row = db.execute(
            """
            SELECT 1
            FROM function_analysis_cache
            WHERE user_id = ? AND language = ? AND function_sha256 = ?
              AND contract_version = ? AND model_name = ?
              AND line_basis = 'function_relative_v1'
            LIMIT 1
            """,
            _cache_key(task, function_sha256=function_sha256),
        ).fetchone()
        if row is not None:
            return True
    return False


def _batch_candidate_payload(candidate: _FunctionBatchCandidate) -> dict[str, object]:
    task = candidate.task
    return {
        "request_id": f"symbol-{task.symbol_id}",
        "language": task.language,
        "file_path": task.file_path,
        "symbol_kind": task.symbol_kind,
        "qualified_name": task.qualified_name,
        "start_line": task.start_line,
        "end_line": task.end_line,
        "source": task.source,
        "analysis_context": task.analysis_context,
    }


def _reset_batch_candidates_to_pending(
    connection_factory: ConnectionFactory,
    candidates: list[_FunctionBatchCandidate],
) -> None:
    with connection_factory() as db:
        db.executemany(
            """
            UPDATE project_symbols
            SET analysis_status = 'pending', analysis_error = NULL
            WHERE id = ? AND analysis_status = 'processing'
            """,
            ((candidate.task.symbol_id,) for candidate in candidates),
        )


def _stop_project_for_ollama_failure(
    connection_factory: ConnectionFactory,
    project_id: str,
    error: analysis_engine.OllamaUnavailableError,
) -> None:
    """Preserve completed work and release all active batch/chunk targets for resumption."""
    with connection_factory() as db:
        db.execute(
            "UPDATE project_symbols SET analysis_status = 'pending', analysis_error = NULL "
            "WHERE project_id = ? AND analysis_status = 'processing'",
            (project_id,),
        )
        refresh_project_function_analysis(db, project_id, forced_status="failed")
        db.execute(
            "UPDATE projects SET function_analysis_error = ? WHERE id = ?",
            (f"{type(error).__name__}: {error}"[:1_000], project_id),
        )


def _increment_analysis_metrics(
    connection_factory: ConnectionFactory,
    project_id: str,
    *,
    model_requests: int = 0,
    batch_requests: int = 0,
) -> None:
    """Persist request attempts before invoking the model so failures remain measurable."""
    with connection_factory() as db:
        db.execute(
            """
            UPDATE projects
            SET function_analysis_model_request_count =
                    function_analysis_model_request_count + ?,
                function_analysis_batch_request_count =
                    function_analysis_batch_request_count + ?
            WHERE id = ?
            """,
            (model_requests, batch_requests, project_id),
        )


def _record_batch_fallback(
    connection_factory: ConnectionFactory,
    project_id: str,
    error: Exception,
    candidate_count: int,
) -> None:
    """Keep batch degradation visible without turning it into an analysis failure."""
    detail = " ".join(str(error).split()) or "No error detail was returned"
    message = (
        f"{type(error).__name__}: {detail} "
        f"({candidate_count}-function batch)"
    )[:1_000]
    with connection_factory() as db:
        db.execute(
            """
            UPDATE projects
            SET function_analysis_batch_fallback_count =
                    function_analysis_batch_fallback_count + 1,
                function_analysis_batch_error = ?
            WHERE id = ?
            """,
            (message, project_id),
        )


def _analyze_project_function_batches(
    connection_factory: ConnectionFactory,
    project_id: str,
    selected_ids: list[int],
    symbol_metadata: dict[int, tuple[str, str, int, int, int, int]],
    *,
    total: int,
    chunk_chars: int,
    batch_request: BatchAnalysisRequest,
    cancel_check: Callable[[], bool] | None,
    pause_wait: Callable[[], None] | None,
    progress_callback: ProgressCallback | None,
    deferred_ids: set[int],
) -> tuple[set[int], bool]:
    """Pre-analyse eligible functions in bounded model batches.

    Cache hits, deterministic results, and oversized functions remain on the one-symbol path.
    Failed groups are bisected so valid smaller groups are retained before irreducible failures
    fall back to the same one-symbol recovery path.
    """
    processed: set[int] = set()
    queued: list[_FunctionBatchCandidate] = []
    prompt_overhead = len(analysis_engine.FUNCTION_REVIEW_RULES) + 600
    queued_chars = prompt_overhead

    def process_batch(
        candidates: list[_FunctionBatchCandidate],
    ) -> tuple[bool, bool]:
        """Try one group, retaining useful subgroups when a larger batch fails."""
        if len(candidates) < 2:
            _reset_batch_candidates_to_pending(connection_factory, candidates)
            return False, False
        if pause_wait:
            pause_wait()
        if cancel_check and cancel_check():
            return False, True
        with connection_factory() as db:
            budgets = [prepare_budget(db, candidate.task) for candidate in candidates]
        if FUNCTION_ANALYSIS_ADAPTIVE_OUTPUT and sum(b["estimated_total_tokens"] for b in budgets) > min(16384, FUNCTION_ANALYSIS_BATCH_MAX_OUTPUT_TOKENS):
            midpoint = len(candidates) // 2
            left_success, cancelled = process_batch(candidates[:midpoint])
            if cancelled:
                return False, True
            right_success, cancelled = process_batch(candidates[midpoint:])
            return left_success or right_success, cancelled
        with connection_factory() as db:
            for candidate, budget in zip(candidates, budgets):
                save_budget(db, candidate.task, budget)
            db.executemany(
                """
                UPDATE project_symbols
                SET analysis_status = 'processing', analysis_error = NULL
                WHERE id = ?
                """,
                ((candidate.task.symbol_id,) for candidate in candidates),
            )
        for batch_index, candidate in enumerate(candidates, 1):
            if progress_callback:
                progress_callback(
                    "analyzing_function_batch",
                    candidate.current,
                    total,
                    candidate.file_path,
                    (
                        f"{candidate.qualified_name} · batch "
                        f"{batch_index} of {len(candidates)}"
                    ),
                    candidate.file_current,
                    candidate.file_total,
                    candidate.function_current,
                    candidate.function_total,
                )
        payloads = [_batch_candidate_payload(candidate) for candidate in candidates]
        expected_ids = {str(payload["request_id"]) for payload in payloads}
        try:
            _increment_analysis_metrics(
                connection_factory,
                project_id,
                model_requests=1,
                batch_requests=1,
            )
            batch_results = run_budgeted(
                connection_factory, [candidate.task for candidate in candidates], budgets, batch_request, batch=True,
                functions=payloads,
                cancel_check=cancel_check,
            )
            if not isinstance(batch_results, dict):
                raise TypeError("Function-analysis batch did not return a result mapping")
            if set(batch_results) != expected_ids or any(
                not isinstance(result, analysis_engine.FunctionAnalysisResult)
                for result in batch_results.values()
            ):
                raise ValueError("Function-analysis batch results did not match its targets")
        except analysis_engine.AnalysisCancelled:
            _reset_batch_candidates_to_pending(connection_factory, candidates)
            return False, True
        except analysis_engine.OllamaUnavailableError as exc:
            _stop_project_for_ollama_failure(connection_factory, project_id, exc)
            raise
        except analysis_engine.FunctionAnalysisBatchFormatError as exc:
            _record_batch_fallback(
                connection_factory,
                project_id,
                exc,
                len(candidates),
            )
            _reset_batch_candidates_to_pending(connection_factory, candidates)
            return False, False
        except Exception as exc:
            _record_batch_fallback(
                connection_factory,
                project_id,
                exc,
                len(candidates),
            )
            _reset_batch_candidates_to_pending(connection_factory, candidates)
            if len(candidates) <= 2:
                return False, False
            midpoint = len(candidates) // 2
            left_success, cancelled = process_batch(candidates[:midpoint])
            if cancelled:
                _reset_batch_candidates_to_pending(
                    connection_factory,
                    candidates[midpoint:],
                )
                return False, True
            right_success, cancelled = process_batch(candidates[midpoint:])
            return left_success and right_success, cancelled

        for candidate in candidates:
            task = candidate.task
            request_id = f"symbol-{task.symbol_id}"
            try:
                result = deterministic_python_contract(task, batch_results[request_id])
                with connection_factory() as db:
                    result = filter_source_proven_false_issues(db, task, result)
                    persist_function_analysis(db, task, result)
                processed.add(task.symbol_id)
            except StaleSymbolSource as exc:
                with connection_factory() as db:
                    mark_symbol_analysis(
                        db,
                        task.symbol_id,
                        "stale",
                        str(exc),
                        increment_attempt=False,
                    )
                processed.add(task.symbol_id)
            except Exception as exc:
                with connection_factory() as db:
                    mark_symbol_analysis(
                        db,
                        task.symbol_id,
                        "failed",
                        f"{type(exc).__name__}: {exc}"[:1_000],
                        increment_attempt=True,
                    )
                processed.add(task.symbol_id)
        return True, False

    def flush_batch() -> tuple[bool, bool]:
        nonlocal queued, queued_chars
        candidates = queued
        queued = []
        queued_chars = prompt_overhead
        if len(candidates) < 2:
            return True, False
        return process_batch(candidates)

    for current, symbol_id in enumerate(selected_ids, 1):
        if symbol_id in deferred_ids:
            continue
        if pause_wait:
            pause_wait()
        if cancel_check and cancel_check():
            return processed, True
        try:
            with connection_factory() as db:
                task = load_function_analysis_task(db, symbol_id, include_inferred=True)
                if _task_may_have_cached_analysis(db, task):
                    continue
                deterministic_result = deterministic_python_analysis(task)
                deterministic_result = filter_source_proven_false_issues(
                    db,
                    task,
                    deterministic_result,
                )
        except Exception:
            continue
        if deterministic_analysis_is_sufficient(
            deterministic_result
        ) or deterministic_analysis_contract_is_complete(task, deterministic_result):
            continue
        estimated_chars = len(task.source) + len(task.analysis_context) + 500
        if len(task.source) > chunk_chars or estimated_chars + prompt_overhead > FUNCTION_ANALYSIS_BATCH_MAX_CHARS:
            continue
        if queued and (
            len(queued) >= FUNCTION_ANALYSIS_BATCH_SIZE
            or queued_chars + estimated_chars > FUNCTION_ANALYSIS_BATCH_MAX_CHARS
        ):
            batch_size = len(queued)
            success, cancelled = flush_batch()
            if cancelled:
                return processed, True
            if batch_size >= 2 and not success:
                return processed, False
        (
            file_path,
            qualified_name,
            file_current,
            file_total,
            function_current,
            function_total,
        ) = symbol_metadata[symbol_id]
        queued.append(
            _FunctionBatchCandidate(
                current=current,
                task=task,
                file_path=file_path,
                qualified_name=qualified_name,
                file_current=file_current,
                file_total=file_total,
                function_current=function_current,
                function_total=function_total,
            )
        )
        queued_chars += estimated_chars
        if len(queued) >= FUNCTION_ANALYSIS_BATCH_SIZE:
            success, cancelled = flush_batch()
            if cancelled:
                return processed, True
            if not success:
                return processed, False
    if len(queued) >= 2:
        success, cancelled = flush_batch()
        if cancelled:
            return processed, True
        if not success:
            return processed, False
    return processed, False


def _prepare_engine_fact_reviews(
    connection_factory: ConnectionFactory, project_id: str, selected_ids: list[int],
    symbol_metadata: dict, *, total: int, force_model: bool,
    cancel_check, pause_wait, progress_callback,
) -> tuple[dict[int, dict], set[int], bool]:
    """Check all selected source locally before any LLM request; never execute uploads."""
    from semantic_review import build_engine_facts
    packets: dict[int, dict] = {}
    finished: set[int] = set()
    for current, symbol_id in enumerate(selected_ids, 1):
        if pause_wait:
            pause_wait()
        if cancel_check and cancel_check():
            return packets, finished, True
        try:
            with connection_factory() as db:
                task = load_function_analysis_task(db, symbol_id, include_inferred=False)
                result = filter_source_proven_false_issues(db, task, deterministic_python_analysis(task))
                packet = build_engine_facts(task, result)
                if packet is not None:
                    packets[symbol_id] = packet
                if force_model or not (
                    deterministic_analysis_is_sufficient(result)
                    or deterministic_analysis_contract_is_complete(task, result)
                ):
                    continue
                result = result.model_copy(update={
                    "summary": deterministic_completion_summary(task, result)
                        if result.syntax_valid else "Source syntax errors prevent semantic review of " + task.qualified_name + ".",
                    "source_facts": packet["facts"] if packet else None,
                })
                persist_function_analysis(db, task, result)
                db.execute("UPDATE projects SET function_analysis_deterministic_count = function_analysis_deterministic_count + 1 WHERE id = ?", (project_id,))
                refresh_project_function_analysis(db, project_id, forced_status="running")
                finished.add(symbol_id)
            if progress_callback:
                progress_callback("using_deterministic_function", current, total, *symbol_metadata[symbol_id])
        except (LookupError, StaleSymbolSource, ValueError, RecursionError):
            # The ordinary symbol path records a stale/missing source precisely.
            continue
    return packets, finished, False


@analysis_engine.ollama_context_scope()
def analyze_project_functions(
    connection_factory: ConnectionFactory,
    project_id: str,
    *,
    analysis_request: AnalysisRequest | None = None,
    batch_analysis_request: BatchAnalysisRequest | None = None,
    chunk_analysis_request: AnalysisRequest | None = None,
    cancel_check: Callable[[], bool] | None = None,
    pause_wait: Callable[[], None] | None = None,
    progress_callback: ProgressCallback | None = None,
    retry_failed: bool = False,
    limit: int | None = None,
    selected_symbol_ids: set[int] | None = None,
    force_model: bool = False,
) -> ProjectFunctionAnalysisSummary:
    """Analyse pending symbols sequentially, committing each result for safe resumption."""
    request = analysis_request or analysis_engine.request_function_analysis
    batch_request = (
        batch_analysis_request or analysis_engine.request_function_analysis_batch
    )
    chunk_request = chunk_analysis_request or (
        analysis_request or analysis_engine.request_function_chunk_analysis
    )
    chunk_chars = min(
        FUNCTION_ANALYSIS_CHUNK_CHARS,
        FUNCTION_ANALYSIS_MAX_SOURCE_CHARS,
    )
    statuses = ["pending", "processing"]
    if retry_failed:
        statuses.extend(("failed", "stale", "skipped"))
    with connection_factory() as db:
        all_symbols = db.execute(
            """
            SELECT symbol.id, symbol.analysis_status, symbol.file_id,
                   symbol.qualified_name, file.path,
                   json_extract(analysis.response_json, '$.review_status') AS review_status
            FROM project_symbols AS symbol
            JOIN project_files AS file ON file.id = symbol.file_id
            LEFT JOIN project_symbol_analyses AS analysis ON analysis.symbol_id = symbol.id
            WHERE symbol.project_id = ?
              AND symbol.symbol_kind IN ('function', 'method')
            ORDER BY file.path COLLATE NOCASE, file.path, symbol.start_byte, symbol.id
            """,
            (project_id,),
        ).fetchall()
        requested_ids = set(selected_symbol_ids or ())
        symbol_ids = [
            int(row["id"])
            for row in all_symbols
            if (
                int(row["id"]) in requested_ids
                if selected_symbol_ids is not None
                else (
                    str(row["analysis_status"]) in statuses
                    or (
                        retry_failed
                        and str(row["review_status"] or "") != "complete"
                    )
                )
            )
        ]
        edges = [(int(row[0]), int(row[1])) for row in db.execute(
            "SELECT DISTINCT caller_symbol_id, resolved_symbol_id FROM project_calls "
            "WHERE project_id = ? AND resolution_status = 'internal' "
            "AND caller_symbol_id IS NOT NULL AND resolved_symbol_id IS NOT NULL",
            (project_id,),
        )]
        ordered_ids, cyclic_ids = dependency_order([int(row["id"]) for row in all_symbols], edges)
        pending_ids = set(symbol_ids)
        symbol_ids = [symbol_id for symbol_id in ordered_ids if symbol_id in pending_ids]
        refresh_project_function_analysis(db, project_id, forced_status="running")
    file_ids = list(dict.fromkeys(int(row["file_id"]) for row in all_symbols))
    file_ordinals = {file_id: ordinal for ordinal, file_id in enumerate(file_ids, 1)}
    symbols_by_file: dict[int, list[int]] = {}
    symbol_metadata: dict[int, tuple[str, str, int, int, int, int]] = {}
    for row in all_symbols:
        file_id = int(row["file_id"])
        symbols_by_file.setdefault(file_id, []).append(int(row["id"]))
    function_ordinals = {
        symbol_id: ordinal
        for file_symbols in symbols_by_file.values()
        for ordinal, symbol_id in enumerate(file_symbols, 1)
    }
    for row in all_symbols:
        symbol_id = int(row["id"])
        file_id = int(row["file_id"])
        file_symbols = symbols_by_file[file_id]
        symbol_metadata[symbol_id] = (
            str(row["path"]),
            str(row["qualified_name"]),
            file_ordinals[file_id],
            len(file_ids),
            function_ordinals[symbol_id],
            len(file_symbols),
        )
    selected_ids = symbol_ids[:limit] if limit is not None else symbol_ids
    total = len(symbol_ids)
    engine_packets: dict[int, dict] = {}
    locally_finished: set[int] = set()
    # Preserve the existing full-contract interface for injected/custom reviewers.
    if (analysis_request is None and batch_analysis_request is None
            and chunk_analysis_request is None and request is _DEFAULT_ANALYSIS_REQUEST):
        engine_packets, locally_finished, local_cancelled = _prepare_engine_fact_reviews(
            connection_factory, project_id, selected_ids, symbol_metadata, total=total,
            force_model=force_model,
            cancel_check=cancel_check, pause_wait=pause_wait, progress_callback=progress_callback,
        )
        if local_cancelled:
            with connection_factory() as db:
                return refresh_project_function_analysis(db, project_id, forced_status="cancelled")
    batch_enabled = not force_model and FUNCTION_ANALYSIS_BATCH_SIZE >= 2 and (
        batch_analysis_request is not None
        or (
            analysis_request is None
            and request is _DEFAULT_ANALYSIS_REQUEST
            and batch_request is _DEFAULT_BATCH_ANALYSIS_REQUEST
        )
    )
    batched_symbol_ids: set[int] = set()
    if batch_enabled:
        batched_symbol_ids, batch_cancelled = _analyze_project_function_batches(
            connection_factory,
            project_id,
            selected_ids,
            symbol_metadata,
            total=total,
            chunk_chars=chunk_chars,
            batch_request=batch_request,
            cancel_check=cancel_check,
            pause_wait=pause_wait,
            progress_callback=progress_callback,
            deferred_ids=cyclic_ids | locally_finished | set(engine_packets) | {caller for caller, callee in edges if callee in pending_ids},
        )
        if batch_cancelled:
            with connection_factory() as db:
                return refresh_project_function_analysis(
                    db,
                    project_id,
                    forced_status="cancelled",
                )
    for current, symbol_id in enumerate(selected_ids, 1):
        if symbol_id in batched_symbol_ids or symbol_id in locally_finished:
            continue
        task: FunctionAnalysisTask | None = None
        if pause_wait:
            pause_wait()
        if cancel_check and cancel_check():
            with connection_factory() as db:
                return refresh_project_function_analysis(
                    db, project_id, forced_status="cancelled"
                )
        try:
            deterministic_result: analysis_engine.FunctionAnalysisResult | None = None
            used_deterministic_result = False
            with connection_factory() as db:
                task = load_function_analysis_task(db, symbol_id, include_inferred=symbol_id not in cyclic_ids)
                packet = engine_packets.get(symbol_id)
                if packet and packet["facts"]["source_sha256"] != hashlib.sha256(task.source.encode("utf-8")).hexdigest():
                    packet = None
                budget = prepare_budget(db, task, recursive=symbol_id in cyclic_ids, semantic_review=packet is not None)
                save_budget(db, task, budget)
                mark_symbol_analysis(db, symbol_id, "processing", None, increment_attempt=False)
                cached_result = None if force_model else load_cached_function_analysis(db, task)
                if cached_result is None:
                    deterministic_result = analysis_engine.FunctionAnalysisResult.model_validate(packet["contract"]) if packet else deterministic_python_analysis(task)
                    deterministic_result = filter_source_proven_false_issues(
                        db,
                        task,
                        deterministic_result,
                    )
            (
                file_path,
                qualified_name,
                file_current,
                file_total,
                function_current,
                function_total,
            ) = symbol_metadata[symbol_id]
            if progress_callback and (
                cached_result is not None or len(task.source) <= chunk_chars
            ):
                progress_callback(
                    (
                        "using_cached_function"
                        if cached_result is not None
                        else "analyzing_function"
                    ),
                    current,
                    total,
                    file_path,
                    qualified_name,
                    file_current,
                    file_total,
                    function_current,
                    function_total,
                )
            if cached_result is None:
                if deterministic_result is None:
                    deterministic_result = deterministic_python_analysis(task)
                if not force_model and (
                    deterministic_analysis_is_sufficient(deterministic_result)
                    or deterministic_analysis_contract_is_complete(
                        task,
                        deterministic_result,
                    )
                ):
                    result = deterministic_result.model_copy(
                        update={
                            "summary": deterministic_completion_summary(
                                task, deterministic_result
                            ),
                            "confidence": max(deterministic_result.confidence, 0.82),
                        }
                    )
                    used_deterministic_result = True
                    if progress_callback:
                        progress_callback(
                            "using_deterministic_function",
                            current,
                            total,
                            file_path,
                            qualified_name,
                            file_current,
                            file_total,
                            function_current,
                            function_total,
                        )
                elif len(task.source) <= chunk_chars:
                    _increment_analysis_metrics(
                        connection_factory,
                        project_id,
                        model_requests=1,
                    )
                    result = run_budgeted(connection_factory, [task], [budget], request,
                        **({"engine_facts": packet} if packet else {}),
                        language=task.language,
                        file_path=task.file_path,
                        symbol_kind=task.symbol_kind,
                        qualified_name=task.qualified_name,
                        start_line=task.start_line,
                        end_line=task.end_line,
                        source=task.source,
                        analysis_context=task.analysis_context,
                        cancel_check=cancel_check,
                    )
                else:
                    chunks = split_function_source(
                        task.source,
                        task.start_line,
                        chunk_chars=chunk_chars,
                    )
                    chunk_results = []
                    for chunk in chunks:
                        if pause_wait:
                            pause_wait()
                        if cancel_check and cancel_check():
                            raise analysis_engine.AnalysisCancelled("Analysis cancelled")
                        with connection_factory() as db:
                            chunk_budget = prepare_budget(db, task, source=chunk.source, recursive=symbol_id in cyclic_ids, semantic_review=packet is not None)
                            save_budget(db, task, chunk_budget)
                        if progress_callback:
                            progress_callback(
                                "analyzing_function_chunk",
                                current,
                                total,
                                file_path,
                                (
                                    f"{qualified_name} · fragment "
                                    f"{chunk.index} of {chunk.total}"
                                ),
                                file_current,
                                file_total,
                                function_current,
                                function_total,
                            )
                        _increment_analysis_metrics(
                            connection_factory,
                            project_id,
                            model_requests=1,
                        )
                        chunk_results.append(
                            run_budgeted(connection_factory, [task], [chunk_budget], chunk_request,
                                **({"engine_facts": packet} if packet and chunk_request is _DEFAULT_CHUNK_ANALYSIS_REQUEST else {}),
                                language=task.language,
                                file_path=task.file_path,
                                symbol_kind=task.symbol_kind,
                                qualified_name=task.qualified_name,
                                function_start_line=task.start_line,
                                function_end_line=task.end_line,
                                chunk_start_line=chunk.start_line,
                                chunk_end_line=chunk.end_line,
                                chunk_index=chunk.index,
                                chunk_total=chunk.total,
                                source=chunk.source,
                                analysis_context=task.analysis_context,
                                cancel_check=cancel_check,
                            )
                        )
                    if packet and chunk_request is _DEFAULT_CHUNK_ANALYSIS_REQUEST:
                        from semantic_review import merge_semantic_chunks
                        result = merge_semantic_chunks(chunk_results, packet)
                    else:
                        result = merge_function_chunk_analyses(chunk_results)
            else:
                result = cached_result
            result = deterministic_python_contract(task, result)
            with connection_factory() as db:
                result = filter_source_proven_false_issues(db, task, result)
                persist_function_analysis(db, task, result)
                refresh_project_function_analysis(db, project_id, forced_status="running")
                if used_deterministic_result:
                    db.execute(
                        """
                        UPDATE projects
                        SET function_analysis_deterministic_count =
                            function_analysis_deterministic_count + 1
                        WHERE id = ?
                        """,
                        (project_id,),
                    )
                if cached_result is not None:
                    db.execute(
                        """
                        UPDATE projects
                        SET function_analysis_cache_hit_count =
                            function_analysis_cache_hit_count + 1
                        WHERE id = ?
                        """,
                        (project_id,),
                    )
        except analysis_engine.AnalysisCancelled:
            with connection_factory() as db:
                mark_symbol_analysis(db, symbol_id, "pending", None, increment_attempt=False)
                return refresh_project_function_analysis(
                    db, project_id, forced_status="cancelled"
                )
        except analysis_engine.OllamaUnavailableError as exc:
            _stop_project_for_ollama_failure(connection_factory, project_id, exc)
            raise
        except StaleSymbolSource as exc:
            with connection_factory() as db:
                mark_symbol_analysis(db, symbol_id, "stale", str(exc), increment_attempt=False)
        except Exception as exc:  # one model failure must not discard completed function results
            if task is not None and _is_malformed_model_json_error(exc):
                try:
                    fallback = _fallback_function_analysis_result(task, exc)
                    fallback = deterministic_python_contract(task, fallback)
                    with connection_factory() as db:
                        fallback = filter_source_proven_false_issues(db, task, fallback)
                        persist_function_analysis(
                            db,
                            task,
                            fallback,
                            cache_result=False,
                        )
                    continue
                except Exception as fallback_exc:
                    exc = fallback_exc
            with connection_factory() as db:
                mark_symbol_analysis(
                    db,
                    symbol_id,
                    "failed",
                    f"{type(exc).__name__}: {exc}"[:1_000],
                    increment_attempt=True,
                )
    with connection_factory() as db:
        return refresh_project_function_analysis(db, project_id)
