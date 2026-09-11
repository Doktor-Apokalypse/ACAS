from __future__ import annotations

import unittest
from unittest.mock import patch

import analysis_engine
from source_inventory import extract_source_input


class SourceInventoryTests(unittest.TestCase):
    def test_fenced_languages_use_project_grammars(self):
        examples = {
            "js": ("function greet() { return 'hello'; }", "greet"),
            "ts": ("function greet(): string { return 'hello'; }", "greet"),
            "cpp": ("int greet() { return 1; }", "greet"),
            "csharp": ("class Greeter { int Greet() { return 1; } }", "Greet"),
            "rust": ("fn greet() -> i32 { 1 }", "greet"),
            "bash": ("greet() { printf hello; }", "greet"),
            "powershell": ("function Greet { return 1 }", "Greet"),
            "sql": ("CREATE TABLE greetings (id INTEGER);", "greetings"),
            "html": ('<!doctype html><html><body id="greeting">Hello</body></html>', "greeting"),
        }
        for language, (source, identifier) in examples.items():
            with self.subTest(language=language):
                isolated = extract_source_input(f"Please review:\n```{language}\n{source}\n```")
                inventory, facts = analysis_engine.build_verified_source_inventory(isolated.source, isolated.language)
                self.assertEqual(facts["inventory_kind"], "grammar", inventory)
                self.assertTrue(facts["parsed"], inventory)
                self.assertIn(identifier, analysis_engine.reviewable_source_identifiers(facts))
                rendered = analysis_engine.render_evidence_review(
                    analysis_engine.EvidenceReview(findings=[], correct_features=[]), source, facts)
                self.assertNotIn("Python", rendered)

    def test_unknown_explicit_language_remains_lexical(self):
        for language in ("madeup", "tsx"):
            isolated = extract_source_input(f"```{language}\nvalue = 1\n```")
            _, facts = analysis_engine.build_verified_source_inventory(isolated.source, isolated.language)
            self.assertFalse(facts["parsed"])
            self.assertEqual(facts["language"], language)

    def test_unfenced_python_preamble_is_still_isolated(self):
        with patch.object(analysis_engine, "ask_ollama", return_value='{"findings":[],"correct_features":[]}'):
            rendered, _ = analysis_engine.verify_analysis_notes(
                "Please inspect this code:\ndef greet():\n    return 'hello'\n", "greet returns hello")
        self.assertIn("Python source was parsed", rendered)

    def test_content_detection_shares_project_language_hints(self):
        for source, language in (
                ("function greet(value: string): string { return value; }", "typescript"),
                ("fn greet() -> i32 { 1 }", "rust"),
                ("function Get-Value { return 1 }", "powershell"),
                ("greet() { printf hello; }", "shell"),
        ):
            self.assertEqual(extract_source_input(source).language, language)

    def test_broken_grammar_keeps_exact_source_and_identifiers(self):
        source = "function broken() { return value + ; }"
        _, facts = analysis_engine.build_verified_source_inventory(source, "javascript")
        self.assertFalse(facts["parsed"])
        self.assertTrue(facts["diagnostics"])
        self.assertIn("broken", facts["identifiers"])

    def test_review_pipeline_preserves_non_python_source(self):
        source = "function greet() { return 'hello'; }"
        with patch.object(analysis_engine, "ask_ollama", return_value='{"findings":[],"correct_features":[]}') as ask:
            rendered, _ = analysis_engine.verify_analysis_notes(f"```js\n{source}\n```", "greet returns hello")
        self.assertIn("javascript", rendered)
        self.assertIn("function greet", ask.call_args.args[0][0]["content"])

    def test_multiple_blocks_expose_verification_scope(self):
        isolated = extract_source_input("```py\nx = 1\n```\n```js\nfunction greet() { return 1; }\n```")
        self.assertEqual(isolated.language, "javascript")
        self.assertEqual(isolated.omitted_blocks, 1)


if __name__ == "__main__":
    unittest.main()
