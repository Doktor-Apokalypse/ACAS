"""Tree-sitter adapter for SQL routines and invocations."""

from __future__ import annotations

from tree_sitter import Language, Node

from .base import (
    AdapterCapabilities,
    CallArgumentRecord,
    CallRecord,
    DefinitionRecord,
    ExtractedStructure,
    LanguageAdapter,
)
from .common import first_descendant, walk_nodes


class SqlAdapter(LanguageAdapter):
    language_id = "sql"
    display_name = "SQL"
    grammar_package = "tree-sitter-sql"
    capabilities = AdapterCapabilities(definitions=True, calls=True)

    def load_language(self) -> Language:
        import tree_sitter_sql

        return Language(tree_sitter_sql.language())

    def _literal_types(self, node: Node, source: bytes) -> tuple[str, ...]:
        text = self.node_text(node, source).strip()
        if node.type in {"literal", "number"}:
            if text.startswith(("'", '"')):
                return ("text",)
            if text.casefold() in {"true", "false"}:
                return ("boolean",)
            if text.casefold() == "null":
                return ("null",)
            return ("numeric",) if any(marker in text for marker in (".", "e", "E")) else ("integer",)
        return ()

    def raw_structure(self, parsed, source: bytes) -> ExtractedStructure:
        definitions: list[DefinitionRecord] = []
        calls: list[CallRecord] = []
        for node in walk_nodes(parsed.tree.root_node):
            if node.type in {"create_function", "create_procedure"}:
                reference = first_descendant(node, "object_reference")
                name_node = reference.child_by_field_name("name") if reference else None
                if name_node is not None:
                    name = self.node_text(name_node, source)
                    body = first_descendant(node, "function_body", "procedure_body")
                    definitions.append(
                        DefinitionRecord(
                            kind="function",
                            name=name,
                            qualified_name=name,
                            span=self.source_span(node),
                            body_span=self.source_span(body) if body else None,
                        )
                    )
            elif node.type == "invocation":
                reference = first_descendant(node, "object_reference")
                if reference is None:
                    continue
                callee = self.node_text(reference, source)
                parameters = node.children_by_field_name("parameter")
                if not parameters:
                    parameters = [
                        child for child in node.named_children if child is not reference
                    ]
                arguments = []
                for parameter in parameters:
                    value = first_descendant(parameter, "literal") or parameter
                    arguments.append(
                        CallArgumentRecord(
                            keyword_name=None,
                            expression_kind=parameter.type,
                            inferred_types=self._literal_types(value, source),
                            span=self.source_span(parameter),
                        )
                    )
                calls.append(
                    CallRecord(
                        callee=callee[:512],
                        span=self.source_span(node),
                        arguments=tuple(arguments),
                        usage_kind="value",
                    )
                )
        return ExtractedStructure(tuple(definitions), (), tuple(calls))
