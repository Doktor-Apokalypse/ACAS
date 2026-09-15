"""Deterministic checks between indexed calls and stored Ollama function contracts."""

from __future__ import annotations

import ast
import builtins
import json
import re
import sqlite3
import textwrap
from dataclasses import dataclass

from project_structure import resolve_project_calls


@dataclass(frozen=True)
class CompatibilitySummary:
    status: str
    checked_count: int
    incompatible_count: int
    unknown_count: int
    not_checked_count: int = 0


@dataclass(frozen=True)
class Finding:
    severity: str
    kind: str
    message: str
    argument_ordinal: int | None = None
    expected_types: tuple[str, ...] = ()
    actual_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class _PythonCallScopeFacts:
    bound_names_by_symbol: dict[int, frozenset[str]]
    parent_by_symbol: dict[int, int | None]
    kind_by_symbol: dict[int, str]
    external_base_class_ids: frozenset[int]


_ALIASES = {
    "integer": "int",
    "i8": "int",
    "i16": "int",
    "i32": "int",
    "i64": "int",
    "u8": "int",
    "u16": "int",
    "u32": "int",
    "u64": "int",
    "double": "float",
    "decimal": "float",
    "number": "number",
    "str": "string",
    "std::string": "string",
    "char*": "string",
    "boolean": "bool",
    "none": "null",
    "nullptr": "null",
    "nil": "null",
    "vector": "sequence",
    "array": "sequence",
    "htmlresponse": "response",
    "jsonresponse": "response",
    "redirectresponse": "response",
    "streamingresponse": "response",
    "fileresponse": "response",
    "plaintextresponse": "response",
    "starlette.responses.htmlresponse": "response",
    "starlette.responses.jsonresponse": "response",
    "starlette.responses.redirectresponse": "response",
    "fastapi.responses.htmlresponse": "response",
    "fastapi.responses.jsonresponse": "response",
    "fastapi.responses.redirectresponse": "response",
}


@dataclass(frozen=True)
class _TypeShape:
    base: str
    arguments: tuple[_TypeShape, ...] = ()
    variadic: bool = False


_WILDCARD_TYPES = {"any", "object", "unknown", "dynamic"}
_CONTAINER_SUPERTYPES = {
    "list": {"sequence", "iterable", "collection"},
    "tuple": {"sequence", "iterable", "collection"},
    "set": {"iterable", "collection"},
    "frozenset": {"iterable", "collection"},
    "dict": {"mapping", "iterable", "collection"},
    "string": {"sequence", "iterable", "collection"},
    "bytes": {"sequence", "iterable", "collection"},
}
_PYTHON_AST_NODE_TYPES = {
    name.casefold()
    for name, value in vars(ast).items()
    if isinstance(value, type) and issubclass(value, ast.AST)
}


def _split_top_level(value: str, separator: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote = ""
    escaped = False
    for index, character in enumerate(value):
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character in {"'", '"'}:
            quote = character
        elif character in "[<(":
            depth += 1
        elif character in "]>)":
            depth = max(0, depth - 1)
        elif character == separator and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    parts.append(value[start:].strip())
    return [part for part in parts if part]


def _normalized_base(value: str) -> str:
    base = value.strip().casefold()
    base = re.sub(r"\b(const|mutable|readonly|ref|out|in)\b", "", base).strip()
    base = base.rsplit(".", 1)[-1]
    return _ALIASES.get(base, base)


def _literal_shape(value: str) -> _TypeShape:
    candidate = value.strip()
    if (candidate.startswith("'") and candidate.endswith("'")) or (
        candidate.startswith('"') and candidate.endswith('"')
    ):
        return _TypeShape("string")
    folded = candidate.casefold()
    if folded in {"true", "false"}:
        return _TypeShape("bool")
    if folded in {"none", "null", "nil", "nullptr"}:
        return _TypeShape("null")
    if re.fullmatch(r"[+-]?\d+", candidate):
        return _TypeShape("int")
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\d*\.\d+)", candidate):
        return _TypeShape("float")
    return _TypeShape(_normalized_base(candidate))


