"""Source excerpts shown by the project function tree."""

from __future__ import annotations

import ast
import re
import textwrap
from collections.abc import Mapping
from functools import lru_cache
from typing import Any


MAX_SOURCE_EXCERPT_CHARS = 500


_BEHAVIOR_VERBS = (
    r"branches|calls|controls flow|returns|updates state|transforms data|validates state"
)
_RETURN_CONTRACT_SUFFIX = re.compile(
    r"\s*The engine (?:return contract is .+|found no value-returning contract)\.\s*$",
    re.IGNORECASE,
)


def function_summary_description(summary: str | None, qualified_name: str | None) -> str:
    """Return tooltip prose without engine bookkeeping about source lines or contracts."""
    compact = " ".join(str(summary or "").split())
    compact = _RETURN_CONTRACT_SUFFIX.sub("", compact)
    selected_name = str(qualified_name or "")
    if selected_name:
        behavior_prefix = re.compile(
            rf"^{re.escape(selected_name)}\s+({_BEHAVIOR_VERBS})\s+at line \d+ using `[^`]*`"
            rf"(;\s*({_BEHAVIOR_VERBS})\s+at line \d+ using `[^`]*`)*\.\s*",
            re.IGNORECASE,
        )
        compact = behavior_prefix.sub("", compact).strip()
    if compact:
        return compact

    name = selected_name.rsplit(".", 1)[-1]
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).strip("_").split("_")
    if not words:
        return ""
    verbs = {
        "build": "Builds", "calculate": "Calculates", "check": "Checks",
        "create": "Creates", "delete": "Deletes", "fetch": "Fetches",
        "find": "Finds", "format": "Formats", "get": "Gets", "load": "Loads",
        "parse": "Parses", "read": "Reads", "render": "Formats", "request": "Requests",
        "save": "Saves", "set": "Sets", "update": "Updates", "validate": "Validates",
        "write": "Writes",
    }
    verb = verbs.get(words[0].casefold())
    if verb is None or len(words) == 1:
        return ""
    return f"{verb} the {' '.join(word.casefold() for word in words[1:])}."


def merge_analyzed_return_types(
    return_lines: list[dict[str, Any]],
    possible_types: list[str],
) -> list[dict[str, Any]]:
    """Use analysed contract types where source syntax cannot identify a return precisely."""
    candidates = [value.strip() for value in possible_types if value.strip()]
    unresolved = []
    for item in return_lines:
        return_type = item.get("return_type")
        if return_type == "unknown" or (isinstance(return_type, str) and " | " in return_type):
            unresolved.append(item)
    if len(candidates) == 1:
        for item in unresolved:
            item["return_type"] = candidates[0]
    elif len(candidates) == len(unresolved):
        for item, return_type in zip(unresolved, candidates, strict=True):
            item["return_type"] = return_type
    return return_lines


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


class _AnnotationCollector(ast.NodeVisitor):
    """Collect annotated local names while excluding nested callable scopes."""

    def __init__(self) -> None:
        self.known_types: dict[str, str] = {}

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        if isinstance(node.target, ast.Name):
            self.known_types[node.target.id] = _annotation_type(node.annotation)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        return

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        return


def _annotation_type(annotation: ast.expr | None) -> str:
    if annotation is None:
        return "unknown"
    value = ast.unparse(annotation)
    return "null" if value in {"None", "NoneType"} else value


