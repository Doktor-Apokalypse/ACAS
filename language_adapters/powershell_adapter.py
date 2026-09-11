"""Tree-sitter adapter for PowerShell."""

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


class PowerShellAdapter(LanguageAdapter):
    language_id = "powershell"
    display_name = "PowerShell"
    grammar_package = "tree-sitter-powershell"
    capabilities = AdapterCapabilities(definitions=True, dependencies=True, calls=True)

    def load_language(self) -> Language:
        import tree_sitter_powershell

        return Language(tree_sitter_powershell.language())

    @staticmethod
    def _literal_types(node: Node) -> tuple[str, ...]:
        mapping = {
            "integer_literal": ("int",),
            "decimal_integer_literal": ("int",),
            "real_literal": ("double",),
            "string_literal": ("string",),
            "expandable_string_literal": ("string",),
            "boolean_literal": ("bool",),
            "array_literal_expression": ("array",),
            "hash_literal_expression": ("hashtable",),
        }
        if node.type == "array_literal_expression":
            inferred = {
                value
                for candidate in walk_nodes(node)
                for value in mapping.get(candidate.type, ())
                if candidate is not node
            }
            if len(inferred) == 1:
                return tuple(inferred)
        return mapping.get(node.type, ())

    def raw_structure(self, parsed, source: bytes) -> ExtractedStructure:
        definitions: list[DefinitionRecord] = []
        dependencies: list[DependencyRecord] = []
        calls: list[CallRecord] = []
        for node in walk_nodes(parsed.tree.root_node):
            if node.type == "function_statement":
                name_node = node.child_by_field_name("name") or first_descendant(
                    node, "function_name"
                )
                if name_node is not None:
                    name = self.node_text(name_node, source)
                    body = node.child_by_field_name("body") or first_descendant(
                        node, "script_block_body"
                    )
                    definitions.append(
                        DefinitionRecord(
                            kind="function",
                            name=name,
                            qualified_name=name,
                            span=self.source_span(node),
                            body_span=self.source_span(body) if body else None,
                        )
                    )
            elif node.type == "command":
                name_node = node.child_by_field_name("command_name") or first_descendant(
                    node, "command_name"
                )
                if name_node is None:
                    continue
                callee = self.node_text(name_node, source)
                elements = node.child_by_field_name("command_elements") or first_descendant(
                    node, "command_elements"
                )
                argument_nodes = (
                    [
                        child
                        for child in elements.named_children
                        if child.type != "command_argument_sep"
                    ]
                    if elements is not None
                    else []
                )
                if callee.casefold() in {"import-module", "."} and argument_nodes:
                    module_name = self.node_text(argument_nodes[0], source).strip("'\"")
                    dependencies.append(
                        DependencyRecord(
                            kind="import",
                            module_name=module_name,
                            imported_names=(),
                            is_relative=(
                                module_name.startswith((".", "\\"))
                                or "/" in module_name
                                or module_name.casefold().endswith((".psm1", ".ps1"))
                            ),
                            span=self.source_span(node),
                        )
                    )
                    continue
                arguments = tuple(
                    CallArgumentRecord(
                        keyword_name=None,
                        expression_kind=argument.type,
                        inferred_types=self._literal_types(argument),
                        span=self.source_span(argument),
                    )
                    for argument in argument_nodes
                )
                parent_types = {candidate.type for candidate in (node.parent, node.parent.parent if node.parent else None) if candidate}
                usage = "assignment" if "assignment_expression" in parent_types else "statement"
                calls.append(
                    CallRecord(
                        callee=callee[:512],
                        span=self.source_span(node),
                        arguments=arguments,
                        usage_kind=usage,
                    )
                )
        return ExtractedStructure(tuple(definitions), tuple(dependencies), tuple(calls))
