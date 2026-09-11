"""Tree-sitter adapter for C++."""

from tree_sitter import Language, Node

from .base import (
    AdapterCapabilities,
    CallArgumentRecord,
    CallRecord,
    DefinitionRecord,
    DependencyRecord,
    ExtractedStructure,
    LanguageAdapter,
)


class CppAdapter(LanguageAdapter):
    language_id = "cpp"
    display_name = "C++"
    grammar_package = "tree-sitter-cpp"
    qualification_separator = "::"
    capabilities = AdapterCapabilities(
        definitions=True,
        dependencies=True,
        calls=True,
    )
    queries = {
        "definitions": """
            (function_definition) @definition.function
            (class_specifier name: (_) @definition.name) @definition.class
            (struct_specifier name: (_) @definition.name) @definition.struct
        """,
        "dependencies": "(preproc_include) @dependency.include",
        "calls": "(call_expression function: (_) @call.target) @call",
    }

    def load_language(self) -> Language:
        import tree_sitter_cpp

        return Language(tree_sitter_cpp.language())

    def _function_name_node(self, declarator: Node | None) -> Node | None:
        if declarator is None:
            return None
        if declarator.type == "function_declarator":
            return self._function_name_node(declarator.child_by_field_name("declarator"))
        if declarator.type in {
            "identifier",
            "field_identifier",
            "qualified_identifier",
            "operator_name",
            "destructor_name",
        }:
            return declarator
        child = declarator.child_by_field_name("declarator")
        if child is not None:
            found = self._function_name_node(child)
            if found is not None:
                return found
        for candidate in declarator.named_children:
            found = self._function_name_node(candidate)
            if found is not None:
                return found
        return None

    def _literal_types(self, node: Node, source: bytes) -> tuple[str, ...]:
        if node.type == "number_literal":
            text = self.node_text(node, source).casefold()
            return ("float",) if any(marker in text for marker in (".", "e", "f")) else ("int",)
        mapping = {
            "string_literal": ("string",),
            "char_literal": ("char",),
            "true": ("bool",),
            "false": ("bool",),
            "null": ("null",),
            "nullptr": ("null",),
            "initializer_list": ("list",),
        }
        return mapping.get(node.type, ())

    def _call_arguments(self, call: Node, source: bytes) -> tuple[CallArgumentRecord, ...]:
        arguments_node = call.child_by_field_name("arguments")
        if arguments_node is None:
            return ()
        return tuple(
            CallArgumentRecord(
                keyword_name=None,
                expression_kind=argument.type,
                inferred_types=self._literal_types(argument, source),
                span=self.source_span(argument),
            )
            for argument in arguments_node.named_children
        )

    def _call_usage(self, call: Node, source: bytes) -> tuple[str, tuple[str, ...]]:
        parent = call.parent
        while parent is not None and parent.type in {"parenthesized_expression"}:
            parent = parent.parent
        if parent is None:
            return "unknown", ()
        if parent.type == "expression_statement":
            return "statement", ()
        if parent.type == "return_statement":
            return "return", ()
        if parent.type in {"init_declarator", "assignment_expression"}:
            declaration = parent.parent if parent.type == "init_declarator" else None
            type_node = declaration.child_by_field_name("type") if declaration else None
            expected = (self.node_text(type_node, source),) if type_node else ()
            return "assignment", expected
        if parent.type == "argument_list":
            return "argument", ()
        if parent.type in {"if_statement", "while_statement", "conditional_expression"}:
            return "condition", ("bool",)
        return "value", ()

    def raw_structure(self, parsed, source: bytes) -> ExtractedStructure:
        definitions: list[DefinitionRecord] = []
        for _pattern, captures in self.query_matches("definitions", parsed.tree.root_node):
            if captures.get("definition.function"):
                node = captures["definition.function"][0]
                declarator = node.child_by_field_name("declarator")
                name_node = self._function_name_node(declarator)
                if name_node is None:
                    continue
                qualified_name = self.node_text(name_node, source)
                name = qualified_name.rsplit("::", 1)[-1]
                kind = "method" if "::" in qualified_name else "function"
            elif captures.get("definition.class"):
                node = captures["definition.class"][0]
                name_node = captures["definition.name"][0]
                name = qualified_name = self.node_text(name_node, source)
                kind = "class"
            else:
                node = captures["definition.struct"][0]
                name_node = captures["definition.name"][0]
                name = qualified_name = self.node_text(name_node, source)
                kind = "struct"
            body = node.child_by_field_name("body")
            definitions.append(
                DefinitionRecord(
                    kind=kind,
                    name=name,
                    qualified_name=qualified_name,
                    span=self.source_span(node),
                    body_span=self.source_span(body) if body else None,
                )
            )

        dependencies: list[DependencyRecord] = []
        for _pattern, captures in self.query_matches("dependencies", parsed.tree.root_node):
            node = captures["dependency.include"][0]
            path_node = node.child_by_field_name("path")
            raw_path = self.node_text(path_node, source) if path_node else ""
            relative = raw_path.startswith('"')
            module_name = raw_path.strip('<>"')
            dependencies.append(
                DependencyRecord(
                    kind="include",
                    module_name=module_name,
                    imported_names=(),
                    is_relative=relative,
                    span=self.source_span(node),
                )
            )

        calls: list[CallRecord] = []
        for _pattern, captures in self.query_matches("calls", parsed.tree.root_node):
            call = captures["call"][0]
            target = captures["call.target"][0]
            calls.append(
                CallRecord(
                    callee=self.node_text(target, source)[:512],
                    span=self.source_span(call),
                    arguments=self._call_arguments(call, source),
                    usage_kind=self._call_usage(call, source)[0],
                    expected_return_types=self._call_usage(call, source)[1],
                )
            )
        return ExtractedStructure(tuple(definitions), tuple(dependencies), tuple(calls))
