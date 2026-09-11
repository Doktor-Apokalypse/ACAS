import unittest

from dependency_context import dependency_order
from deterministic_rules import javascript_null_member_issues, typescript_signature


class DependencyOrderTests(unittest.TestCase):
    def test_dependencies_precede_callers_and_unrelated_order_is_stable(self):
        ordered, cyclic = dependency_order([1, 2, 3, 4], [(1, 3), (3, 2)])
        self.assertEqual(ordered, [2, 3, 1, 4])
        self.assertEqual(cyclic, set())

    def test_recursive_components_are_kept_together(self):
        ordered, cyclic = dependency_order([1, 2, 3, 4, 5], [(1, 2), (2, 3), (3, 2), (3, 4), (5, 5)])
        self.assertEqual(ordered, [4, 2, 3, 1, 5])
        self.assertEqual(cyclic, {2, 3, 5})

    def test_deep_graph_does_not_use_python_recursion(self):
        nodes = list(range(5_000))
        ordered, cyclic = dependency_order(nodes, [(node, node + 1) for node in nodes[:-1]])
        self.assertEqual(ordered, list(reversed(nodes)))
        self.assertFalse(cyclic)


class DeterministicJavaScriptTests(unittest.TestCase):
    def test_typescript_declared_parameters_override_guesses(self):
        parameters, returns = typescript_signature(
            "function label(value: string, suffix?: string, count: number = 1): string { return value; }")
        self.assertEqual([item["required"] for item in parameters], [True, False, False])
        self.assertEqual([item["accepted_types"] for item in parameters],
                         [["string"], ["string", "undefined"], ["number", "undefined"]])
        self.assertEqual(returns, "string")

    def test_typescript_destructuring_is_not_guessed(self):
        self.assertIsNone(typescript_signature("function label({value}: {value: string}): string { return value; }"))

    def test_null_access_has_exact_evidence_and_absolute_lines(self):
        for language in ("javascript", "typescript"):
            issues = javascript_null_member_issues("function value() {\n  return null.name;\n}", language, 10)
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0]["start_line"], 11)
            self.assertEqual(issues[0]["evidence"], "null.name")
            self.assertEqual(issues[0]["provenance"], "deterministic")

    def test_clean_guards_handlers_comments_and_nested_functions_are_not_flagged(self):
        sources = [
            "function value() { return null?.name; }",
            "function value() { return ({name: 'ok'}).name; }",
            "function value() { return false && null.name; }",
            "function value() { if (false) return null.name; return 1; }",
            "function value() { try { return null.name; } catch { return 1; } }",
            "function value() { return 'null.name'; }",
            "function value() { return () => null.name; }",
            "function value() { /* return null.name; */ return 1; }",
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(javascript_null_member_issues(source, "javascript", 1), [])