def _type_shapes(type_name: str) -> tuple[_TypeShape, ...]:
    value = type_name.strip()
    if not value:
        return ()
    # Forward references and postponed annotations may quote the complete type
    # expression. Interpret that expression as a type here, rather than as a
    # runtime string literal.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        try:
            decoded = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            decoded = None
        if isinstance(decoded, str):
            value = decoded.strip()
    if re.match(r"(?i)^object\s+with\s+attributes?\b", value):
        return (_TypeShape("object"),)
    value = re.sub(r"(?i)\b(const|mutable|readonly|ref|out|in)\b", "", value).strip()
    optional_suffix = value.endswith("?")
    if optional_suffix:
        value = value[:-1].strip()

    union_parts = _split_top_level(value, "|")
    if len(union_parts) > 1:
        shapes = tuple(
            shape
            for part in union_parts
            for shape in _type_shapes(part)
        )
        return (*shapes, _TypeShape("null")) if optional_suffix else shapes

    generic_match = re.fullmatch(r"(.+?)[\[<](.*)[\]>]", value)
    if generic_match:
        base = _normalized_base(generic_match.group(1))
        body = generic_match.group(2).strip()
        parts = _split_top_level(body, ",")
        if base == "optional":
            shapes = tuple(shape for part in parts for shape in _type_shapes(part))
            return (*shapes, _TypeShape("null"))
        if base == "union":
            shapes = tuple(shape for part in parts for shape in _type_shapes(part))
            return (*shapes, _TypeShape("null")) if optional_suffix else shapes
        if base == "literal":
            shapes = tuple(_literal_shape(part) for part in parts)
            return (*shapes, _TypeShape("null")) if optional_suffix else shapes
        variadic = bool(parts and parts[-1] == "...")
        if variadic:
            parts = parts[:-1]
        parsed_arguments = [_type_shapes(part) for part in parts]
        arguments = tuple(
            shapes[0] if len(shapes) == 1 else _TypeShape("unknown")
            for shapes in parsed_arguments
            if shapes
        )
        shape = _TypeShape(base, arguments, variadic)
    else:
        shape = _literal_shape(value)
    return (shape, _TypeShape("null")) if optional_suffix else (shape,)


def normalize_type(type_name: str) -> str:
    """Return the normalized outer type without confusing generic ellipses for modules."""
    shapes = _type_shapes(type_name)
    return shapes[0].base if shapes else ""


def _combine_compatibility(results: list[bool | None]) -> bool | None:
    if any(result is False for result in results):
        return False
    if any(result is None for result in results):
        return None
    return True


def _container_arguments_compatible(
    actual: _TypeShape,
    expected: _TypeShape,
) -> bool | None:
    if not expected.arguments:
        return True
    if not actual.arguments:
        return None
    if expected.base in {"sequence", "iterable", "collection"}:
        expected_item = expected.arguments[0]
        actual_items = (
            actual.arguments
            if actual.base == "tuple"
            else actual.arguments[:1]
        )
        return _combine_compatibility(
            [_shape_compatible(item, expected_item) for item in actual_items]
        )
    if actual.base == expected.base == "tuple":
        if expected.variadic and len(expected.arguments) == 1:
            return _combine_compatibility(
                [
                    _shape_compatible(item, expected.arguments[0])
                    for item in actual.arguments
                ]
            )
        if actual.variadic and not expected.variadic:
            return None
        if len(actual.arguments) != len(expected.arguments):
            return False
    if len(actual.arguments) != len(expected.arguments):
        return None
    return _combine_compatibility(
        [
            _shape_compatible(actual_item, expected_item)
            for actual_item, expected_item in zip(
                actual.arguments,
                expected.arguments,
                strict=True,
            )
        ]
    )


def _shape_compatible(actual: _TypeShape, expected: _TypeShape) -> bool | None:
    if expected.base in _WILDCARD_TYPES:
        return True
    if actual.base in _WILDCARD_TYPES:
        return None
    if actual.base == expected.base:
        return _container_arguments_compatible(actual, expected)
    if actual.base == "int" and expected.base in {"float", "number"}:
        return True
    if actual.base in {"int", "float"} and expected.base == "number":
        return True
    if expected.base == "ast" and actual.base in _PYTHON_AST_NODE_TYPES:
        return True
    if expected.base in _CONTAINER_SUPERTYPES.get(actual.base, set()):
        return _container_arguments_compatible(actual, expected)
    return False


def types_compatible(actual: tuple[str, ...], expected: tuple[str, ...]) -> bool | None:
    if not actual or not expected:
        return None
    actual_shapes = tuple(shape for item in actual for shape in _type_shapes(item))
    expected_shapes = tuple(shape for item in expected for shape in _type_shapes(item))
    if not actual_shapes or not expected_shapes:
        return None
    results = [
        _shape_compatible(actual_shape, expected_shape)
        for actual_shape in actual_shapes
        for expected_shape in expected_shapes
    ]
    if any(result is True for result in results):
        return True
    if any(result is None for result in results):
        return None
    return False


def _json_types(value: object) -> tuple[str, ...]:
    try:
        decoded = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return ()
    return tuple(str(item) for item in decoded) if isinstance(decoded, list) else ()


