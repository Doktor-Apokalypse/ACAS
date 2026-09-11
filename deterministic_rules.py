"""Small grammar-backed defect rules with deliberately narrow coverage."""

from __future__ import annotations

from language_adapters.registry import get_adapter


def typescript_signature(source: str) -> tuple[list[dict[str, object]], str | None] | None:
    """Extract ordinary named parameters; leave unsupported signature shapes unknown."""
    adapter = get_adapter("typescript")
    if adapter is None:
        return None
    try:
        parsed = adapter.parse_text(source)
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        return None
    if parsed.has_syntax_errors:
        return None
    nodes = [node for node in parsed.tree.root_node.named_children if node.type == "function_declaration"]
    if len(nodes) != 1:
        return None
    function = nodes[0]
    formal = function.child_by_field_name("parameters")
    if formal is None:
        return None
    encoded = source.encode("utf-8")
    parameters = []
    for node in formal.named_children:
        if node.type == "comment":
            continue
        name = node.child_by_field_name("pattern")
        if node.type not in {"required_parameter", "optional_parameter"} or name is None or name.type != "identifier":
            return None
        annotation = node.child_by_field_name("type")
        type_name = adapter.node_text(annotation, encoded).lstrip(":").strip() if annotation else "unknown"
        default = node.child_by_field_name("value")
        parameter_name = adapter.node_text(name, encoded)
        if len(type_name) > 160 or len(parameter_name) > 160 or len(parameters) >= 100:
            return None
        if parameter_name == "this":
            continue
        required = node.type == "required_parameter" and default is None
        parameters.append({
            "name": parameter_name, "kind": "positional_only",
            "required": required,
            "accepted_types": [type_name or "unknown",
                               *(["undefined"] if not required and type_name != "undefined" else [])],
            "default_description": adapter.node_text(default, encoded)[:500] if default else None,
            "description": "Source-declared TypeScript parameter; unknown denotes a missing annotation.",
        })
    return_node = function.child_by_field_name("return_type")
    return_type = adapter.node_text(return_node, encoded).lstrip(":").strip() if return_node else None
    return parameters, return_type if return_type and len(return_type) <= 160 else None


def javascript_null_member_issues(source: str, language: str, start_line: int) -> list[dict[str, object]]:
    """Prove null member access in a sole unconditional return statement.

    Avoid regex claims about guarded branches, optional chaining, catches, nested
    functions or comments. Other shapes remain available for model review.
    """
    if language not in {"javascript", "typescript"}:
        return []
    adapter = get_adapter(language)
    if adapter is None:
        return []
    try:
        parsed = adapter.parse_text(source)
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        return []
    if parsed.has_syntax_errors:
        return []
    functions = [node for node in parsed.tree.root_node.named_children if node.type == "function_declaration"]
    if len(functions) != 1:
        return []
    body = functions[0].child_by_field_name("body")
    if body is None:
        return []
    statements = [node for node in body.named_children if node.type != "comment"]
    if len(statements) != 1 or statements[0].type != "return_statement":
        return []
    expressions = statements[0].named_children
    if len(expressions) != 1 or expressions[0].type != "member_expression":
        return []
    member = expressions[0]
    obj = member.child_by_field_name("object")
    if obj is None or obj.type != "null" or any(child.type == "optional_chain" for child in member.children):
        return []
    evidence = adapter.node_text(member, source.encode("utf-8"))
    line = start_line + member.start_point.row
    return [{
        "severity": "error", "category": "runtime", "title": "Property access on null",
        "description": "The unconditional return evaluates a property of the literal null, raising TypeError.",
        "start_line": line, "end_line": start_line + member.end_point.row,
        "proof": "source-v1", "evidence": evidence, "failure_type": "TypeError",
        "trigger": "Execution reaches the sole return statement and accesses a property of null.",
        "reachability": "Function body entry -> unconditional return -> null property access.",
        "guard_check": "The body contains only this return, with no guard, handler or optional chain.",
        "guard_evidence": [], "provenance": "deterministic",
    }]
