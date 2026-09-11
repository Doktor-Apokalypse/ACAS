"""Small traversal helpers shared by grammar-specific adapters."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from .base import SourceSpan


def walk_nodes(root: Node) -> Iterator[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.named_children))


def first_descendant(node: Node | None, *types: str) -> Node | None:
    if node is None:
        return None
    wanted = set(types)
    for candidate in walk_nodes(node):
        if candidate.type in wanted:
            return candidate
    return None


def shifted_span(
    span: SourceSpan,
    *,
    byte_offset: int,
    row_offset: int,
    first_row_column_offset: int,
) -> SourceSpan:
    start_column = span.start_column
    end_column = span.end_column
    if span.start_line == 1:
        start_column += first_row_column_offset
    if span.end_line == 1:
        end_column += first_row_column_offset
    return SourceSpan(
        start_line=span.start_line + row_offset,
        start_column=start_column,
        end_line=span.end_line + row_offset,
        end_column=end_column,
        start_byte=span.start_byte + byte_offset,
        end_byte=span.end_byte + byte_offset,
    )