_PYTHON_BUILTINS = set(dir(builtins))
_DYNAMIC_CALLEE_PATTERN = re.compile(r"[()\[\]{}]|(?:^|\s)(?:lambda|await)\b|[+\-*/%]")
_IDENTIFIER_TARGET_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:(?:\.|::|->)[A-Za-z_][A-Za-z0-9_]*)*$"
)


def _call_simple_name(callee: str) -> str:
    return callee.strip().replace("->", ".").replace("::", ".").rsplit(".", 1)[-1]


def _call_qualifier(callee: str) -> str:
    normalized = callee.strip().replace("->", ".").replace("::", ".")
    return normalized.split(".", 1)[0]


def _is_dynamic_call_target(callee: str) -> bool:
    normalized = callee.strip()
    if not normalized:
        return True
    return bool(_DYNAMIC_CALLEE_PATTERN.search(normalized)) or not bool(
        _IDENTIFIER_TARGET_PATTERN.match(normalized)
    )


def _has_external_dependency_for_call(db: sqlite3.Connection, call: sqlite3.Row) -> bool:
    callee = str(call["callee"])
    simple_name = _call_simple_name(callee)
    qualifier = _call_qualifier(callee)
    dependencies = db.execute(
        """
        SELECT dependency_kind, module_name, imported_names_json
        FROM project_dependencies
        WHERE file_id = ? AND resolution_status = 'external'
        """,
        (int(call["file_id"]),),
    ).fetchall()
    for dependency in dependencies:
        imported_names = set(_json_types(dependency["imported_names_json"]))
        module_name = str(dependency["module_name"])
        module_leaf = module_name.lstrip(".").replace("::", ".").rsplit(".", 1)[-1]
        if (
            simple_name in imported_names
            or qualifier in imported_names
            or qualifier == module_leaf
            or simple_name == module_leaf
        ):
            return True
    return False


def _call_is_caller_parameter(
    db: sqlite3.Connection,
    call: sqlite3.Row,
    simple_name: str,
) -> bool:
    caller_symbol_id = call["caller_symbol_id"]
    if caller_symbol_id is None:
        return False
    symbol_id: int | None = int(caller_symbol_id)
    while symbol_id is not None:
        if db.execute(
            """
            SELECT 1 FROM project_symbol_parameters
            WHERE symbol_id = ? AND name = ?
            LIMIT 1
            """,
            (symbol_id, simple_name),
        ).fetchone() is not None:
            return True
        parent = db.execute(
            "SELECT parent_symbol_id FROM project_symbols WHERE id = ?",
            (symbol_id,),
        ).fetchone()
        symbol_id = (
            int(parent["parent_symbol_id"])
            if parent is not None and parent["parent_symbol_id"] is not None
            else None
        )
    return False


def _python_scope_bound_names(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> frozenset[str]:
    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.bound: set[str] = set()
            self.excluded: set[str] = set()

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Store):
                self.bound.add(node.id)

        def visit_arg(self, node: ast.arg) -> None:
            self.bound.add(node.arg)

        def visit_Import(self, node: ast.Import) -> None:
            self.bound.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            self.bound.update(alias.asname or alias.name for alias in node.names if alias.name != "*")

        def visit_Global(self, node: ast.Global) -> None:
            self.excluded.update(node.names)

        def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
            self.excluded.update(node.names)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.bound.add(node.name)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.bound.add(node.name)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.bound.add(node.name)

        def visit_Lambda(self, _node: ast.Lambda) -> None:
            return

    visitor = Visitor()
    for argument in (
        *function.args.posonlyargs,
        *function.args.args,
        *function.args.kwonlyargs,
    ):
        visitor.visit(argument)
    if function.args.vararg is not None:
        visitor.visit(function.args.vararg)
    if function.args.kwarg is not None:
        visitor.visit(function.args.kwarg)
    for statement in function.body:
        visitor.visit(statement)
    return frozenset(visitor.bound - visitor.excluded)


def _python_node_qualified_name(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    parents: dict[ast.AST, ast.AST],
) -> str:
    names = [node.name]
    current: ast.AST = node
    while (parent := parents.get(current)) is not None:
        if isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.append(parent.name)
        current = parent
    return ".".join(reversed(names))


def _source_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, memoryview):
        return value.tobytes().decode("utf-8", errors="replace")
    return str(value or "")


