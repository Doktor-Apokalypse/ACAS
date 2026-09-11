"""Central registry for installed project-language adapters."""

from __future__ import annotations

from .base import AdapterStatus, LanguageAdapter
from .csharp_adapter import CSharpAdapter
from .cpp_adapter import CppAdapter
from .ecmascript_adapter import JavaScriptAdapter, TypeScriptAdapter
from .html_adapter import HtmlAdapter
from .powershell_adapter import PowerShellAdapter
from .python_adapter import PythonAdapter
from .rust_adapter import RustAdapter
from .shell_adapter import ShellAdapter
from .sql_adapter import SqlAdapter


_ADAPTERS: tuple[LanguageAdapter, ...] = (
    PythonAdapter(),
    CppAdapter(),
    CSharpAdapter(),
    JavaScriptAdapter(),
    TypeScriptAdapter(),
    RustAdapter(),
    ShellAdapter(),
    PowerShellAdapter(),
    HtmlAdapter(),
    SqlAdapter(),
)
_ALIASES = {
    "c": "cpp",
    "c++": "cpp",
    "cs": "csharp",
    "js": "javascript",
    "py": "python",
    "ps1": "powershell",
    "sh": "shell",
    "ts": "typescript",
}
_BY_LANGUAGE = {adapter.language_id: adapter for adapter in _ADAPTERS}


def get_adapter(language: str | None) -> LanguageAdapter | None:
    if not language:
        return None
    normalized = language.strip().casefold()
    return _BY_LANGUAGE.get(_ALIASES.get(normalized, normalized))


def adapters() -> tuple[LanguageAdapter, ...]:
    return _ADAPTERS


def adapter_statuses() -> tuple[AdapterStatus, ...]:
    return tuple(adapter.status() for adapter in _ADAPTERS)
