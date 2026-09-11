"""Bounded resolved dependency evidence and cycle-aware scheduling."""

from __future__ import annotations

import hashlib
import ast
import heapq
import json
import sqlite3
import textwrap

from project_inventory import decode_text_content


def dependency_order(symbol_ids: list[int], edges: list[tuple[int, int]]) -> tuple[list[int], set[int]]:
    """Order dependency SCCs before callers without recursion on deep projects.

    Members of a recursive component retain the original stable order. Their
    inferred contracts must not be fed back into other members as new facts.
    """
    rank = {symbol_id: index for index, symbol_id in enumerate(symbol_ids)}
    graph = {symbol_id: set() for symbol_id in symbol_ids}
    reverse = {symbol_id: set() for symbol_id in symbol_ids}
    for caller, callee in edges:
        if caller in graph and callee in graph:
            graph[caller].add(callee)
            reverse[callee].add(caller)
    visited: set[int] = set()
    finished: list[int] = []
    for start in symbol_ids:
        if start in visited:
            continue
        stack = [(start, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                finished.append(node)
            elif node not in visited:
                visited.add(node)
                stack.append((node, True))
                stack.extend(
                    (child, False) for child in sorted(graph[node], key=rank.__getitem__, reverse=True) if child not in visited)
    visited.clear()
    components: list[list[int]] = []
    cyclic: set[int] = set()
    for start in reversed(finished):
        if start in visited:
            continue
        component = []
        pending = [start]
        visited.add(start)
        while pending:
            node = pending.pop()
            component.append(node)
            for child in reverse[node]:
                if child not in visited:
                    visited.add(child)
                    pending.append(child)
        component.sort(key=rank.__getitem__)
        if len(component) > 1 or start in graph[start]:
            cyclic.update(component)
        components.append(component)
    # Components were discovered caller-first. Preserve source order for unrelated
    # components using a dependency-first stable sort over their condensation DAG.
    owner = {node: index for index, component in enumerate(components) for node in component}
    deps = {index: {owner[child] for node in component for child in graph[node] if owner[child] != index}
            for index, component in enumerate(components)}
    waiting = {index: len(dependencies) for index, dependencies in deps.items()}
    consumers = {index: set() for index in deps}
    for index, dependencies in deps.items():
        for dependency in dependencies:
            consumers[dependency].add(index)
    priorities = {index: min(rank[node] for node in component) for index, component in enumerate(components)}
    ready = [(priorities[index], index) for index, count in waiting.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[int] = []
    while ready:
        _, index = heapq.heappop(ready)
        ordered.extend(components[index])
        for consumer in consumers[index]:
            waiting[consumer] -= 1
            if waiting[consumer] == 0:
                heapq.heappush(ready, (priorities[consumer], consumer))
    return ordered, cyclic


def resolved_context_items(db: sqlite3.Connection, project_id: str, symbol_id: int,
                           *, include_inferred: bool = False) -> list[str]:
    """Use indexed call resolution; matching names alone never establish identity."""
    rows = db.execute(
        """
        SELECT DISTINCT symbol.id, symbol.qualified_name, symbol.start_byte, symbol.end_byte,
               symbol.body_start_byte, symbol.source_sha256, symbol.analysis_status,
               file.path, file.content, file.parser_source_sha256,
               analysis.response_json, analysis.source_sha256 AS analysis_source_sha256
        FROM project_calls AS call
        JOIN project_symbols AS symbol ON symbol.id = call.resolved_symbol_id
        JOIN project_files AS file ON file.id = symbol.file_id
        LEFT JOIN project_symbol_analyses AS analysis ON analysis.symbol_id = symbol.id
        WHERE call.project_id = ? AND symbol.project_id = ? AND call.caller_symbol_id = ?
          AND call.resolution_status = 'internal' AND symbol.id != ?
        ORDER BY file.path, symbol.start_byte, symbol.id LIMIT 40
        """, (project_id, project_id, symbol_id, symbol_id),
    ).fetchall()
    sources = []
    contracts = []
    for row in rows:
        source, _ = decode_text_content(bytes(row["content"]))
        if source is None:
            continue
        encoded = source.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        if digest != row["source_sha256"] or digest != row["parser_source_sha256"]:
            continue
        start, end = int(row["start_byte"]), int(row["end_byte"])
        if not 0 <= start < end <= len(encoded):
            continue
        body_start = row["body_start_byte"]
        excerpt_end = end if end - start <= 1_200 else body_start
        if excerpt_end is None or not start < int(excerpt_end) <= end:
            continue
        try:
            excerpt = encoded[start:int(excerpt_end)].decode("utf-8")
        except UnicodeDecodeError:
            continue
        if len(excerpt) > 1_200:
            continue
        # A digest covers even omitted bodies, invalidating consumers on any edit.
        sources.append("# Resolved source " + json.dumps({
            "path": row["path"], "symbol": row["qualified_name"],
            "symbol_sha256": hashlib.sha256(encoded[start:end]).hexdigest(), "excerpt": excerpt,
            "complete_symbol": int(excerpt_end) == end,
        }, ensure_ascii=True))
        if not include_inferred or row["analysis_status"] != "completed" or row["analysis_source_sha256"] != digest:
            continue
        try:
            response = json.loads(row["response_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(response, dict) or response.get("review_status") != "complete" or response.get("analysis_method") == "fallback":
            continue
        # Persisted model contracts remain explicitly provisional. Never send issue
        # prose as an instruction or promote a previous model claim to engine proof.
        contract = {key: response.get(key) for key in ("parameters", "returns", "raised_errors", "side_effects")}
        payload = {
            "path": row["path"], "symbol": row["qualified_name"], "contract": contract,
        }
        # Descriptions can crowd out all of a useful large contract. Retain its
        # structured fields and explicitly signal any compacting of the payload.
        compact = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        if len(compact) > 2_000:
            contract["parameters"] = [
                {key: parameter[key] for key in ("name", "kind", "required", "accepted_types") if key in parameter}
                for parameter in (response.get("parameters") or []) if isinstance(parameter, dict)
            ]
            returns = response.get("returns")
            if isinstance(returns, dict):
                contract["returns"] = {key: returns[key] for key in ("may_return_value", "nullable") if key in returns}
                contract["returns"]["possible_types"] = [
                    item["type"] for item in returns.get("possible_types", []) if isinstance(item, dict) and "type" in item
                ]
            payload["descriptions_omitted"] = True
            compact = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        if len(compact) <= 4_000:
            contracts.append("# Inferred callee contract (hypothesis; verify against source) " + compact)
    # Give each compact contract a chance before large source excerpts consume
    # the shared dependency allowance. Every contract remains provisional.
    return contracts + sources


def python_control_flow_items(source: str, start_line: int) -> list[str]:
    """Extract bounded branch/handler locations without executing uploaded code."""
    fragment = textwrap.dedent(source)
    try:
        module = ast.parse(fragment)
    except (SyntaxError, ValueError):
        return []
    function = next((node for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
    if function is None:
        return []
    events = []

    def excerpt(node):
        return (ast.get_source_segment(fragment, node) or "")[:320]

    def visit(statements, path):
        for statement in statements:
            if len(events) >= 32 or len(path) > 8:
                return
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(statement, ast.If):
                visit(statement.body, [*path, "if " + excerpt(statement.test)])
                visit(statement.orelse, [*path, "else of if " + excerpt(statement.test)])
            elif isinstance(statement, (ast.Try, ast.TryStar)):
                visit(statement.body, [*path, "try body"])
                for handler in statement.handlers:
                    visit(handler.body, [*path, "except " + (excerpt(handler.type) if handler.type else "all exceptions")])
                visit(statement.orelse, [*path, "try else (no exception)"])
                visit(statement.finalbody, [*path, "finally"])
            elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                test = statement.test if isinstance(statement, ast.While) else statement.iter
                visit(statement.body, [*path, "loop over/while " + excerpt(test)])
                visit(statement.orelse, [*path, "loop else (no break)"])
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                visit(statement.body, [*path, "with " + ", ".join(excerpt(item.context_expr) for item in statement.items)])
            elif isinstance(statement, ast.Match):
                for case in statement.cases:
                    condition = "case " + excerpt(case.pattern)
                    if case.guard:
                        condition += " if " + excerpt(case.guard)
                    visit(case.body, [*path, condition])
            elif isinstance(statement, (ast.Return, ast.Raise, ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign)):
                # Record calls as source observations, not claims of known side effects.
                kind = "return" if isinstance(statement, ast.Return) else "raise" if isinstance(statement, ast.Raise) else "statement"
                events.append("# Source control-flow fact " + json.dumps({
                    "relative_line": statement.lineno, "kind": kind,
                    "enclosing_blocks": path, "source": excerpt(statement),
                }, ensure_ascii=True, separators=(",", ":")))
    visit(function.body, [])
    return (["# Partial source control-flow map; relative_line counts from the function's first line as 1. Omitted statements and paths remain unreviewed by this map.", *events] if events else [])


def file_dependency_items(db: sqlite3.Connection, project_id: str, file_id: int, source: str) -> list[str]:
    encoded = source.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    items = []
    for row in db.execute(
            "SELECT module_name, resolution_status, start_byte, end_byte, source_sha256 "
            "FROM project_dependencies WHERE project_id = ? AND file_id = ? ORDER BY start_byte LIMIT 40",
            (project_id, file_id),
    ):
        start, end = int(row["start_byte"]), int(row["end_byte"])
        if row["source_sha256"] != digest or not 0 <= start < end <= len(encoded) or end - start > 800:
            continue
        items.append("# File dependency " + json.dumps({
            "module": row["module_name"], "resolution": row["resolution_status"],
            "source": encoded[start:end].decode("utf-8"),
        }, ensure_ascii=True))
    return items
