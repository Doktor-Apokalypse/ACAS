"""Source excerpts shown by the project function tree."""

from __future__ import annotations

import ast
import re
import textwrap
from collections.abc import Mapping
from functools import lru_cache
from typing import Any


MAX_SOURCE_EXCERPT_CHARS = 500


def compact_source_excerpt(source: bytes, start_byte: int, end_byte: int) -> str:
    """Return one safe, compact UTF-8 excerpt from a stored source range."""
    if start_byte < 0 or end_byte < start_byte or start_byte >= len(source):
        return ""
    text = source[start_byte:min(end_byte, len(source))].decode("utf-8", errors="replace")
    compact = " ".join(text.split())
    if len(compact) <= MAX_SOURCE_EXCERPT_CHARS:
        return compact
    return compact[: MAX_SOURCE_EXCERPT_CHARS - 3].rstrip() + "..."


class _ReturnCollector(ast.NodeVisitor):
    """Collect returns belonging to one function while excluding nested callables."""

    def __init__(self) -> None:
        self.nodes: list[ast.Return] = []

    def visit_Return(self, node: ast.Return) -> None:  # noqa: N802 - ast visitor API
        self.nodes.append(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        return

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        return


def _python_function_metadata(source: str, start_line: int) -> tuple[str, list[dict[str, Any]]]:
    dedented = textwrap.dedent(source)
    try:
        module = ast.parse(dedented)
    except SyntaxError:
        return "", []
    function = next(
        (
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ),
        None,
    )
    if function is None:
        return "", []
    prefix = "async def" if isinstance(function, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(function.returns)}" if function.returns is not None else ""
    header = f"{prefix} {function.name}({ast.unparse(function.args)}){returns}:"
    collector = _ReturnCollector()
    for statement in function.body:
        collector.visit(statement)
    values: list[dict[str, Any]] = []
    flow_dependent = len(collector.nodes) > 1
    for node in collector.nodes:
        statement = ast.get_source_segment(dedented, node) or "return"
        values.append(
            {
                "line": start_line + node.lineno - 1,
                "code": _compact_text(statement),
                "flow_dependent": flow_dependent,
            }
        )
    return header, values


def _compact_text(value: str) -> str:
    compact = " ".join(value.split())
    if len(compact) <= MAX_SOURCE_EXCERPT_CHARS:
        return compact
    return compact[: MAX_SOURCE_EXCERPT_CHARS - 3].rstrip() + "..."


def _generic_function_metadata(source: str, start_line: int) -> tuple[str, list[dict[str, Any]]]:
    lines = source.splitlines()
    header_parts: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        header_parts.append(stripped)
        if "{" in stripped or stripped.endswith((':', '=>')):
            break
        if len(header_parts) >= 8:
            break
    header = _compact_text(" ".join(header_parts))
    if "{" in header:
        header = header.split("{", 1)[0].rstrip() + " {"
    return_matches: list[tuple[int, str]] = []
    for offset, line in enumerate(lines):
        for match in re.finditer(r"\breturn\b[^;\r\n]*;?", line):
            return_matches.append(
                (start_line + offset, _compact_text(match.group(0)))
            )
    flow_dependent = len(return_matches) > 1
    returns = [
        {"line": line, "code": code, "flow_dependent": flow_dependent}
        for line, code in return_matches
    ]
    return header, returns


@lru_cache(maxsize=32_768)
def _cached_function_source_metadata(
    source: str,
    language: str,
    start_line: int,
) -> tuple[str, tuple[tuple[int, str, bool], ...]]:
    if language == "python":
        header, return_lines = _python_function_metadata(source, start_line)
    else:
        header, return_lines = _generic_function_metadata(source, start_line)
    packed = tuple(
        (int(item["line"]), str(item["code"]), bool(item["flow_dependent"]))
        for item in return_lines
    )
    return header, packed


def function_source_metadata(row: Mapping[str, Any]) -> dict[str, object]:
    """Build a declaration header and owned return statements for one symbol row."""
    content = bytes(row["content"])
    start_byte = int(row["start_byte"])
    end_byte = int(row["end_byte"])
    if start_byte < 0 or end_byte < start_byte or start_byte >= len(content):
        return {"header": "", "return_lines": []}
    source = content[start_byte:min(end_byte, len(content))].decode("utf-8", errors="replace")
    header, packed = _cached_function_source_metadata(
        source,
        str(row.get("language") or "").lower(),
        int(row["start_line"]),
    )
    return {
        "header": header,
        "return_lines": [
            {"line": line, "code": code, "flow_dependent": flow_dependent}
            for line, code, flow_dependent in packed
        ],
    }