def _python_call_scope_facts(
    db: sqlite3.Connection,
    project_id: str,
) -> _PythonCallScopeFacts:
    symbols = db.execute(
        """
        SELECT id, file_id, parent_symbol_id, symbol_kind, qualified_name
        FROM project_symbols WHERE project_id = ?
        """,
        (project_id,),
    ).fetchall()
    symbol_by_location = {
        (int(row["file_id"]), str(row["qualified_name"])): int(row["id"])
        for row in symbols
    }
    parent_by_symbol = {
        int(row["id"]): (
            int(row["parent_symbol_id"])
            if row["parent_symbol_id"] is not None
            else None
        )
        for row in symbols
    }
    kind_by_symbol = {int(row["id"]): str(row["symbol_kind"]) for row in symbols}
    local_class_names = {
        str(row["qualified_name"]).rsplit(".", 1)[-1]
        for row in symbols
        if str(row["symbol_kind"]) == "class"
    }
    external_names_by_file: dict[int, set[str]] = {}
    for row in db.execute(
        """
        SELECT file_id, module_name, imported_names_json
        FROM project_dependencies
        WHERE project_id = ? AND resolution_status = 'external'
        """,
        (project_id,),
    ).fetchall():
        module = str(row["module_name"]).lstrip(".")
        names = external_names_by_file.setdefault(int(row["file_id"]), set())
        if module:
            names.update((module.split(".", 1)[0], module.rsplit(".", 1)[-1]))
        names.update(_json_types(row["imported_names_json"]))

    bound_names_by_symbol: dict[int, frozenset[str]] = {}
    external_base_class_ids: set[int] = set()
    files = db.execute(
        """
        SELECT id, content FROM project_files
        WHERE project_id = ? AND language = 'python' AND is_binary = 0
        """,
        (project_id,),
    ).fetchall()
    for file_row in files:
        file_id = int(file_row["id"])
        try:
            module = ast.parse(_source_text(file_row["content"]))
        except SyntaxError:
            continue
        parents = {
            child: parent
            for parent in ast.walk(module)
            for child in ast.iter_child_nodes(parent)
        }
        own_bindings = {
            node: _python_scope_bound_names(node)
            for node in ast.walk(module)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        for node, own in own_bindings.items():
            available = set(own)
            current: ast.AST = node
            while (parent := parents.get(current)) is not None:
                if isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef):
                    available.update(own_bindings.get(parent, ()))
                current = parent
            qualified_name = _python_node_qualified_name(node, parents)
            symbol_id = symbol_by_location.get((file_id, qualified_name))
            if symbol_id is not None:
                bound_names_by_symbol[symbol_id] = frozenset(available)

        external_names = external_names_by_file.get(file_id, set())
        for node in ast.walk(module):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                try:
                    base_name = ast.unparse(base)
                except Exception:
                    continue
                root = base_name.split(".", 1)[0]
                leaf = base_name.rsplit(".", 1)[-1]
                if leaf in local_class_names or not (
                    root in external_names or leaf in external_names
                ):
                    continue
                qualified_name = _python_node_qualified_name(node, parents)
                symbol_id = symbol_by_location.get((file_id, qualified_name))
                if symbol_id is not None:
                    external_base_class_ids.add(symbol_id)
                break
    return _PythonCallScopeFacts(
        bound_names_by_symbol=bound_names_by_symbol,
        parent_by_symbol=parent_by_symbol,
        kind_by_symbol=kind_by_symbol,
        external_base_class_ids=frozenset(external_base_class_ids),
    )


def _caller_has_external_base(
    call: sqlite3.Row,
    facts: _PythonCallScopeFacts,
) -> bool:
    symbol_id = int(call["caller_symbol_id"]) if call["caller_symbol_id"] is not None else None
    while symbol_id is not None:
        if (
            facts.kind_by_symbol.get(symbol_id) == "class"
            and symbol_id in facts.external_base_class_ids
        ):
            return True
        symbol_id = facts.parent_by_symbol.get(symbol_id)
    return False


def _call_matches_unresolved_dependency(
    db: sqlite3.Connection,
    call: sqlite3.Row,
) -> bool:
    """Retain calls through unresolved project imports as actionable local unknowns."""
    callee = str(call["callee"])
    qualifier = _call_qualifier(callee)
    simple_name = _call_simple_name(callee)
    for dependency in db.execute(
        """
        SELECT module_name, imported_names_json
        FROM project_dependencies
        WHERE file_id = ? AND resolution_status IN ('unresolved', 'ambiguous')
        """,
        (int(call["file_id"]),),
    ).fetchall():
        imported_names = set(_json_types(dependency["imported_names_json"]))
        module_leaf = str(dependency["module_name"]).lstrip(".").rsplit(".", 1)[-1]
        if qualifier in imported_names or qualifier == module_leaf or simple_name in imported_names:
            return True
    return False


