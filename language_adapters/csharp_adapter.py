"""Tree-sitter adapter for C#."""

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
from .common import first_descendant, walk_nodes


class CSharpAdapter(LanguageAdapter):
    language_id = "csharp"
    display_name = "C#"
    grammar_package = "tree-sitter-c-sharp"
    capabilities = AdapterCapabilities(definitions=True, dependencies=True, calls=True)

    def load_language(self) -> Language:
        import tree_sitter_c_sharp

        return Language(tree_sitter_c_sharp.language())

    @staticmethod
    def _literal_types(node: Node) -> tuple[str, ...]:
        mapping = {
            "integer_literal": ("int",),
            "real_literal": ("double",),
            "string_literal": ("string",),
            "verbatim_string_literal": ("string",),
            "character_literal": ("char",),
            "boolean_literal": ("bool",),
            "null_literal": ("null",),
            "array_creation_expression": ("array",),
            "implicit_array_creation_expression": ("array",),
            "lambda_expression": ("delegate",),
        }
        return mapping.get(node.type, ())

    def _arguments(self, call: Node, source: bytes) -> tuple[CallArgumentRecord, ...]:
        arguments = call.child_by_field_name("arguments")
        if arguments is None:
            return ()
        records: list[CallArgumentRecord] = []
        for argument in arguments.named_children:
            value = argument.child_by_field_name("value") or (
                argument.named_children[-1] if argument.named_children else argument
            )
            name_node = argument.child_by_field_name("name")
            records.append(
                CallArgumentRecord(
                    keyword_name=self.node_text(name_node, source) if name_node else None,
                    expression_kind=argument.type,
                    inferred_types=self._literal_types(value),
                    span=self.source_span(argument),
                )
            )
        return tuple(records)

    def _usage(self, call: Node, source: bytes) -> tuple[str, tuple[str, ...]]:
        parent = call.parent
        while parent is not None and parent.type in {"parenthesized_expression", "await_expression"}:
            parent = parent.parent
        if parent is None:
            return "unknown", ()
        if parent.type == "expression_statement":
            return "statement", ()
        if parent.type == "return_statement":
            return "return", ()
        if parent.type in {"variable_declarator", "assignment_expression"}:
            declaration = parent.parent
            type_node = declaration.child_by_field_name("type") if declaration else None
            expected = (self.node_text(type_node, source),) if type_node else ()
            return "assignment", expected
        if parent.type == "argument":
            return "argument", ()
        if parent.type in {"if_statement", "while_statement", "conditional_expression"}:
            return "condition", ("bool",)
        return "value", ()

    def raw_structure(self, parsed, source: bytes) -> ExtractedStructure:
        definitions: list[DefinitionRecord] = []
        dependencies: list[DependencyRecord] = []
        calls: list[CallRecord] = []
        for node in walk_nodes(parsed.tree.root_node):
            if node.type in {"class_declaration", "struct_declaration", "interface_declaration"}:
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    name = self.node_text(name_node, source)
                    body = node.child_by_field_name("body")
                    definitions.append(
                        DefinitionRecord(
                            kind="struct" if node.type == "struct_declaration" else "class",
                            name=name,
                            qualified_name=name,
                            span=self.source_span(node),
                            body_span=self.source_span(body) if body else None,
                        )
                    )
            elif node.type in {
                "method_declaration",
                "constructor_declaration",
                "local_function_statement",
            }:
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
            elif node.type == "using_directive":
                module_name = self.node_text(node, source).strip().removeprefix("using ").rstrip(";")
                if "=" in module_name:
                    module_name = module_name.split("=", 1)[1].strip()
                dependencies.append(
                    DependencyRecord(
                        kind="import",
                        module_name=module_name,
                        imported_names=(),
                        is_relative=False,
                        span=self.source_span(node),
                    )
                )
            elif node.type == "invocation_expression":
                target = node.child_by_field_name("function") or first_descendant(
                    node, "identifier", "member_access_expression"
                )
                if target is not None:
                    usage, expected = self._usage(node, source)
                    calls.append(
                        CallRecord(
                            callee=self.node_text(target, source)[:512],
                            span=self.source_span(node),
                            arguments=self._arguments(node, source),
                            usage_kind=usage,
                            expected_return_types=expected,
                        )
                    )
        return ExtractedStructure(tuple(definitions), tuple(dependencies), tuple(calls))
