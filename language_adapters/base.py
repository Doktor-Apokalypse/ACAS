"""Shared Tree-sitter adapter contracts and syntax diagnostics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from importlib.metadata import PackageNotFoundError, version
from typing import Iterator, Mapping

from tree_sitter import Language, Node, Parser, Query, QueryCursor, Tree


MAX_DIAGNOSTICS = 100


@dataclass(frozen=True)
class AdapterCapabilities:
    """Grammar-backed structures an adapter knows how to locate."""

    syntax_diagnostics: bool = True
    definitions: bool = False
    dependencies: bool = False
    calls: bool = False
    semantic_types: bool = False
    embedded_languages: bool = False


@dataclass(frozen=True)
class SyntaxDiagnostic:
    kind: str
    message: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    start_byte: int
    end_byte: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ParsedSource:
    language_id: str
    tree: Tree
    diagnostics: tuple[SyntaxDiagnostic, ...]
    error_count: int
    missing_count: int
    diagnostics_truncated: bool

    @property
    def has_syntax_errors(self) -> bool:
        return bool(self.error_count or self.missing_count or self.tree.root_node.has_error)


@dataclass(frozen=True)
class SourceSpan:
    """One-based inclusive lines plus exact normalized UTF-8 byte offsets."""

    start_line: int
    start_column: int
    end_line: int
    end_column: int
    start_byte: int
    end_byte: int


@dataclass(frozen=True)
class DefinitionRecord:
    kind: str
    name: str
    qualified_name: str
    span: SourceSpan
    body_span: SourceSpan | None = None
    parent_index: int | None = None
    nesting_depth: int = 0


@dataclass(frozen=True)
class DependencyRecord:
    kind: str
    module_name: str
    imported_names: tuple[str, ...]
    is_relative: bool
    span: SourceSpan


@dataclass(frozen=True)
class CallArgumentRecord:
    keyword_name: str | None
    expression_kind: str
    inferred_types: tuple[str, ...]
    span: SourceSpan


@dataclass(frozen=True)
class CallRecord:
    callee: str
    span: SourceSpan
    caller_index: int | None = None
    arguments: tuple[CallArgumentRecord, ...] = ()
    usage_kind: str = "unknown"
    expected_return_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtractedStructure:
    definitions: tuple[DefinitionRecord, ...]
    dependencies: tuple[DependencyRecord, ...]
    calls: tuple[CallRecord, ...]


@dataclass(frozen=True)
class AdapterStatus:
    language: str
    display_name: str
    available: bool
    grammar_name: str | None
    grammar_package: str
    grammar_version: str | None
    abi_version: int | None
    semantic_version: str | None
    capabilities: AdapterCapabilities
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["capabilities"] = asdict(self.capabilities)
        return value


class LanguageAdapter:
    """Base class for one language grammar and its grammar-specific queries."""

    language_id = ""
    display_name = ""
    grammar_package = ""
    capabilities = AdapterCapabilities()
    queries: Mapping[str, str] = {}
    qualification_separator = "."

    def __init__(self) -> None:
        self._language: Language | None = None

    def load_language(self) -> Language:
        raise NotImplementedError

    @property
    def language(self) -> Language:
        if self._language is None:
            self._language = self.load_language()
        return self._language

    def new_parser(self) -> Parser:
        """Return a fresh parser; parser instances are not shared across requests."""
        return Parser(self.language)

    def compile_queries(self) -> dict[str, Query]:
        return {name: Query(self.language, source) for name, source in self.queries.items()}

    def query_matches(
        self,
        name: str,
        root: Node,
    ) -> Iterator[tuple[int, dict[str, list[Node]]]]:
        """Yield query matches while keeping native query state alive.

        On Windows, captured ``Node`` objects from ``tree_sitter.QueryCursor``
        can become unsafe if the cursor/query objects are destroyed before the
        caller reads node point/range fields.  Streaming from this generator
        keeps those native objects referenced for the duration of iteration.
        """
        query = Query(self.language, self.queries[name])
        cursor = QueryCursor(query)
        yield from cursor.matches(root)

    @staticmethod
    def node_text(node: Node, source: bytes) -> str:
        return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    @staticmethod
    def source_span(node: Node) -> SourceSpan:
        start_line = node.start_point.row + 1
        end_line = node.end_point.row + 1
        if node.end_point.column == 0 and node.end_byte > node.start_byte:
            end_line = max(start_line, end_line - 1)
        return SourceSpan(
            start_line=start_line,
            start_column=node.start_point.column + 1,
            end_line=end_line,
            end_column=node.end_point.column + 1,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
        )

    def raw_structure(
        self,
        parsed: ParsedSource,
        source: bytes,
    ) -> ExtractedStructure:
        raise NotImplementedError

    def extract_structure(self, parsed: ParsedSource, text: str) -> ExtractedStructure:
        """Extract records and attach deterministic nesting/caller relationships."""
        source = text.encode("utf-8")
        raw = self.raw_structure(parsed, source)
        definitions = sorted(
            raw.definitions,
            key=lambda item: (item.span.start_byte, -item.span.end_byte, item.kind, item.name),
        )
        completed: list[DefinitionRecord] = []
        for definition in definitions:
            parent_index: int | None = None
            for candidate_index in range(len(completed) - 1, -1, -1):
                candidate = completed[candidate_index]
                if (
                    candidate.span.start_byte <= definition.span.start_byte
                    and candidate.span.end_byte >= definition.span.end_byte
                    and candidate.span != definition.span
                ):
                    parent_index = candidate_index
                    break
            parent = completed[parent_index] if parent_index is not None else None
            qualified_name = definition.qualified_name
            if parent is not None and self.qualification_separator not in qualified_name:
                qualified_name = (
                    parent.qualified_name + self.qualification_separator + qualified_name
                )
            kind = definition.kind
            if kind == "function" and parent is not None and parent.kind in {"class", "struct"}:
                kind = "method"
            completed.append(
                replace(
                    definition,
                    kind=kind,
                    qualified_name=qualified_name,
                    parent_index=parent_index,
                    nesting_depth=(parent.nesting_depth + 1) if parent is not None else 0,
                )
            )

        calls: list[CallRecord] = []
        for call in sorted(raw.calls, key=lambda item: (item.span.start_byte, item.span.end_byte)):
            caller_index: int | None = None
            for candidate_index in range(len(completed) - 1, -1, -1):
                candidate = completed[candidate_index]
                if (
                    candidate.span.start_byte <= call.span.start_byte
                    and candidate.span.end_byte >= call.span.end_byte
                ):
                    caller_index = candidate_index
                    break
            calls.append(replace(call, caller_index=caller_index))
        return ExtractedStructure(
            definitions=tuple(completed),
            dependencies=tuple(
                sorted(
                    raw.dependencies,
                    key=lambda item: (item.span.start_byte, item.module_name, item.kind),
                )
            ),
            calls=tuple(calls),
        )

    def validate(self) -> None:
        if not self.language_id or not self.display_name or not self.grammar_package:
            raise ValueError("Adapter identity metadata is incomplete")
        self.compile_queries()

    def parse_text(self, text: str) -> ParsedSource:
        """Parse normalized UTF-8 source and collect bounded syntax diagnostics."""
        tree = self.new_parser().parse(text.encode("utf-8"))
        diagnostics: list[SyntaxDiagnostic] = []
        error_count = 0
        missing_count = 0
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            kind: str | None = None
            message = ""
            if node.is_error:
                error_count += 1
                kind = "error"
                message = "Unexpected or unparsed syntax"
            elif node.is_missing:
                missing_count += 1
                kind = "missing"
                message = f"Missing {node.type}"
            elif node.has_error and not any(child.has_error for child in node.children):
                # Some grammars hide a missing token inside a visible wrapper:
                # has_error is true, but neither is_missing nor is_error is set
                # on an exposed node (for example C#'s _identifier_token).
                if node.start_byte == node.end_byte:
                    missing_count += 1
                    kind = "missing"
                    message = f"Missing syntax within {node.type}"
                else:
                    error_count += 1
                    kind = "error"
                    message = f"Incomplete syntax within {node.type}"
            if kind and len(diagnostics) < MAX_DIAGNOSTICS:
                diagnostics.append(
                    SyntaxDiagnostic(
                        kind=kind,
                        message=message,
                        start_line=node.start_point.row + 1,
                        start_column=node.start_point.column + 1,
                        end_line=node.end_point.row + 1,
                        end_column=node.end_point.column + 1,
                        start_byte=node.start_byte,
                        end_byte=node.end_byte,
                    )
                )
            stack.extend(reversed(node.children))
        total = error_count + missing_count
        return ParsedSource(
            language_id=self.language_id,
            tree=tree,
            diagnostics=tuple(diagnostics),
            error_count=error_count,
            missing_count=missing_count,
            diagnostics_truncated=total > len(diagnostics),
        )

    def status(self) -> AdapterStatus:
        grammar_name: str | None = None
        grammar_version: str | None = None
        abi_version: int | None = None
        semantic_version: str | None = None
        error: str | None = None
        try:
            self.validate()
            grammar_name = self.language.name or self.language_id
            abi_version = self.language.abi_version
            if self.language.semantic_version is not None:
                semantic_version = ".".join(map(str, self.language.semantic_version))
            try:
                grammar_version = version(self.grammar_package)
            except PackageNotFoundError:
                grammar_version = None
            available = True
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
            available = False
            error = type(exc).__name__
        return AdapterStatus(
            language=self.language_id,
            display_name=self.display_name,
            available=available,
            grammar_name=grammar_name,
            grammar_package=self.grammar_package,
            grammar_version=grammar_version,
            abi_version=abi_version,
            semantic_version=semantic_version,
            capabilities=self.capabilities,
            error=error,
        )
