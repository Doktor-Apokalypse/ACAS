"""Extensible Tree-sitter language adapter API."""

from .base import (
    AdapterCapabilities,
    AdapterStatus,
    CallArgumentRecord,
    CallRecord,
    DefinitionRecord,
    DependencyRecord,
    ExtractedStructure,
    LanguageAdapter,
    ParsedSource,
    SyntaxDiagnostic,
    SourceSpan,
)
from .registry import adapter_statuses, adapters, get_adapter

__all__ = [
    "AdapterCapabilities",
    "AdapterStatus",
    "CallArgumentRecord",
    "CallRecord",
    "DefinitionRecord",
    "DependencyRecord",
    "ExtractedStructure",
    "LanguageAdapter",
    "ParsedSource",
    "SyntaxDiagnostic",
    "SourceSpan",
    "adapter_statuses",
    "adapters",
    "get_adapter",
]
