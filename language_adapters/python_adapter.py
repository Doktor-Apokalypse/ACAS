"""Tree-sitter adapter for Python."""

import ast
import re
import textwrap

from tree_sitter import Language, Node

from .base import (
    AdapterCapabilities,
    CallArgumentRecord,
    CallRecord,
    DefinitionRecord,
    DependencyRecord,
    ExtractedStructure,
    LanguageAdapter,
    ParsedSource,
    SourceSpan,
    SyntaxDiagnostic,
)


class PythonAdapter(LanguageAdapter):
    language_id = "python"
    display_name = "Python"
    grammar_package = "tree-sitter-python"
    capabilities = AdapterCapabilities(
        definitions=True,
        dependencies=True,
        calls=True,
    )
    queries = {
        "definitions": """
            (function_definition
                name: (identifier) @definition.name) @definition.function
            (class_definition
                name: (identifier) @definition.name) @definition.class
        """,
        "dependencies": """
            (import_statement) @dependency.import
            (import_from_statement) @dependency.import
        """,
        "calls": "(call function: (_) @call.target) @call",
    }

    def load_language(self) -> Language:
        import tree_sitter_python

        return Language(tree_sitter_python.language())

    def _import_name(self, node: Node, source: bytes) -> str:
        name = node.child_by_field_name("name") if node.type == "aliased_import" else node
        return self.node_text(name or node, source)

    @staticmethod
    def _literal_types(node: Node) -> tuple[str, ...]:
        mapping = {
            "integer": ("int",),
            "float": ("float",),
            "string": ("str",),
            "concatenated_string": ("str",),
            "true": ("bool",),
            "false": ("bool",),
            "none": ("None",),
            "list": ("list",),
            "list_comprehension": ("list",),
            "dictionary": ("dict",),
            "dictionary_comprehension": ("dict",),
            "set": ("set",),
            "set_comprehension": ("set",),
            "tuple": ("tuple",),
            "lambda": ("callable",),
        }
        return mapping.get(node.type, ())

    def _call_arguments(self, call: Node, source: bytes) -> tuple[CallArgumentRecord, ...]:
        arguments_node = call.child_by_field_name("arguments")
        if arguments_node is None:
            return ()
        records: list[CallArgumentRecord] = []
        # Some versions of the Windows Tree-sitter binding can dereference
        # freed memory when ``start_point``/``end_point`` are read from a
        # deeply nested argument node.  The enclosing call span is stable and
        # still gives the caller a useful source location.
        call_span = self.source_span(call)
        # Iterate by index instead of using ``named_children``.  The latter
        # triggers an access violation in the Windows Tree-sitter binding for
        # some large Python trees (the interpreter is terminated before the
        # exception can be handled).  Indexed access is equivalent here once
        # we filter out punctuation/unnamed nodes, and keeps the web process
        # alive while analysing uploaded projects.
        for index in range(arguments_node.child_count):
            argument = arguments_node.child(index)
            if argument is None or not argument.is_named:
                continue
            keyword_name: str | None = None
            value = argument
            if argument.type == "keyword_argument":
                name = argument.child_by_field_name("name")
                value = argument.child_by_field_name("value") or argument
                keyword_name = self.node_text(name, source) if name else None
            records.append(
                CallArgumentRecord(
                    keyword_name=keyword_name,
                    expression_kind=argument.type,
                    inferred_types=self._literal_types(value),
                    span=call_span,
                )
            )
        return tuple(records)

    def _call_usage(self, call: Node, source: bytes) -> tuple[str, tuple[str, ...]]:
        parent = call.parent
        while parent is not None and parent.type in {"parenthesized_expression"}:
            parent = parent.parent
        if parent is not None and parent.type == "await":
            parent = parent.parent
            while parent is not None and parent.type in {"parenthesized_expression"}:
                parent = parent.parent

        def usage(value: str) -> str:
            return value

        if parent is None:
            return usage("unknown"), ()
        if parent.type == "expression_statement":
            return usage("statement"), ()
        if parent.type == "return_statement":
            return usage("return"), ()
        if parent.type in {"assignment", "annotated_assignment", "named_expression"}:
            annotation = parent.child_by_field_name("type")
            expected = (self.node_text(annotation, source),) if annotation else ()
            return usage("assignment"), expected
        if parent.type == "argument_list":
            return usage("argument"), ()
        if parent.type in {"if_statement", "while_statement", "conditional_expression"}:
            return usage("condition"), ("bool",)
        return usage("value"), ()

    @staticmethod
    def _ast_walk(root: ast.AST):
        stack = [root]
        while stack:
            node = stack.pop()
            yield node
            children = list(ast.iter_child_nodes(node))
            stack.extend(reversed(children))

    @staticmethod
    def _line_starts(source: bytes) -> list[int]:
        starts = [0]
        offset = 0
        for line in source.splitlines(keepends=True):
            offset += len(line)
            starts.append(offset)
        return starts

    @staticmethod
    def _ast_span(node: ast.AST, line_starts: list[int]) -> SourceSpan | None:
        lineno = getattr(node, "lineno", None)
        col = getattr(node, "col_offset", None)
        end_lineno = getattr(node, "end_lineno", None)
        end_col = getattr(node, "end_col_offset", None)
        if lineno is None or col is None or end_lineno is None or end_col is None:
            return None
        start_byte = line_starts[lineno - 1] + col
        end_byte = line_starts[end_lineno - 1] + end_col
        return SourceSpan(
            start_line=lineno,
            start_column=col + 1,
            end_line=end_lineno,
            end_column=end_col + 1,
            start_byte=start_byte,
            end_byte=end_byte,
        )

    @staticmethod
    def _rebase_span(
        span: SourceSpan,
        *,
        base_line: int,
        base_byte: int,
        dedent_columns: int,
    ) -> SourceSpan:
        start_column = span.start_column + dedent_columns
        end_column = span.end_column + dedent_columns
        return SourceSpan(
            start_line=base_line + span.start_line - 1,
            start_column=start_column,
            end_line=base_line + span.end_line - 1,
            end_column=end_column,
            start_byte=base_byte + span.start_byte + dedent_columns,
            end_byte=base_byte + span.end_byte + dedent_columns,
        )

    @classmethod
    def _definition_span(cls, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, line_starts: list[int]) -> SourceSpan | None:
        span = cls._ast_span(node, line_starts)
        if span is None or not node.decorator_list:
            return span
        first_decorator = min(
            (
                cls._ast_span(decorator, line_starts)
                for decorator in node.decorator_list
            ),
            key=lambda item: item.start_byte if item else span.start_byte,
        )
        if first_decorator is None:
            return span
        return SourceSpan(
            start_line=first_decorator.start_line,
            start_column=max(1, first_decorator.start_column - 1),
            end_line=span.end_line,
            end_column=span.end_column,
            start_byte=max(0, first_decorator.start_byte - 1),
            end_byte=span.end_byte,
        )

    @classmethod
    def _ast_literal_types(cls, node: ast.AST) -> tuple[str, ...]:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                return ("bool",)
            if isinstance(node.value, int):
                return ("int",)
            if isinstance(node.value, float):
                return ("float",)
            if isinstance(node.value, str):
                return ("str",)
            if isinstance(node.value, bytes):
                return ("bytes",)
            if node.value is None:
                return ("None",)
        if isinstance(node, ast.List):
            item_types = [cls._ast_literal_types(item) for item in node.elts]
            if item_types and all(
                len(item) == 1 and item == item_types[0]
                for item in item_types
            ):
                return (f"list[{item_types[0][0]}]",)
            return ("list",)
        if isinstance(node, ast.ListComp):
            return ("list",)
        if isinstance(node, ast.Dict):
            key_types = [
                cls._ast_literal_types(key)
                for key in node.keys
                if key is not None
            ]
            value_types = [cls._ast_literal_types(value) for value in node.values]
            if (
                node.keys
                and all(key is not None for key in node.keys)
                and key_types
                and value_types
                and all(len(item) == 1 and item == key_types[0] for item in key_types)
                and all(len(item) == 1 and item == value_types[0] for item in value_types)
            ):
                return (f"dict[{key_types[0][0]}, {value_types[0][0]}]",)
            return ("dict",)
        if isinstance(node, ast.DictComp):
            return ("dict",)
        if isinstance(node, ast.Set):
            item_types = [cls._ast_literal_types(item) for item in node.elts]
            if item_types and all(
                len(item) == 1 and item == item_types[0]
                for item in item_types
            ):
                return (f"set[{item_types[0][0]}]",)
            return ("set",)
        if isinstance(node, ast.SetComp):
            return ("set",)
        if isinstance(node, ast.Tuple):
            item_types = [cls._ast_literal_types(item) for item in node.elts]
            if item_types and all(len(item) == 1 for item in item_types):
                labels = [item[0] for item in item_types]
                if all(label == labels[0] for label in labels):
                    return (f"tuple[{labels[0]}, ...]",)
                return (f"tuple[{', '.join(labels)}]",)
            return ("tuple",)
        if isinstance(node, ast.Lambda):
            return ("callable",)
        return ()

    @staticmethod
    def _ast_annotation_type(node: ast.AST | None) -> tuple[str, ...]:
        """Return a value-level type from a source annotation.

        Metadata wrappers such as ``Annotated`` and ``ClassVar`` describe how a
        value is used, not a distinct runtime argument type, so the inner type is
        used for compatibility checks.
        """
        if node is None:
            return ()
        if isinstance(node, ast.Subscript):
            wrapper = PythonAdapter._ast_dotted_name(node.value).rsplit(".", 1)[-1]
            if wrapper in {"Annotated", "ClassVar", "Final", "Required", "NotRequired"}:
                inner = node.slice.elts[0] if isinstance(node.slice, ast.Tuple) else node.slice
                return PythonAdapter._ast_annotation_type(inner)
        try:
            value = ast.unparse(node).strip()
        except Exception:
            return ()
        return (value,) if value else ()

    @staticmethod
    def _ast_dotted_name(node: ast.AST | None) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            owner = PythonAdapter._ast_dotted_name(node.value)
            return f"{owner}.{node.attr}" if owner else node.attr
        return ""

    @staticmethod
    def _merge_inferred_types(*groups: tuple[str, ...]) -> tuple[str, ...]:
        values: list[str] = []
        seen: set[str] = set()
        for group in groups:
            for value in group:
                key = value.casefold()
                if value and key not in seen:
                    seen.add(key)
                    values.append(value)
        return tuple(values[:12])

    @staticmethod
    def _generic_item_types(type_names: tuple[str, ...]) -> tuple[str, ...]:
        values: list[str] = []
        for type_name in type_names:
            match = re.fullmatch(
                r"(?:list|set|frozenset|sequence|iterable|iterator|generator)\[(.+)\]",
                re.sub(r"\s+", "", type_name),
                re.IGNORECASE,
            )
            if match:
                values.append(match.group(1))
                continue
            tuple_match = re.fullmatch(
                r"tuple\[(.+)\]",
                re.sub(r"\s+", "", type_name),
                re.IGNORECASE,
            )
            if tuple_match:
                parts = [part for part in tuple_match.group(1).split(",") if part != "..."]
                values.extend(parts)
        return PythonAdapter._merge_inferred_types(tuple(values))

    @staticmethod
    def _ast_type_expression_names(node: ast.AST | None) -> tuple[str, ...]:
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return PythonAdapter._merge_inferred_types(
                PythonAdapter._ast_type_expression_names(node.left),
                PythonAdapter._ast_type_expression_names(node.right),
            )
        if isinstance(node, ast.Tuple):
            return PythonAdapter._merge_inferred_types(
                *(PythonAdapter._ast_type_expression_names(item) for item in node.elts)
            )
        name = PythonAdapter._ast_dotted_name(node)
        return (name,) if name else ()

    @staticmethod
    def _ast_isinstance_narrowing(test: ast.AST, name: str) -> tuple[str, ...]:
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
            return PythonAdapter._merge_inferred_types(
                *(PythonAdapter._ast_isinstance_narrowing(value, name) for value in test.values)
            )
        if (
            isinstance(test, ast.Call)
            and isinstance(test.func, ast.Name)
            and test.func.id == "isinstance"
            and len(test.args) >= 2
            and isinstance(test.args[0], ast.Name)
            and test.args[0].id == name
        ):
            return PythonAdapter._ast_type_expression_names(test.args[1])
        return ()

    @staticmethod
    def _scope_nodes(scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[ast.AST, ...]:
        """Return nodes owned by one lexical scope, excluding nested scopes."""
        nodes: list[ast.AST] = []

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                if node is scope:
                    for statement in node.body:
                        self.visit(statement)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                if node is scope:
                    for statement in node.body:
                        self.visit(statement)

            def visit_ClassDef(self, _node: ast.ClassDef) -> None:
                return

            def visit_Lambda(self, _node: ast.Lambda) -> None:
                return

            def generic_visit(self, node: ast.AST) -> None:
                nodes.append(node)
                super().generic_visit(node)

        visitor = Visitor()
        if isinstance(scope, ast.Module):
            for statement in scope.body:
                if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                    continue
                visitor.visit(statement)
        else:
            visitor.visit(scope)
        return tuple(nodes)

    @classmethod
    def _ast_infer_types(
        cls,
        node: ast.AST | None,
        environment: dict[str, tuple[str, ...]],
        function_returns: dict[str, tuple[str, ...]],
        method_returns: dict[tuple[str, str], tuple[str, ...]],
        class_fields: dict[str, dict[str, tuple[str, ...]]],
        class_names: set[str],
        current_class: str | None,
    ) -> tuple[str, ...]:
        if node is None:
            return ()
        if isinstance(node, ast.Name):
            return environment.get(node.id, ()) or (
                (node.id,) if node.id in class_names else ()
            )
        if isinstance(node, ast.JoinedStr):
            return ("str",)
        if isinstance(node, ast.Compare):
            return ("bool",)
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.Not):
                return ("bool",)
            return cls._ast_infer_types(
                node.operand,
                environment,
                function_returns,
                method_returns,
                class_fields,
                class_names,
                current_class,
            )
        if isinstance(node, ast.BoolOp):
            alternatives = tuple(
                cls._ast_infer_types(
                    value,
                    environment,
                    function_returns,
                    method_returns,
                    class_fields,
                    class_names,
                    current_class,
                )
                for value in node.values
            )
            return (
                cls._merge_inferred_types(*alternatives)
                if alternatives and all(alternatives)
                else ()
            )
        if isinstance(node, ast.BinOp):
            left = cls._ast_infer_types(
                node.left,
                environment,
                function_returns,
                method_returns,
                class_fields,
                class_names,
                current_class,
            )
            right = cls._ast_infer_types(
                node.right,
                environment,
                function_returns,
                method_returns,
                class_fields,
                class_names,
                current_class,
            )
            if not left or not right:
                return ()
            if left == right:
                return left
            numeric = {value.casefold() for value in (*left, *right)}
            if numeric and numeric <= {"int", "float"}:
                return ("float",) if "float" in numeric else ("int",)
            return ()
        if isinstance(node, ast.IfExp):
            body_types = cls._ast_infer_types(
                node.body,
                environment,
                function_returns,
                method_returns,
                class_fields,
                class_names,
                current_class,
            )
            else_types = cls._ast_infer_types(
                node.orelse,
                environment,
                function_returns,
                method_returns,
                class_fields,
                class_names,
                current_class,
            )
            if not body_types or not else_types:
                return ()
            return cls._merge_inferred_types(body_types, else_types)
        if isinstance(node, ast.NamedExpr):
            return cls._ast_infer_types(
                node.value,
                environment,
                function_returns,
                method_returns,
                class_fields,
                class_names,
                current_class,
            )
        if isinstance(node, ast.Await):
            return cls._ast_infer_types(
                node.value,
                environment,
                function_returns,
                method_returns,
                class_fields,
                class_names,
                current_class,
            )
        if isinstance(node, ast.List | ast.Set | ast.Tuple):
            inferred_items = tuple(
                cls._ast_infer_types(
                    item,
                    environment,
                    function_returns,
                    method_returns,
                    class_fields,
                    class_names,
                    current_class,
                )
                for item in node.elts
            )
            item_types = (
                cls._merge_inferred_types(*inferred_items)
                if inferred_items and all(inferred_items)
                else ()
            )
            base = type(node).__name__.casefold()
            if item_types and len(item_types) == 1:
                suffix = ", ..." if isinstance(node, ast.Tuple) else ""
                return (f"{base}[{item_types[0]}{suffix}]",)
            return (base,)
        if isinstance(node, ast.Dict):
            inferred_keys = tuple(
                cls._ast_infer_types(
                    key,
                    environment,
                    function_returns,
                    method_returns,
                    class_fields,
                    class_names,
                    current_class,
                )
                for key in node.keys
                if key is not None
            )
            inferred_values = tuple(
                cls._ast_infer_types(
                    value,
                    environment,
                    function_returns,
                    method_returns,
                    class_fields,
                    class_names,
                    current_class,
                )
                for value in node.values
            )
            key_types = (
                cls._merge_inferred_types(*inferred_keys)
                if inferred_keys and all(inferred_keys)
                else ()
            )
            value_types = (
                cls._merge_inferred_types(*inferred_values)
                if inferred_values and all(inferred_values)
                else ()
            )
            if len(key_types) == len(value_types) == 1:
                return (f"dict[{key_types[0]}, {value_types[0]}]",)
            return ("dict",)
        if isinstance(node, ast.ListComp | ast.SetComp | ast.GeneratorExp):
            local = dict(environment)
            for generator in node.generators:
                if isinstance(generator.target, ast.Name):
                    iterable = cls._ast_infer_types(
                        generator.iter,
                        local,
                        function_returns,
                        method_returns,
                        class_fields,
                        class_names,
                        current_class,
                    )
                    item_types = cls._generic_item_types(iterable)
                    if item_types:
                        local[generator.target.id] = item_types
                    for condition in generator.ifs:
                        narrowed = cls._ast_isinstance_narrowing(
                            condition, generator.target.id
                        )
                        if narrowed:
                            local[generator.target.id] = narrowed
            item_types = cls._ast_infer_types(
                node.elt,
                local,
                function_returns,
                method_returns,
                class_fields,
                class_names,
                current_class,
            )
            base = "list" if isinstance(node, ast.ListComp) else (
                "set" if isinstance(node, ast.SetComp) else "Iterator"
            )
            return (
                tuple(f"{base}[{item_type}]" for item_type in item_types)
                if item_types
                else (base,)
            )
        if isinstance(node, ast.DictComp):
            local = dict(environment)
            for generator in node.generators:
                if isinstance(generator.target, ast.Name):
                    iterable = cls._ast_infer_types(
                        generator.iter,
                        local,
                        function_returns,
                        method_returns,
                        class_fields,
                        class_names,
                        current_class,
                    )
                    item_types = cls._generic_item_types(iterable)
                    if item_types:
                        local[generator.target.id] = item_types
                    for condition in generator.ifs:
                        narrowed = cls._ast_isinstance_narrowing(
                            condition, generator.target.id
                        )
                        if narrowed:
                            local[generator.target.id] = narrowed
            keys = cls._ast_infer_types(
                node.key,
                local,
                function_returns,
                method_returns,
                class_fields,
                class_names,
                current_class,
            )
            values = cls._ast_infer_types(
                node.value,
                local,
                function_returns,
                method_returns,
                class_fields,
                class_names,
                current_class,
            )
            return (f"dict[{keys[0]}, {values[0]}]",) if len(keys) == len(values) == 1 else ("dict",)
        if isinstance(node, ast.Subscript):
            owner = cls._ast_infer_types(
                node.value,
                environment,
                function_returns,
                method_returns,
                class_fields,
                class_names,
                current_class,
            )
            if isinstance(node.slice, ast.Slice) or (
                isinstance(node.slice, ast.Tuple)
                and any(isinstance(item, ast.Slice) for item in node.slice.elts)
            ):
                return owner
            values: list[str] = []
            for type_name in owner:
                compact = re.sub(r"\s+", "", type_name)
                mapping = re.fullmatch(r"(?:dict|mapping)\[[^,]+,(.+)\]", compact, re.IGNORECASE)
                if mapping:
                    values.append(mapping.group(1))
                    continue
                values.extend(cls._generic_item_types((compact,)))
            return cls._merge_inferred_types(tuple(values))
        if isinstance(node, ast.Attribute):
            owners = cls._ast_infer_types(
                node.value,
                environment,
                function_returns,
                method_returns,
                class_fields,
                class_names,
                current_class,
            )
            return cls._merge_inferred_types(
                *(class_fields.get(owner.rsplit(".", 1)[-1], {}).get(node.attr, ()) for owner in owners)
            )
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                name = node.func.id
                if name in function_returns:
                    return function_returns[name]
                if name in class_names:
                    return (name,)
                builtin_returns = {
                    "bool": ("bool",),
                    "bytes": ("bytes",),
                    "dict": ("dict",),
                    "float": ("float",),
                    "int": ("int",),
                    "len": ("int",),
                    "list": ("list",),
                    "repr": ("str",),
                    "set": ("set",),
                    "str": ("str",),
                    "tuple": ("tuple",),
                }
                return builtin_returns.get(name, ())
            if isinstance(node.func, ast.Attribute):
                receiver_types = cls._ast_infer_types(
                    node.func.value,
                    environment,
                    function_returns,
                    method_returns,
                    class_fields,
                    class_names,
                    current_class,
                )
                returns = cls._merge_inferred_types(
                    *(
                        method_returns.get((owner.rsplit(".", 1)[-1], node.func.attr), ())
                        for owner in receiver_types
                    )
                )
                if returns:
                    return returns
                if any(owner.casefold() in {"str", "string"} for owner in receiver_types):
                    if node.func.attr in {
                        "capitalize", "casefold", "center", "expandtabs", "format",
                        "format_map", "join", "ljust", "lower", "lstrip", "removeprefix",
                        "removesuffix", "replace", "rjust", "rstrip", "strip", "swapcase",
                        "title", "translate", "upper", "zfill",
                    }:
                        return ("str",)
                    if node.func.attr in {
                        "endswith", "isalnum", "isalpha", "isascii", "isdecimal", "isdigit",
                        "isidentifier", "islower", "isnumeric", "isprintable", "isspace",
                        "istitle", "isupper", "startswith",
                    }:
                        return ("bool",)
                    if node.func.attr in {"count", "find", "index", "rfind", "rindex"}:
                        return ("int",)
                    if node.func.attr in {"split", "splitlines", "rsplit"}:
                        return ("list[str]",)
                    if node.func.attr == "encode":
                        return ("bytes",)
            return ()
        literal = cls._ast_literal_types(node)
        return literal

    @classmethod
    def _ast_argument_type_index(cls, module: ast.Module) -> dict[int, tuple[str, ...]]:
        """Infer conservative argument types from annotations and stable local bindings."""
        parents = {
            child: parent
            for parent in ast.walk(module)
            for child in ast.iter_child_nodes(parent)
        }
        class_names = {node.name for node in ast.walk(module) if isinstance(node, ast.ClassDef)}
        class_fields: dict[str, dict[str, tuple[str, ...]]] = {}
        method_returns: dict[tuple[str, str], tuple[str, ...]] = {}
        return_candidates: dict[str, list[tuple[str, ...]]] = {}
        for node in ast.walk(module):
            if isinstance(node, ast.ClassDef):
                fields = class_fields.setdefault(node.name, {})
                for statement in node.body:
                    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                        fields[statement.target.id] = cls._ast_annotation_type(statement.annotation)
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            returns = cls._ast_annotation_type(node.returns)
            if returns and returns[0] not in {"None", "NoReturn", "Never"}:
                parent = parents.get(node)
                if isinstance(parent, ast.ClassDef):
                    method_returns[(parent.name, node.name)] = returns
                else:
                    return_candidates.setdefault(node.name, []).append(returns)
        function_returns = {
            name: candidates[0]
            for name, candidates in return_candidates.items()
            if candidates and all(candidate == candidates[0] for candidate in candidates)
        }

        type_aliases: dict[str, tuple[str, ...]] = {}
        for statement in module.body:
            if not isinstance(statement, ast.Assign):
                continue
            alias_base = (
                cls._ast_dotted_name(statement.value.value).rsplit(".", 1)[-1]
                if isinstance(statement.value, ast.Subscript)
                else cls._ast_dotted_name(statement.value).rsplit(".", 1)[-1]
            )
            if alias_base != "Callable":
                continue
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    type_aliases[target.id] = ("Callable",)

        def annotation_types(node: ast.AST | None) -> tuple[str, ...]:
            if isinstance(node, ast.Name) and node.id in type_aliases:
                return cls._merge_inferred_types((node.id,), type_aliases[node.id])
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                return cls._merge_inferred_types(
                    annotation_types(node.left), annotation_types(node.right)
                )
            return cls._ast_annotation_type(node)

        module_environment: dict[str, tuple[str, ...]] = {}
        for statement in module.body:
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                annotation = annotation_types(statement.annotation)
                if annotation:
                    module_environment[statement.target.id] = annotation
            elif isinstance(statement, ast.Assign):
                inferred = cls._ast_infer_types(
                    statement.value,
                    module_environment,
                    function_returns,
                    method_returns,
                    class_fields,
                    class_names,
                    None,
                )
                if inferred:
                    for target in statement.targets:
                        if isinstance(target, ast.Name):
                            module_environment[target.id] = inferred

        base_environments: dict[ast.AST, dict[str, tuple[str, ...]]] = {
            module: {},
        }

        def enclosing_scope(node: ast.AST) -> ast.Module | ast.FunctionDef | ast.AsyncFunctionDef:
            current = parents.get(node)
            while current is not None:
                if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
                    return current
                current = parents.get(current)
            return module

        def function_base_environment(
            function: ast.FunctionDef | ast.AsyncFunctionDef,
        ) -> dict[str, tuple[str, ...]]:
            if function in base_environments:
                return dict(base_environments[function])
            owner = enclosing_scope(function)
            environment = dict(module_environment)
            if isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef):
                environment.update(function_base_environment(owner))
            parent = parents.get(function)
            current_class = parent.name if isinstance(parent, ast.ClassDef) else None
            if current_class:
                environment.setdefault("self", (current_class,))
                environment.setdefault("cls", (current_class,))
            args = function.args
            for argument in (*args.posonlyargs, *args.args, *args.kwonlyargs):
                annotation = annotation_types(argument.annotation)
                if annotation:
                    environment[argument.arg] = annotation
            if args.vararg is not None:
                annotation = annotation_types(args.vararg.annotation)
                if annotation:
                    environment[args.vararg.arg] = (f"tuple[{annotation[0]}, ...]",)
            if args.kwarg is not None:
                annotation = annotation_types(args.kwarg.annotation)
                if annotation:
                    environment[args.kwarg.arg] = (f"dict[str, {annotation[0]}]",)
            base_environments[function] = environment
            return dict(environment)

        control_nodes = (
            ast.If,
            ast.For,
            ast.AsyncFor,
            ast.While,
            ast.Try,
            ast.TryStar,
            ast.Match,
            ast.match_case,
            ast.ExceptHandler,
        )

        def control_path(
            node: ast.AST,
            scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef,
        ) -> tuple[tuple[int, str], ...]:
            path: list[tuple[int, str]] = []
            child: ast.AST = node
            parent = parents.get(child)
            while parent is not None and parent is not scope:
                if isinstance(parent, control_nodes):
                    field_name = ""
                    for name, value in ast.iter_fields(parent):
                        if value is child or (
                            isinstance(value, list) and child in value
                        ):
                            field_name = name
                            break
                    path.append((id(parent), field_name))
                child = parent
                parent = parents.get(parent)
            path.reverse()
            return tuple(path)

        scope_node_cache: dict[
            ast.Module | ast.FunctionDef | ast.AsyncFunctionDef,
            tuple[ast.AST, ...],
        ] = {}
        binding_cache: dict[
            ast.Module | ast.FunctionDef | ast.AsyncFunctionDef,
            tuple[ast.AST, ...],
        ] = {}

        def owned_nodes(
            scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef,
        ) -> tuple[ast.AST, ...]:
            if scope not in scope_node_cache:
                scope_node_cache[scope] = cls._scope_nodes(scope)
            return scope_node_cache[scope]

        def binding_nodes(
            scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef,
        ) -> tuple[ast.AST, ...]:
            if scope not in binding_cache:
                binding_cache[scope] = tuple(
                    sorted(
                        (
                            node
                            for node in owned_nodes(scope)
                            if isinstance(
                                node,
                                ast.Assign
                                | ast.AnnAssign
                                | ast.NamedExpr
                                | ast.For
                                | ast.AsyncFor,
                            )
                        ),
                        key=lambda node: (
                            int(getattr(node, "lineno", 0)),
                            int(getattr(node, "col_offset", 0)),
                        ),
                    )
                )
            return binding_cache[scope]

        def environment_at_call(
            scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef,
            call: ast.Call,
        ) -> dict[str, tuple[str, ...]]:
            environment = (
                {}
                if isinstance(scope, ast.Module)
                else function_base_environment(scope)
            )
            parent = parents.get(scope) if not isinstance(scope, ast.Module) else None
            current_class = parent.name if isinstance(parent, ast.ClassDef) else None
            call_position = (
                int(getattr(call, "lineno", 0)),
                int(getattr(call, "col_offset", 0)),
            )
            call_path = control_path(call, scope)

            def narrow_from_test(test: ast.AST, truthy: bool) -> None:
                if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                    narrow_from_test(test.operand, not truthy)
                    return
                if isinstance(test, ast.BoolOp) and (
                    truthy and isinstance(test.op, ast.And)
                    or not truthy and isinstance(test.op, ast.Or)
                ):
                    for value in test.values:
                        narrow_from_test(value, truthy)
                    return
                if (
                    isinstance(test, ast.Call)
                    and isinstance(test.func, ast.Name)
                    and test.func.id == "isinstance"
                    and len(test.args) >= 2
                    and isinstance(test.args[0], ast.Name)
                ):
                    if not truthy:
                        return
                    names = cls._ast_type_expression_names(test.args[1])
                    if names:
                        environment[test.args[0].id] = names
                    return
                if (
                    isinstance(test, ast.Compare)
                    and len(test.ops) == len(test.comparators) == 1
                    and isinstance(test.left, ast.Name)
                    and isinstance(test.comparators[0], ast.Constant)
                    and test.comparators[0].value is None
                ):
                    non_null = (
                        truthy and isinstance(test.ops[0], ast.IsNot)
                    ) or (
                        not truthy and isinstance(test.ops[0], ast.Is)
                    )
                    if non_null:
                        remaining = tuple(
                            value for value in environment.get(test.left.id, ())
                            if value.casefold() not in {"none", "nonetype", "null"}
                        )
                        if remaining:
                            environment[test.left.id] = remaining
                        else:
                            environment.pop(test.left.id, None)

            for binding in binding_nodes(scope):
                binding_position = (
                    int(getattr(binding, "lineno", 0)),
                    int(getattr(binding, "col_offset", 0)),
                )
                if binding_position >= call_position:
                    continue
                binding_path = control_path(binding, scope)
                if binding_path != call_path[: len(binding_path)]:
                    continue
                targets: list[ast.Name] = []
                value: ast.AST | None = None
                annotation: tuple[str, ...] = ()
                if isinstance(binding, ast.Assign):
                    targets = [
                        target
                        for target in binding.targets
                        if isinstance(target, ast.Name)
                    ]
                    value = binding.value
                elif isinstance(binding, ast.AnnAssign) and isinstance(binding.target, ast.Name):
                    targets = [binding.target]
                    value = binding.value
                    annotation = annotation_types(binding.annotation)
                elif isinstance(binding, ast.NamedExpr) and isinstance(binding.target, ast.Name):
                    targets = [binding.target]
                    value = binding.value
                elif isinstance(binding, ast.For | ast.AsyncFor) and isinstance(binding.target, ast.Name):
                    if (id(binding), "body") not in call_path:
                        continue
                    targets = [binding.target]
                    iterable = cls._ast_infer_types(
                        binding.iter,
                        environment,
                        function_returns,
                        method_returns,
                        class_fields,
                        class_names,
                        current_class,
                    )
                    inferred = cls._generic_item_types(iterable)
                    if inferred:
                        environment[binding.target.id] = inferred
                    continue
                inferred = annotation or cls._ast_infer_types(
                    value,
                    environment,
                    function_returns,
                    method_returns,
                    class_fields,
                    class_names,
                    current_class,
                )
                if inferred:
                    for target in targets:
                        environment[target.id] = inferred
            current: ast.AST = call
            while current is not scope:
                parent_node = parents.get(current)
                if parent_node is None:
                    break
                if isinstance(parent_node, ast.If):
                    if current in parent_node.body:
                        narrow_from_test(parent_node.test, True)
                    elif current in parent_node.orelse:
                        narrow_from_test(parent_node.test, False)
                for _field, value in ast.iter_fields(parent_node):
                    if not isinstance(value, list) or current not in value:
                        continue
                    for previous in value[: value.index(current)]:
                        if not isinstance(previous, ast.If) or not previous.body:
                            continue
                        if isinstance(previous.body[-1], ast.Continue | ast.Return | ast.Raise):
                            narrow_from_test(previous.test, False)
                    break
                current = parent_node
            return environment

        result: dict[int, tuple[str, ...]] = {}
        scopes: list[ast.Module | ast.FunctionDef | ast.AsyncFunctionDef] = [
            module,
            *[
                node
                for node in ast.walk(module)
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            ],
        ]
        for scope in scopes:
            parent = parents.get(scope) if not isinstance(scope, ast.Module) else None
            current_class = parent.name if isinstance(parent, ast.ClassDef) else None
            for node in owned_nodes(scope):
                if not isinstance(node, ast.Call):
                    continue
                environment = environment_at_call(scope, node)
                for argument in node.args:
                    inferred = cls._ast_infer_types(
                        argument,
                        environment,
                        function_returns,
                        method_returns,
                        class_fields,
                        class_names,
                        current_class,
                    )
                    if inferred:
                        result[id(argument)] = inferred
                for keyword in node.keywords:
                    inferred = cls._ast_infer_types(
                        keyword.value,
                        environment,
                        function_returns,
                        method_returns,
                        class_fields,
                        class_names,
                        current_class,
                    )
                    if inferred:
                        result[id(keyword.value)] = inferred
        return result

    @staticmethod
    def _ast_expression_kind(node: ast.AST) -> str:
        if isinstance(node, ast.Starred):
            return "list_splat"
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                return "true" if node.value else "false"
            if isinstance(node.value, int):
                return "integer"
            if isinstance(node.value, float):
                return "float"
            if isinstance(node.value, str):
                return "string"
            if node.value is None:
                return "none"
        return type(node).__name__.removesuffix("Comp").lower()

    @staticmethod
    def _ast_callee(node: ast.AST, source_text: str) -> str:
        if isinstance(node, ast.Name):
            return node.id
        segment = ast.get_source_segment(source_text, node)
        if segment:
            return " ".join(segment.split())
        try:
            return ast.unparse(node)
        except Exception:
            return type(node).__name__

    @staticmethod
    def _ast_call_usage(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> tuple[str, tuple[str, ...]]:
        parent = parents.get(call)
        if isinstance(parent, ast.Await):
            parent = parents.get(parent)

        def usage(value: str) -> str:
            return value

        while isinstance(parent, ast.Expr) and isinstance(parent.value, ast.Call) and parent.value is not call:
            parent = parents.get(parent)
        if isinstance(parent, ast.Expr):
            return usage("statement"), ()
        if isinstance(parent, ast.Return):
            return usage("return"), ()
        if isinstance(parent, ast.Assign | ast.NamedExpr):
            return usage("assignment"), ()
        if isinstance(parent, ast.AnnAssign):
            annotation = ast.unparse(parent.annotation) if parent.annotation else ""
            return usage("assignment"), (annotation,) if annotation else ()
        if isinstance(parent, ast.keyword) or isinstance(parent, ast.Call):
            return usage("argument"), ()
        if isinstance(parent, ast.If | ast.While | ast.IfExp):
            return usage("condition"), ("bool",)
        return usage("value"), ()

    @classmethod
    def _ast_call_arguments(
        cls,
        call: ast.Call,
        call_span: SourceSpan,
        inferred_types_by_node: dict[int, tuple[str, ...]] | None = None,
    ) -> tuple[CallArgumentRecord, ...]:
        records: list[CallArgumentRecord] = []
        inferred_types_by_node = inferred_types_by_node or {}
        for argument in call.args:
            records.append(
                CallArgumentRecord(
                    keyword_name=None,
                    expression_kind=cls._ast_expression_kind(argument),
                    inferred_types=(
                        inferred_types_by_node.get(id(argument))
                        or cls._ast_literal_types(argument)
                    ),
                    span=call_span,
                )
            )
        for keyword in call.keywords:
            if keyword.arg is None:
                records.append(
                    CallArgumentRecord(
                        keyword_name=None,
                        expression_kind="dictionary_splat",
                        inferred_types=(
                            inferred_types_by_node.get(id(keyword.value))
                            or cls._ast_literal_types(keyword.value)
                        ),
                        span=call_span,
                    )
                )
                continue
            records.append(
                CallArgumentRecord(
                    keyword_name=keyword.arg,
                    expression_kind="keyword_argument",
                    inferred_types=(
                        inferred_types_by_node.get(id(keyword.value))
                        or cls._ast_literal_types(keyword.value)
                    ),
                    span=call_span,
                )
            )
        return tuple(records)

    def parse_text(self, text: str) -> ParsedSource:
        """Parse Python source, avoiding Tree-sitter on syntactically invalid files.

        The Windows Tree-sitter Python binding can raise a native access violation
        on some malformed sources.  A native crash cannot be caught by Python, so
        invalid Python is detected with the standard-library AST parser first and
        then handled by the fallback structure extractor.
        """
        try:
            ast.parse(text)
        except SyntaxError as exc:
            source = text.encode("utf-8")
            line_starts = self._line_starts(source)
            line = max(1, min(exc.lineno or 1, len(line_starts)))
            column = max(1, exc.offset or 1)
            start_byte = min(
                len(source),
                line_starts[line - 1] + max(0, column - 1),
            )
            end_byte = min(len(source), max(start_byte + 1, start_byte))
            diagnostic = SyntaxDiagnostic(
                kind="error",
                message=exc.msg or "Invalid Python syntax",
                start_line=line,
                start_column=column,
                end_line=line,
                end_column=column + 1,
                start_byte=start_byte,
                end_byte=end_byte,
            )
            tree = self.new_parser().parse(b"")
            return ParsedSource(
                language_id=self.language_id,
                tree=tree,
                diagnostics=(diagnostic,),
                error_count=1,
                missing_count=0,
                diagnostics_truncated=False,
            )
        return super().parse_text(text)

    @classmethod
    def _fallback_structure(cls, source_text: str, source: bytes) -> ExtractedStructure:
        """Index obvious definitions in syntactically invalid Python files."""
        line_starts = cls._line_starts(source)
        lines = source_text.splitlines(keepends=True)
        definitions: list[DefinitionRecord] = []
        calls: list[CallRecord] = []
        matches = list(re.finditer(
            r"(?m)^(?P<indent>[ \t]*)(?:(?:async)[ \t]+)?(?P<kind>def|class)[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
            source_text,
        ))
        for index, match in enumerate(matches):
            line = source_text.count("\n", 0, match.start()) + 1
            column = len(match.group("indent")) + 1
            indent_width = len(match.group("indent").replace("\t", "    "))
            line_start = line_starts[line - 1]
            header_end_line = line
            bracket_balance = 0
            for candidate_line in range(line, len(lines) + 1):
                text = lines[candidate_line - 1]
                bracket_balance += text.count("(") + text.count("[") + text.count("{")
                bracket_balance -= text.count(")") + text.count("]") + text.count("}")
                header_end_line = candidate_line
                if text.rstrip().endswith(":") and bracket_balance <= 0:
                    break
            end_line = header_end_line
            for candidate_line in range(header_end_line + 1, len(lines) + 1):
                text = lines[candidate_line - 1]
                stripped = text.strip()
                if not stripped or stripped.startswith("#"):
                    end_line = candidate_line
                    continue
                candidate_indent = len(text[: len(text) - len(text.lstrip(" \t"))].replace("\t", "    "))
                if candidate_indent <= indent_width:
                    break
                end_line = candidate_line
            if end_line == line and index + 1 < len(matches):
                end_line = max(line, source_text.count("\n", 0, matches[index + 1].start()))
            line_end = line_starts[end_line] if end_line < len(line_starts) else len(source)
            definitions.append(
                DefinitionRecord(
                    kind="function" if match.group("kind") == "def" else "class",
                    name=match.group("name"),
                    qualified_name=match.group("name"),
                    span=SourceSpan(
                        start_line=line,
                        start_column=column,
                        end_line=end_line,
                        end_column=max(1, line_end - line_starts[end_line - 1] + 1),
                        start_byte=line_start + column - 1,
                        end_byte=line_end,
                    ),
                    body_span=None,
                )
            )
            if match.group("kind") != "def":
                continue
            snippet = source[line_start:line_end].decode("utf-8", errors="replace")
            dedented = textwrap.dedent(snippet)
            dedent_columns = 0
            if dedented != snippet:
                first_line = snippet.splitlines()[0] if snippet.splitlines() else ""
                dedented_first_line = dedented.splitlines()[0] if dedented.splitlines() else ""
                dedent_columns = max(0, len(first_line) - len(dedented_first_line))
            try:
                module = ast.parse(dedented)
            except SyntaxError:
                continue
            snippet_source = dedented
            snippet_bytes = snippet_source.encode("utf-8")
            snippet_line_starts = cls._line_starts(snippet_bytes)
            parents = {
                child: parent
                for parent in cls._ast_walk(module)
                for child in ast.iter_child_nodes(parent)
            }
            for call in cls._ast_walk(module):
                if not isinstance(call, ast.Call):
                    continue
                span = cls._ast_span(call, snippet_line_starts)
                if span is None:
                    continue
                absolute_span = cls._rebase_span(
                    span,
                    base_line=line,
                    base_byte=line_start,
                    dedent_columns=dedent_columns,
                )
                usage_kind, expected_return_types = cls._ast_call_usage(call, parents)
                calls.append(
                    CallRecord(
                        callee=cls._ast_callee(call.func, snippet_source)[:512],
                        span=absolute_span,
                        caller_index=len(definitions) - 1,
                        arguments=tuple(
                            CallArgumentRecord(
                                keyword_name=argument.keyword_name,
                                expression_kind=argument.expression_kind,
                                inferred_types=argument.inferred_types,
                                span=absolute_span,
                            )
                            for argument in cls._ast_call_arguments(call, absolute_span)
                        ),
                        usage_kind=usage_kind,
                        expected_return_types=expected_return_types,
                    )
                )
        return ExtractedStructure(tuple(definitions), (), tuple(calls))

    def raw_structure(self, parsed, source: bytes) -> ExtractedStructure:
        try:
            source_text = source.decode("utf-8")
        except UnicodeDecodeError:
            return ExtractedStructure((), (), ())
        try:
            module = ast.parse(source_text)
        except SyntaxError:
            return self._fallback_structure(source_text, source)
        line_starts = self._line_starts(source)
        parents = {
            child: parent
            for parent in self._ast_walk(module)
            for child in ast.iter_child_nodes(parent)
        }
        inferred_types_by_node = self._ast_argument_type_index(module)

        definitions: list[DefinitionRecord] = []
        for node in self._ast_walk(module):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue
            span = self._definition_span(node, line_starts)
            if span is None:
                continue
            definitions.append(
                DefinitionRecord(
                    kind="class" if isinstance(node, ast.ClassDef) else "function",
                    name=node.name,
                    qualified_name=node.name,
                    span=span,
                    # Avoid dereferencing the Python grammar's body child on
                    # Windows.  With some tree-sitter native wheels, reading
                    # point/range fields from this child can terminate the
                    # interpreter with an access violation.  The full
                    # definition span is stable and sufficient for indexing.
                    body_span=None,
                )
            )

        dependencies: list[DependencyRecord] = []
        for node in self._ast_walk(module):
            span = self._ast_span(node, line_starts)
            if span is None:
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    dependencies.append(
                        DependencyRecord(
                            kind="import",
                            module_name=alias.name,
                            imported_names=(alias.asname,) if alias.asname else (),
                            is_relative=False,
                            span=span,
                        )
                    )
            elif isinstance(node, ast.ImportFrom):
                module_name = ("." * node.level) + (node.module or "")
                imported_names = tuple(alias.asname or alias.name for alias in node.names)
                dependencies.append(
                    DependencyRecord(
                        kind="import_from",
                        module_name=module_name,
                        imported_names=imported_names,
                        is_relative=node.level > 0,
                        span=span,
                    )
                )

        calls: list[CallRecord] = []
        for call in self._ast_walk(module):
            if isinstance(call, ast.Call):
                span = self._ast_span(call, line_starts)
                if span is None:
                    continue
                usage_kind, expected_return_types = self._ast_call_usage(call, parents)
                calls.append(
                    CallRecord(
                        callee=self._ast_callee(call.func, source_text)[:512],
                        span=span,
                        arguments=self._ast_call_arguments(
                            call,
                            span,
                            inferred_types_by_node,
                        ),
                        usage_kind=usage_kind,
                        expected_return_types=expected_return_types,
                    )
                )
        return ExtractedStructure(tuple(definitions), tuple(dependencies), tuple(calls))
