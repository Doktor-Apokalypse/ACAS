"""Tree-sitter adapter for Rust."""

from __future__ import annotations

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
from .common import walk_nodes


class RustAdapter(LanguageAdapter):
    language_id = "rust"
    display_name = "Rust"
    grammar_package = "tree-sitter-rust"
    qualification_separator = "::"
    capabilities = AdapterCapabilities(definitions=True, dependencies=True, calls=True)

    def load_language(self) -> Language:
        import tree_sitter_rust

        return Language(tree_sitter_rust.language())

    @staticmethod
    def _literal_types(node: Node) -> tuple[str, ...]:
        mapping = {
            "integer_literal": ("int",),
            "float_literal": ("float",),
            "string_literal": ("String",),
            "raw_string_literal": ("String",),
            "char_literal": ("char",),
            "boolean_literal": ("bool",),
            "array_expression": ("array",),
            "tuple_expression": ("tuple",),
            "closure_expression": ("closure",),
        }
        return mapping.get(node.type, ())

    def _arguments(self, call: Node) -> tuple[CallArgumentRecord, ...]:
        arguments = call.child_by_field_name("arguments")
        if arguments is None:
            return ()
        return tuple(
            CallArgumentRecord(
                keyword_name=None,
                expression_kind=argument.type,
                inferred_types=self._literal_types(argument),
                span=self.source_span(argument),
            )
            for argument in arguments.named_children
        )

    def _usage(self, call: Node, source: bytes) -> tuple[str, tuple[str, ...]]:
        parent = call.parent
        if parent is None:
            return "unknown", ()
        if parent.type == "expression_statement":
            return "statement", ()
        if parent.type == "return_expression":
            return "return", ()
        if parent.type == "let_declaration":
            type_node = parent.child_by_field_name("type")
            expected = (self.node_text(type_node, source),) if type_node else ()
            return "assignment", expected
        if parent.type == "arguments":
            return "argument", ()
        if parent.type in {"if_expression", "while_expression"}:
            return "condition", ("bool",)
        return "value", ()

    def raw_structure(self, parsed, source: bytes) -> ExtractedStructure:
        definitions: list[DefinitionRecord] = []
        dependencies: list[DependencyRecord] = []
        calls: list[CallRecord] = []
        for node in walk_nodes(parsed.tree.root_node):
            if node.type == "function_item":
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    name = self.node_text(name_node, source)
                    body = node.child_by_field_name("body")
                    definitions.append(
                        DefinitionRecord(
                            kind="function",
                            name=name,
                            qualified_name=name,
                            span=self.source_span(node),
                            body_span=self.source_span(body) if body else None,
                        )
                    )
            elif node.type == "impl_item":
                type_node = node.child_by_field_name("type")
                if type_node is not None:
                    name = self.node_text(type_node, source)
                    body = node.child_by_field_name("body")
                    definitions.append(
                        DefinitionRecord(
                            kind="class",
                            name=name,
                            qualified_name=name,
                            span=self.source_span(node),
                            body_span=self.source_span(body) if body else None,
                        )
                    )
            elif node.type == "struct_item":
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    name = self.node_text(name_node, source)
                    body = node.child_by_field_name("body")
                    definitions.append(
                        DefinitionRecord(
                            kind="struct",
                            name=name,
                            qualified_name=name,
                            span=self.source_span(node),
                            body_span=self.source_span(body) if body else None,
                        )
                    )
            elif node.type == "use_declaration":
                argument = node.child_by_field_name("argument")
                module_name = self.node_text(argument, source) if argument else ""
                dependencies.append(
                    DependencyRecord(
                        kind="import",
                        module_name=module_name,
                        imported_names=(),
                        is_relative=module_name.startswith(("self::", "super::")),
                        span=self.source_span(node),
                    )
                )
            elif node.type == "call_expression":
                target = node.child_by_field_name("function")
                if target is not None:
                    usage, expected = self._usage(node, source)
                    calls.append(
                        CallRecord(
                            callee=self.node_text(target, source)[:512],
                            span=self.source_span(node),
                            arguments=self._arguments(node),
                            usage_kind=usage,
                            expected_return_types=expected,
                        )
                    )
        return ExtractedStructure(tuple(definitions), tuple(dependencies), tuple(calls))
