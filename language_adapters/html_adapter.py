"""HTML syntax adapter with embedded JavaScript structural extraction."""

from __future__ import annotations

from dataclasses import replace

from tree_sitter import Language

from .base import (
    AdapterCapabilities,
    CallArgumentRecord,
    DependencyRecord,
    ExtractedStructure,
    LanguageAdapter,
)
from .common import shifted_span, walk_nodes
from .ecmascript_adapter import JavaScriptAdapter


class HtmlAdapter(LanguageAdapter):
    language_id = "html"
    display_name = "HTML"
    grammar_package = "tree-sitter-html"
    capabilities = AdapterCapabilities(
        definitions=True,
        dependencies=True,
        calls=True,
        embedded_languages=True,
    )

    def __init__(self) -> None:
        super().__init__()
        self._javascript = JavaScriptAdapter()

    def load_language(self) -> Language:
        import tree_sitter_html

        return Language(tree_sitter_html.language())

    def _shift(self, span, raw_text):
        return shifted_span(
            span,
            byte_offset=raw_text.start_byte,
            row_offset=raw_text.start_point.row,
            first_row_column_offset=raw_text.start_point.column,
        )

    def raw_structure(self, parsed, source: bytes) -> ExtractedStructure:
        definitions = []
        dependencies = []
        calls = []
        for node in walk_nodes(parsed.tree.root_node):
            if node.type != "script_element":
                continue
            start_tag = next(
                (child for child in node.named_children if child.type == "start_tag"), None
            )
            if start_tag is not None:
                for attribute in (
                    child for child in start_tag.named_children if child.type == "attribute"
                ):
                    children = attribute.named_children
                    if len(children) < 2:
                        continue
                    name = self.node_text(children[0], source).casefold()
                    if name != "src":
                        continue
                    module_name = self.node_text(children[1], source).strip("'\"")
                    dependencies.append(
                        DependencyRecord(
                            kind="import",
                            module_name=module_name,
                            imported_names=(),
                            is_relative=not module_name.casefold().startswith(
                                ("http://", "https://", "//", "/")
                            ),
                            span=self.source_span(attribute),
                        )
                    )
            raw_text = next(
                (child for child in node.named_children if child.type == "raw_text"), None
            )
            if raw_text is None or raw_text.end_byte <= raw_text.start_byte:
                continue
            text = source[raw_text.start_byte : raw_text.end_byte].decode(
                "utf-8", errors="replace"
            )
            embedded_parsed = self._javascript.parse_text(text)
            embedded = self._javascript.raw_structure(embedded_parsed, text.encode("utf-8"))
            for definition in embedded.definitions:
                definitions.append(
                    replace(
                        definition,
                        span=self._shift(definition.span, raw_text),
                        body_span=(
                            self._shift(definition.body_span, raw_text)
                            if definition.body_span
                            else None
                        ),
                    )
                )
            for dependency in embedded.dependencies:
                dependencies.append(
                    replace(dependency, span=self._shift(dependency.span, raw_text))
                )
            for call in embedded.calls:
                arguments = tuple(
                    CallArgumentRecord(
                        keyword_name=argument.keyword_name,
                        expression_kind=argument.expression_kind,
                        inferred_types=argument.inferred_types,
                        span=self._shift(argument.span, raw_text),
                    )
                    for argument in call.arguments
                )
                calls.append(
                    replace(
                        call,
                        span=self._shift(call.span, raw_text),
                        arguments=arguments,
                    )
                )
        return ExtractedStructure(tuple(definitions), tuple(dependencies), tuple(calls))
