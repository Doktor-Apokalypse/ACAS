"""Tree-sitter structural adapters for JavaScript and TypeScript."""

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


class EcmaScriptAdapter(LanguageAdapter):
    capabilities = AdapterCapabilities(definitions=True, dependencies=True, calls=True)

    @staticmethod
    def _literal_types(node: Node) -> tuple[str, ...]:
        mapping = {
            "number": ("number",),
            "string": ("string",),
            "template_string": ("string",),
            "true": ("boolean",),
            "false": ("boolean",),
            "null": ("null",),
            "undefined": ("undefined",),
            "array": ("array",),
            "object": ("object",),
            "arrow_function": ("function",),
            "function_expression": ("function",),
        }
        return mapping.get(node.type, ())

    def _definition(self, node: Node, source: bytes) -> DefinitionRecord | None:
        function_types = {
            "function_declaration",
            "generator_function_declaration",
            "method_definition",
        }
        if node.type in function_types:
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return None
            name = self.node_text(name_node, source)
            body = node.child_by_field_name("body")
            return DefinitionRecord(
                kind="function",
                name=name,
                qualified_name=name,
                span=self.source_span(node),
                body_span=self.source_span(body) if body else None,
            )
        if node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value is None or value.type not in {
                "arrow_function",
                "function_expression",
                "generator_function",
            }:
                return None
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return None
            name = self.node_text(name_node, source)
            body = value.child_by_field_name("body")
            return DefinitionRecord(
                kind="function",
                name=name,
                qualified_name=name,
                span=self.source_span(node),
                body_span=self.source_span(body) if body else None,
            )
        if node.type == "class_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return None
            name = self.node_text(name_node, source)
            body = node.child_by_field_name("body")
            return DefinitionRecord(
                kind="class",
                name=name,
                qualified_name=name,
                span=self.source_span(node),
                body_span=self.source_span(body) if body else None,
            )
        return None

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
        while parent is not None and parent.type in {"parenthesized_expression", "await_expression"}:
            parent = parent.parent
        if parent is None:
            return "unknown", ()
        if parent.type == "expression_statement":
            return "statement", ()
        if parent.type == "return_statement":
            return "return", ()
        if parent.type in {"variable_declarator", "assignment_expression"}:
            type_node = parent.child_by_field_name("type")
            expected = (self.node_text(type_node, source).lstrip(":"),) if type_node else ()
            return "assignment", expected
        if parent.type in {"arguments", "new_expression"}:
            return "argument", ()
        if parent.type in {"if_statement", "while_statement", "ternary_expression"}:
            return "condition", ("boolean",)
        return "value", ()

    def raw_structure(self, parsed, source: bytes) -> ExtractedStructure:
        definitions: list[DefinitionRecord] = []
        dependencies: list[DependencyRecord] = []
        calls: list[CallRecord] = []
        for node in walk_nodes(parsed.tree.root_node):
            definition = self._definition(node, source)
            if definition is not None:
                definitions.append(definition)
            if node.type == "import_statement":
                source_node = node.child_by_field_name("source")
                if source_node is not None:
                    module_name = self.node_text(source_node, source).strip("'\"")
                    imported_names = tuple(
                        self.node_text(candidate.child_by_field_name("name") or candidate, source)
                        for candidate in walk_nodes(node)
                        if candidate.type == "import_specifier"
                    )
                    dependencies.append(
                        DependencyRecord(
                            kind="import",
                            module_name=module_name,
                            imported_names=imported_names,
                            is_relative=module_name.startswith("."),
                            span=self.source_span(node),
                        )
                    )
            if node.type == "call_expression":
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


class JavaScriptAdapter(EcmaScriptAdapter):
    language_id = "javascript"
    display_name = "JavaScript"
    grammar_package = "tree-sitter-javascript"

    def load_language(self) -> Language:
        import tree_sitter_javascript

        return Language(tree_sitter_javascript.language())


class TypeScriptAdapter(EcmaScriptAdapter):
    language_id = "typescript"
    display_name = "TypeScript"
    grammar_package = "tree-sitter-typescript"

    def load_language(self) -> Language:
        import tree_sitter_typescript

        # TSX is a superset of the TypeScript grammar and also covers .tsx inventory files.
        return Language(tree_sitter_typescript.language_tsx())