def _unresolved_call_outcome(
    db: sqlite3.Connection,
    call: sqlite3.Row,
    python_scope_facts: _PythonCallScopeFacts | None = None,
) -> tuple[str, Finding]:
    """Classify calls outside local contract coverage separately from unresolved project calls."""
    callee = str(call["callee"])
    resolution_status = str(call["resolution_status"])
    if resolution_status == "unresolved":
        language = str(call["language"] or "")
        simple_name = _call_simple_name(callee)
        if _is_dynamic_call_target(callee):
            return (
                "out_of_scope",
                Finding(
                    "info",
                    "dynamic_call",
                    f"Dynamic call / not checked: {callee}",
                ),
            )
        if language == "python" and "." not in callee and simple_name in _PYTHON_BUILTINS:
            return (
                "out_of_scope",
                Finding(
                    "info",
                    "builtin_call",
                    f"Builtin call / not checked: {callee}",
                ),
            )
        if _has_external_dependency_for_call(db, call):
            return (
                "out_of_scope",
                Finding(
                    "info",
                    "external_call",
                    f"External call / not checked: {callee}",
                ),
            )
        if (
            language == "python"
            and "." not in callee
            and _call_is_caller_parameter(db, call, simple_name)
        ):
            return (
                "out_of_scope",
                Finding(
                    "info",
                    "callable_parameter_call",
                    f"Callable parameter / not checked: {callee}",
                ),
            )
        if (
            language == "python"
            and "." not in callee
            and python_scope_facts is not None
            and call["caller_symbol_id"] is not None
            and simple_name in python_scope_facts.bound_names_by_symbol.get(
                int(call["caller_symbol_id"]),
                frozenset(),
            )
        ):
            return (
                "out_of_scope",
                Finding(
                    "info",
                    "dynamic_local_call",
                    f"Locally bound callable / not checked: {callee}",
                ),
            )
        normalized_callee = callee.replace("::", ".").replace("->", ".")
        if (
            language == "python"
            and normalized_callee.startswith(("self.", "cls."))
            and normalized_callee.count(".") >= 2
        ):
            return (
                "out_of_scope",
                Finding(
                    "info",
                    "dynamic_receiver_method_call",
                    f"Nested receiver method / not checked: {callee}",
                ),
            )
        if (
            language == "python"
            and normalized_callee.startswith(("self.", "cls."))
            and python_scope_facts is not None
            and _caller_has_external_base(call, python_scope_facts)
        ):
            return (
                "out_of_scope",
                Finding(
                    "info",
                    "external_inherited_method_call",
                    f"Externally inherited method / not checked: {callee}",
                ),
            )
        if (
            language == "python"
            and "." in callee
            and not callee.startswith(("self.", "cls."))
            and not _call_matches_unresolved_dependency(db, call)
        ):
            return (
                "out_of_scope",
                Finding(
                    "info",
                    "dynamic_method_call",
                    f"Dynamic receiver method / not checked: {callee}",
                ),
            )
        return (
            "in_scope",
            Finding(
                "info",
                "unresolved_internal_call",
                f"Unresolved internal call: {callee}",
            ),
        )
    if resolution_status == "external":
        return (
            "out_of_scope",
            Finding(
                "info",
                "external_call",
                f"External call / not checked: {callee}",
            ),
        )
    return (
        "in_scope",
        Finding(
            "info",
            "unresolved_call",
            f"Call target is {resolution_status}; no internal contract was selected.",
        ),
    )


