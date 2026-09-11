"""Source isolation and grammar-backed facts shared with project analysis.

Source text is always data. No uploaded code is imported or executed.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from language_adapters.registry import get_adapter
from project_inventory import language_from_content, shebang_language


@dataclass(frozen=True)
class SourceInput:
    source: str
    language: str | None
    omitted_blocks: int = 0


def detect_source_language(source: str, hint: str = "") -> str | None:
    aliases = {"bash": "shell", "zsh": "shell", "c#": "csharp", "rs": "rust",
               "jsx": "javascript", "pwsh": "powershell"}
    normalized = aliases.get(hint.casefold(), hint.casefold())
    if normalized:
        adapter = get_adapter(normalized)
        # Explicit unsupported language labels must not silently become Python.
        return adapter.language_id if adapter else normalized
    detected = shebang_language(source) or language_from_content(source)[0]
    if detected:
        return detected
    try:
        ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    return "python"


def extract_source_input(message: str) -> SourceInput:
    marker = re.search(
        r"(?is)---\s*BEGIN SCRIPT\s*---\s*\n?(.*?)\s*---\s*END SCRIPT\s*---", message
    )
    if marker:
        source = marker.group(1).strip()
        return SourceInput(source, detect_source_language(source))
    blocks = list(re.finditer(r"(?m)^\s*```([^\r\n`]*)\r?\n(.*?)^\s*```\s*$", message, re.S))
    if blocks:
        block = max(blocks, key=lambda item: len(item.group(2)))
        source = block.group(2).rstrip()
        hint = block.group(1).strip().split()
        return SourceInput(source, detect_source_language(source, hint[0] if hint else ""), len(blocks) - 1)
    return SourceInput(message, detect_source_language(message))


def grammar_inventory(source: str, language: str | None) -> tuple[str, dict[str, object]]:
    """Collect structural facts; parsing never implies semantic completeness."""
    identifiers = set(re.findall(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\b", source))
    imports: set[str] = set()
    functions: set[str] = set()
    classes: set[str] = set()
    calls: set[str] = set()
    facts: dict[str, object] = {
        "parsed": False, "language": language or "unknown", "identifiers": identifiers,
        "imports": imports, "functions": functions, "classes": classes, "calls": calls,
        "diagnostics": [], "inventory_kind": "lexical", "coverage": "identifiers only",
    }
    lines = [f"Language: {language or 'unknown'}"]
    adapter = get_adapter(language)
    if adapter is None:
        lines.append("No grammar adapter available; lexical identifiers only. Syntax and semantics are unknown.")
    else:
        try:
            parsed = adapter.parse_text(source)
            structure = adapter.extract_structure(parsed, source)
        except Exception as exc:
            # Missing or failed native grammars must leave evidence verification usable.
            lines.append(f"Grammar inventory unavailable ({type(exc).__name__}); lexical identifiers only.")
        else:
            facts.update(parsed=not parsed.has_syntax_errors, inventory_kind="grammar",
                         coverage="definitions, dependencies, calls and syntax diagnostics",
                         diagnostics=[item.as_dict() for item in parsed.diagnostics])
            lines.append("Grammar-derived structure; types, reachability and absence of defects are not established.")
            for definition in structure.definitions:
                identifiers.update((definition.name, definition.qualified_name))
                names = functions if definition.kind in {"function", "method"} else classes
                names.add(definition.name)
                lines.append(
                    f"{definition.kind} {definition.qualified_name}: lines {definition.span.start_line}-{definition.span.end_line}")
            for dependency in structure.dependencies:
                names = {dependency.module_name, *dependency.imported_names} - {""}
                imports.update(names)
                identifiers.update(names)
                lines.append(
                    f"Dependency at line {dependency.span.start_line}: {dependency.module_name} ({', '.join(dependency.imported_names)})")
            for call in structure.calls:
                calls.add(call.callee)
                identifiers.update((call.callee, call.callee.rsplit(".", 1)[-1]))
            lines.append("Calls: " + ", ".join(sorted(calls)))
            for diagnostic in parsed.diagnostics:
                lines.append(f"Syntax diagnostic at line {diagnostic.start_line}: {diagnostic.message}")
    lines.append("Identifiers: " + ", ".join(sorted(identifiers)[:300]))
    return "\n".join(lines), facts
