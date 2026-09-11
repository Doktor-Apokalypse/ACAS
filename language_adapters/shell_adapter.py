"""Tree-sitter adapter for POSIX-style shell scripts parsed by the Bash grammar."""

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


class ShellAdapter(LanguageAdapter):
    language_id = "shell"
    display_name = "Shell"
    grammar_package = "tree-sitter-bash"
    capabilities = AdapterCapabilities(definitions=True, dependencies=True, calls=True)

    def load_language(self) -> Language:
        import tree_sitter_bash

        return Language(tree_sitter_bash.language())

    @staticmethod
    def _literal_types(node: Node) -> tuple[str, ...]:
        if any(
            candidate.type in {"simple_expansion", "command_substitution"}
            for candidate in walk_nodes(node)
        ):
            return ()
        if node.type == "number":
            return ("number",)
        if node.type in {"string", "raw_string", "ansi_c_string", "word"}:
            return ("string",)
        return ()

    def raw_structure(self, parsed, source: bytes) -> ExtractedStructure:
        definitions: list[DefinitionRecord] = []
        dependencies: list[DependencyRecord] = []
        calls: list[CallRecord] = []
        for node in walk_nodes(parsed.tree.root_node):
            if node.type == "function_definition":
                name_node = node.child_by_field_name("name") or first_descendant(node, "word")
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
            elif node.type == "command":
                name_container = node.child_by_field_name("name")
                name_node = first_descendant(name_container, "word", "command_name")
                if name_node is None:
                    continue
                callee = self.node_text(name_node, source)
                argument_nodes = node.children_by_field_name("argument")
                if not argument_nodes:
                    argument_nodes = [
                        child for child in node.named_children if child is not name_container
                    ]
                if callee in {"source", "."} and argument_nodes:
                    module_name = self.node_text(argument_nodes[0], source).strip("'\"")
                    dependencies.append(
                        DependencyRecord(
                            kind="import",
                            module_name=module_name,
                            imported_names=(),
                            is_relative=not module_name.startswith("/"),
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
                usage = "value" if node.parent and node.parent.type == "command_substitution" else "statement"
                calls.append(
                    CallRecord(
                        callee=callee[:512],
                        span=self.source_span(node),
                        arguments=arguments,
                        usage_kind=usage,
                    )
                )
        return ExtractedStructure(tuple(definitions), tuple(dependencies), tuple(calls))