def _check_arguments(
    call: sqlite3.Row,
    arguments: list[sqlite3.Row],
    parameters: list[sqlite3.Row],
) -> tuple[str, list[Finding]]:
    if str(call["detail_status"]) != "complete":
        return "unknown", [Finding("info", "missing_call_detail", "Call predates argument indexing.")]
    filtered_parameters = []
    for index, row in enumerate(parameters):
        if str(row["parameter_kind"]) == "receiver":
            continue
        if index == 0 and str(row["name"]) in {"self", "cls"}:
            continue
        filtered_parameters.append(row)
    parameters = filtered_parameters
    positional_parameters = [
        row
        for row in parameters
        if str(row["parameter_kind"]) in {"positional_only", "positional_or_keyword", "unknown"}
    ]
    keyword_parameters = {
        str(row["name"]): row
        for row in parameters
        if str(row["parameter_kind"]) in {"positional_or_keyword", "keyword_only", "unknown"}
    }
    has_varargs = any(str(row["parameter_kind"]) == "variadic_positional" for row in parameters)
    has_kwargs = any(str(row["parameter_kind"]) == "variadic_keyword" for row in parameters)
    findings: list[Finding] = []
    assigned: set[str] = set()
    unknown_types = False
    positional_index = 0

    for argument in arguments:
        ordinal = int(argument["ordinal"])
        expression_kind = str(argument["expression_kind"])
        if expression_kind in {"list_splat", "dictionary_splat", "spread_element"}:
            unknown_types = True
            continue
        keyword = str(argument["keyword_name"]) if argument["keyword_name"] is not None else None
        if keyword is None:
            if positional_index < len(positional_parameters):
                parameter = positional_parameters[positional_index]
                positional_index += 1
            elif has_varargs:
                unknown_types = True
                continue
            else:
                findings.append(
                    Finding("error", "unexpected_argument", "Too many positional arguments.", ordinal)
                )
                continue
        else:
            parameter = keyword_parameters.get(keyword)
            if parameter is None:
                if has_kwargs:
                    unknown_types = True
                    continue
                findings.append(
                    Finding("error", "unexpected_keyword", f"Unexpected keyword argument '{keyword}'.", ordinal)
                )
                continue
        parameter_name = str(parameter["name"])
        if parameter_name in assigned:
            findings.append(
                Finding("error", "duplicate_argument", f"Argument '{parameter_name}' is supplied more than once.", ordinal)
            )
            continue
        assigned.add(parameter_name)
        actual = _json_types(argument["inferred_types_json"])
        expected = _json_types(parameter["accepted_types_json"])
        compatible = types_compatible(actual, expected)
        if compatible is False:
            findings.append(
                Finding(
                    "error",
                    "argument_type",
                    f"Argument '{parameter_name}' has type {', '.join(actual)}; expected {', '.join(expected)}.",
                    ordinal,
                    expected,
                    actual,
                )
            )
        elif compatible is None:
            unknown_types = True

    splat_present = any(
        str(row["expression_kind"]) in {"list_splat", "dictionary_splat", "spread_element"}
        for row in arguments
    )
    if not splat_present:
        for parameter in parameters:
            kind = str(parameter["parameter_kind"])
            if kind.startswith("variadic") or not bool(parameter["required"]):
                continue
            name = str(parameter["name"])
            if name not in assigned:
                findings.append(
                    Finding("error", "missing_argument", f"Required argument '{name}' is missing.")
                )
    if any(item.severity == "error" for item in findings):
        return "incompatible", findings
    return ("unknown" if unknown_types else "compatible"), findings


def _check_return(
    call: sqlite3.Row,
    callee_analysis: sqlite3.Row,
    caller_return_types: tuple[str, ...],
    *,
    callee_is_async: bool = False,
    call_is_awaited: bool = False,
    source_usage_kind: str | None = None,
) -> tuple[str, list[Finding]]:
    usage = source_usage_kind or str(call["usage_kind"])
    # Calling an async function produces a coroutine object. Its eventual
    # return contract applies only once awaited; passing it to create_task or
    # gather is therefore a valid value use.
    if callee_is_async and not call_is_awaited:
        return "compatible", []
    if usage == "statement":
        return "compatible", []
    may_return = bool(callee_analysis["may_return_value"])
    if not may_return:
        return "incompatible", [
            Finding("error", "void_value_used", "A function that returns no value is used as a value.")
        ]
    if usage == "condition":
        return "compatible", []
    actual = _json_types(callee_analysis["return_types_json"])
    expected = _json_types(call["expected_return_types_json"])
    if usage == "return" and caller_return_types:
        expected = caller_return_types
    if not expected:
        return "compatible", []
    compatible = types_compatible(actual, expected)
    if compatible is True:
        return "compatible", []
    if compatible is None:
        return "unknown", []
    return "incompatible", [
        Finding(
            "error",
            "return_type",
            f"Returned type {', '.join(actual)} does not match expected {', '.join(expected)}.",
            expected_types=expected,
            actual_types=actual,
        )
    ]


def _python_async_symbol_ids(
    db: sqlite3.Connection,
    project_id: str,
) -> set[int]:
    """Return Python symbol ids whose indexed source is an async definition."""
    async_ids: set[int] = set()
    for row in db.execute(
        """
        SELECT symbol.id, symbol.start_byte, symbol.end_byte, file.content
        FROM project_symbols AS symbol
        JOIN project_files AS file ON file.id = symbol.file_id
        WHERE symbol.project_id = ? AND file.language = 'python'
          AND symbol.symbol_kind IN ('function', 'method')
        """,
        (project_id,),
    ).fetchall():
        try:
            content = bytes(row["content"])
            source = content[int(row["start_byte"]):int(row["end_byte"])].decode("utf-8")
            module = ast.parse(textwrap.dedent(source))
        except (SyntaxError, UnicodeDecodeError, TypeError, ValueError):
            continue
        if any(isinstance(node, ast.AsyncFunctionDef) for node in module.body):
            async_ids.add(int(row["id"]))
    return async_ids