def _python_expression_type(
    expression: ast.expr | None,
    known_types: Mapping[str, str],
    declared_type: str,
) -> str:
    if expression is None or (isinstance(expression, ast.Constant) and expression.value is None):
        return "null"
    if isinstance(expression, ast.Constant):
        names = {
            bool: "bool", int: "int", float: "float", complex: "complex",
            str: "str", bytes: "bytes",
        }
        return names.get(type(expression.value)) or type(expression.value).__name__ or "unknown"
    if isinstance(expression, ast.JoinedStr):
        return "str"
    if isinstance(expression, ast.List | ast.ListComp):
        return "list"
    if isinstance(expression, ast.Dict | ast.DictComp):
        return "dict"
    if isinstance(expression, ast.Set | ast.SetComp):
        return "set"
    if isinstance(expression, ast.Tuple):
        return "tuple"
    if isinstance(expression, ast.Compare):
        return "bool"
    if isinstance(expression, ast.Name):
        inferred = known_types.get(expression.id)
        if inferred:
            return inferred
    if isinstance(expression, ast.Call):
        if isinstance(expression.func, ast.Name) and expression.func.id in {
            "bool", "bytes", "dict", "float", "frozenset", "int", "list", "set", "str", "tuple",
        }:
            return expression.func.id
        if isinstance(expression.func, ast.Attribute):
            if expression.func.attr in {
                "capitalize", "casefold", "format", "join", "lower", "lstrip", "removeprefix",
                "removesuffix", "replace", "rstrip", "strip", "swapcase", "title", "upper",
            }:
                return "str"
            if expression.func.attr in {"rsplit", "split", "splitlines"}:
                return "list[str]"
    if declared_type != "unknown":
        optional_inner = (
            declared_type[len("Optional["):-1]
            if declared_type.startswith("Optional[") and declared_type.endswith("]")
            else declared_type
        )
        non_null = [
            value.strip()
            for value in re.split(r"\s*\|\s*", optional_inner)
            if value.strip() not in {"None", "NoneType", "null"}
        ]
        if len(non_null) == 1:
            return non_null[0]
        return declared_type.replace("None", "null")
    return "unknown"


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
    declared_type = _annotation_type(function.returns)
    arguments = (
        *function.args.posonlyargs,
        *function.args.args,
        *function.args.kwonlyargs,
    )
    known_types = {
        argument.arg: _annotation_type(argument.annotation)
        for argument in arguments
        if argument.annotation is not None
    }
    if function.args.vararg and function.args.vararg.annotation is not None:
        known_types[function.args.vararg.arg] = _annotation_type(function.args.vararg.annotation)
    if function.args.kwarg and function.args.kwarg.annotation is not None:
        known_types[function.args.kwarg.arg] = _annotation_type(function.args.kwarg.annotation)
    annotation_collector = _AnnotationCollector()
    for statement in function.body:
        annotation_collector.visit(statement)
    known_types.update(annotation_collector.known_types)
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
                "return_type": _python_expression_type(node.value, known_types, declared_type),
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
        {
            "line": line,
            "code": code,
            "return_type": _generic_return_type(code, header),
            "flow_dependent": flow_dependent,
        }
        for line, code in return_matches
    ]
    return header, returns


def _generic_return_type(code: str, header: str) -> str:
    expression = re.sub(r"^return\s*", "", code, flags=re.IGNORECASE).rstrip("; ").strip()
    if not expression or expression.casefold() in {"null", "nil", "none", "nullptr", "undefined"}:
        return "null"
    if re.fullmatch(r"[-+]?\d+", expression):
        return "int"
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)", expression):
        return "float"
    if expression.casefold() in {"true", "false"}:
        return "bool"
    if (
        len(expression) >= 2
        and expression[0] == expression[-1]
        and expression[0] in {'"', "'", "`"}
    ):
        return "str"
    declaration = re.match(
        r"\s*([A-Za-z_][\w:<>,.?\[\] ]*)\s+[A-Za-z_$][\w$]*\s*\(",
        header,
    )
    return declaration.group(1).strip() if declaration else "unknown"


@lru_cache(maxsize=32_768)
def _cached_function_source_metadata(
    source: str,
    language: str,
    start_line: int,
) -> tuple[str, tuple[tuple[int, str, str, bool], ...]]:
    if language == "python":
        header, return_lines = _python_function_metadata(source, start_line)
    else:
        header, return_lines = _generic_function_metadata(source, start_line)
    packed = tuple(
        (
            int(item["line"]),
            str(item["code"]),
            str(item["return_type"]),
            bool(item["flow_dependent"]),
        )
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
            {
                "line": line,
                "code": code,
                "return_type": return_type,
                "flow_dependent": flow_dependent,
            }
            for line, code, return_type, flow_dependent in packed
        ],
    }