def _python_call_source_contexts(
    db: sqlite3.Connection,
    project_id: str,
) -> dict[tuple[int, int, int], tuple[bool, str]]:
    """Return fresh await/value context for persisted Python call locations."""
    contexts: dict[tuple[int, int, int], tuple[bool, str]] = {}

    def usage_kind(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> tuple[bool, str]:
        parent = parents.get(call)
        awaited = isinstance(parent, ast.Await)
        if awaited:
            parent = parents.get(parent)
        if isinstance(parent, ast.Expr):
            return awaited, "statement"
        if isinstance(parent, ast.Return):
            return awaited, "return"
        if isinstance(parent, ast.Assign | ast.AnnAssign | ast.NamedExpr):
            return awaited, "assignment"
        if isinstance(parent, ast.keyword | ast.Call):
            return awaited, "argument"
        if isinstance(parent, ast.If | ast.While | ast.IfExp):
            return awaited, "condition"
        return awaited, "value"

    for row in db.execute(
        "SELECT id, content FROM project_files WHERE project_id = ? AND language = 'python'",
        (project_id,),
    ).fetchall():
        try:
            module = ast.parse(bytes(row["content"]).decode("utf-8"))
        except (SyntaxError, UnicodeDecodeError, TypeError):
            continue
        parents = {
            child: parent
            for parent in ast.walk(module)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            contexts[
                (int(row["id"]), int(node.lineno), int(node.col_offset) + 1)
            ] = usage_kind(node, parents)
    return contexts


def _check_constructor_return(
    call: sqlite3.Row,
    constructor_type: str,
    caller_return_types: tuple[str, ...],
) -> tuple[str, list[Finding]]:
    """Check the instance produced by a class call, not __init__'s None return."""
    usage = str(call["usage_kind"])
    if usage in {"statement", "condition"}:
        return "compatible", []
    expected = _json_types(call["expected_return_types_json"])
    if usage == "return" and caller_return_types:
        expected = caller_return_types
    if not expected or not constructor_type:
        return "compatible", []
    actual = (constructor_type,)
    compatible = types_compatible(actual, expected)
    if compatible is True:
        return "compatible", []
    if compatible is None:
        return "unknown", []
    return "incompatible", [
        Finding(
            "error",
            "return_type",
            f"Constructed type {constructor_type} does not match expected {', '.join(expected)}.",
            expected_types=expected,
            actual_types=actual,
        )
    ]


def check_project_call_compatibility(
    db: sqlite3.Connection,
    project_id: str,
) -> CompatibilitySummary:
    """Replace compatibility rows for a project using the latest stored contracts."""
    # Re-resolve persisted calls so analyzer improvements also apply to projects
    # imported before the current process started.
    resolve_project_calls(db, project_id)
    db.execute(
        """
        DELETE FROM project_call_findings
        WHERE call_id IN (
            SELECT id FROM project_calls WHERE project_id = ?
        )
        """,
        (project_id,),
    )
    db.execute("DELETE FROM project_call_compatibility WHERE project_id = ?", (project_id,))
    calls = db.execute(
        """
        SELECT call.*, file.language,
               caller_analysis.source_sha256 AS caller_analysis_sha256,
               callee_analysis.source_sha256 AS callee_analysis_sha256,
               callee_analysis.may_return_value,
               callee.symbol_kind AS callee_symbol_kind,
               callee.name AS callee_name,
               COALESCE((
                   SELECT json_group_array(type_name)
                   FROM project_symbol_return_types
                   WHERE symbol_id = call.resolved_symbol_id ORDER BY ordinal
               ), '[]') AS return_types_json
        FROM project_calls AS call
        JOIN project_files AS file ON file.id = call.file_id
        LEFT JOIN project_symbol_analyses AS caller_analysis
               ON caller_analysis.symbol_id = call.caller_symbol_id
        LEFT JOIN project_symbol_analyses AS callee_analysis
               ON callee_analysis.symbol_id = call.resolved_symbol_id
        LEFT JOIN project_symbols AS callee
               ON callee.id = call.resolved_symbol_id
        WHERE call.project_id = ? ORDER BY call.id
        """,
        (project_id,),
    ).fetchall()
    python_scope_facts = _python_call_scope_facts(db, project_id)
    python_async_symbol_ids = _python_async_symbol_ids(db, project_id)
    python_call_source_contexts = _python_call_source_contexts(db, project_id)
    incompatible_count = unknown_count = not_checked_count = 0
    for call in calls:
        call_id = int(call["id"])
        findings: list[Finding] = []
        scope_status = "in_scope"
        if str(call["resolution_status"]) != "internal" or call["resolved_symbol_id"] is None:
            argument_status = return_status = status = "unknown"
            scope_status, finding = _unresolved_call_outcome(
                db,
                call,
                python_scope_facts,
            )
            findings.append(finding)
        elif str(call["callee_symbol_kind"] or "") == "class":
            argument_status = return_status = status = "unknown"
            scope_status = "out_of_scope"
            findings.append(
                Finding(
                    "info",
                    "internal_constructor_call",
                    f"Generated or inherited constructor / not checked: {call['callee']}",
                )
            )
        elif call["callee_analysis_sha256"] is None:
            argument_status = return_status = status = "unknown"
            findings.append(
                Finding("info", "missing_contract", "The called function has no completed analysis contract.")
            )
        else:
            arguments = db.execute(
                "SELECT * FROM project_call_arguments WHERE call_id = ? ORDER BY ordinal",
                (call_id,),
            ).fetchall()
            parameters = db.execute(
                "SELECT * FROM project_symbol_parameters WHERE symbol_id = ? ORDER BY ordinal",
                (int(call["resolved_symbol_id"]),),
            ).fetchall()
            argument_status, argument_findings = _check_arguments(call, arguments, parameters)
            caller_return_types: tuple[str, ...] = ()
            if call["caller_symbol_id"] is not None:
                caller_return_types = tuple(
                    str(row["type_name"])
                    for row in db.execute(
                        "SELECT type_name FROM project_symbol_return_types WHERE symbol_id = ? ORDER BY ordinal",
                        (int(call["caller_symbol_id"]),),
                    ).fetchall()
                )
            if (
                str(call["callee_name"] or "") in {"__init__", "__new__"}
                and _call_simple_name(str(call["callee"]))
                not in {"__init__", "__new__"}
            ):
                return_status, return_findings = _check_constructor_return(
                    call,
                    _call_simple_name(str(call["callee"])),
                    caller_return_types,
                )
            else:
                source_call_context = python_call_source_contexts.get(
                    (
                        int(call["file_id"]),
                        int(call["start_line"]),
                        int(call["start_column"]),
                    )
                )
                return_status, return_findings = _check_return(
                    call,
                    call,
                    caller_return_types,
                    callee_is_async=int(call["resolved_symbol_id"]) in python_async_symbol_ids,
                    call_is_awaited=bool(
                        source_call_context and source_call_context[0]
                    ),
                    source_usage_kind=(
                        source_call_context[1] if source_call_context else None
                    ),
                )
            findings.extend(argument_findings)
            findings.extend(return_findings)
            if "incompatible" in {argument_status, return_status}:
                status = "incompatible"
            elif argument_status == return_status == "compatible":
                status = "compatible"
            else:
                status = "unknown"
        if status == "incompatible":
            incompatible_count += 1
        elif scope_status == "out_of_scope":
            not_checked_count += 1
        elif status == "unknown":
            unknown_count += 1
        db.execute(
            """
            INSERT INTO project_call_compatibility(
                call_id, project_id, caller_symbol_id, callee_symbol_id, status,
                argument_status, return_status, scope_status, caller_analysis_sha256,
                callee_analysis_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call_id,
                project_id,
                call["caller_symbol_id"],
                call["resolved_symbol_id"],
                status,
                argument_status,
                return_status,
                scope_status,
                call["caller_analysis_sha256"],
                call["callee_analysis_sha256"],
            ),
        )
        db.executemany(
            """
            INSERT INTO project_call_findings(
                call_id, ordinal, severity, finding_kind, message,
                argument_ordinal, expected_types_json, actual_types_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    call_id,
                    ordinal,
                    item.severity,
                    item.kind,
                    item.message,
                    item.argument_ordinal,
                    json.dumps(item.expected_types, separators=(",", ":")),
                    json.dumps(item.actual_types, separators=(",", ":")),
                )
                for ordinal, item in enumerate(findings)
            ),
        )
    checked_count = len(calls)
    if checked_count == 0:
        project_status = "unavailable"
    else:
        project_status = "completed"
    db.execute(
        """
        UPDATE projects
        SET call_compatibility_status = ?, call_compatibility_checked_count = ?,
            call_compatibility_incompatible_count = ?, call_compatibility_unknown_count = ?,
            call_compatibility_not_checked_count = ?,
            call_compatibility_error = NULL,
            call_compatibility_updated_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            project_status,
            checked_count,
            incompatible_count,
            unknown_count,
            not_checked_count,
            project_id,
        ),
    )
    return CompatibilitySummary(
        project_status,
        checked_count,
        incompatible_count,
        unknown_count,
        not_checked_count,
    )
