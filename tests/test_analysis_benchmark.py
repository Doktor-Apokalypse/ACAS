from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from analysis_benchmark import (
    BenchmarkExpectation,
    BenchmarkManifest,
    CleanRegion,
    ObservedFinding,
    default_mutation_recipes,
    evaluate_findings,
    load_manifest,
    materialize_mutation_corpus,
    materialize_seeded_error_corpus,
    seeded_error_fixture,
)


class AnalysisBenchmarkTests(unittest.TestCase):
    def test_metrics_count_misses_false_positives_duplicates_and_advisories(self) -> None:
        manifest = BenchmarkManifest(
            name="metrics",
            expectations=(
                BenchmarkExpectation(
                    id="expected-unbound",
                    path="mutated/demo.py",
                    source_contains="return label",
                    category="type",
                    title_pattern="assignment",
                ),
            ),
            clean_regions=(
                CleanRegion(
                    id="clean-helper",
                    path="clean/helper.py",
                    start_contains="def helper",
                ),
            ),
        )
        findings = [
            ObservedFinding(
                id="true-positive",
                analyzer="function",
                path="root/mutated/demo.py",
                start_line=4,
                end_line=4,
                severity="error",
                category="type",
                title="Local variable may be used before assignment",
                provenance="deterministic",
            ),
            ObservedFinding(
                id="duplicate",
                analyzer="function",
                path="root/mutated/demo.py",
                start_line=4,
                end_line=4,
                severity="unsafe",
                category="type",
                title="Assignment may be missing",
                provenance="model",
            ),
            ObservedFinding(
                id="false-positive",
                analyzer="function",
                path="root/mutated/demo.py",
                start_line=2,
                end_line=2,
                severity="warning",
                category="logic",
                title="Unrelated warning",
                provenance="model",
            ),
            ObservedFinding(
                id="clean-false-positive",
                analyzer="function",
                path="clean/helper.py",
                start_line=2,
                end_line=2,
                severity="warning",
                category="resource",
                title="Clean helper warning",
                provenance="model",
            ),
            ObservedFinding(
                id="advisory",
                analyzer="function",
                path="root/mutated/demo.py",
                start_line=None,
                end_line=None,
                severity="info",
                category="maintainability",
                title="Naming suggestion",
                provenance="model",
                report_tier="advisory",
            ),
        ]
        metrics = evaluate_findings(
            findings,
            manifest,
            analyzer_version="test-v1",
            sources={
                "root/mutated/demo.py": (
                    "def status(enabled):\n"
                    "    if enabled:\n"
                    "        label = 'ready'\n"
                    "    return label\n"
                ),
                "clean/helper.py": "def helper():\n    return 1\n",
            },
        )

        self.assertEqual(metrics.true_positive_count, 1)
        self.assertEqual(metrics.false_positive_count, 2)
        self.assertEqual(metrics.duplicate_count, 1)
        self.assertEqual(metrics.missed_count, 0)
        self.assertEqual(metrics.advisory_count, 1)
        self.assertEqual(metrics.unanchored_count, 1)
        self.assertEqual(metrics.clean_region_finding_count, 1)
        self.assertEqual(metrics.precision, 0.25)
        self.assertEqual(metrics.recall, 1.0)

    def test_materialized_corpus_has_clean_and_mutated_multi_language_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = materialize_mutation_corpus(directory)
            manifest = load_manifest(manifest_path)
            root = Path(directory)

            recipes = default_mutation_recipes()
            self.assertEqual({item.language for item in recipes}, {"python", "javascript", "typescript", "cpp", "csharp", "rust", "shell", "powershell"})
            self.assertEqual(len(manifest.expectations), len(recipes))
            for recipe in recipes:
                self.assertTrue((root / "clean" / f"{recipe.name}.{recipe.extension}").is_file())
                self.assertTrue((root / "mutated" / f"{recipe.name}.{recipe.extension}").is_file())

            raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(raw_manifest["format_version"], 1)

    def test_seeded_error_corpus_preserves_all_eight_known_faults(self) -> None:
        fixture = seeded_error_fixture()
        self.assertEqual(len(fixture.manifest.expectations), 8)
        self.assertEqual(
            {item.id for item in fixture.manifest.expectations},
            {
                "bad-response-quality-return",
                "undefined-method-counts",
                "undefined-input-estimate",
                "unexpected-allow-empty",
                "missing-project-id",
                "bad-compact-excerpt-return",
                "undefined-packed-values",
                "rust-adapter-syntax",
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = materialize_seeded_error_corpus(directory)
            manifest = load_manifest(manifest_path)
            root = Path(directory)
            self.assertEqual(manifest, fixture.manifest)
            for relative_path in fixture.clean_sources:
                self.assertTrue((root / "clean" / relative_path).is_file())
                self.assertTrue((root / relative_path).is_file())

    def test_missing_or_ambiguous_anchor_cannot_match_any_line(self) -> None:
        manifest = BenchmarkManifest("anchors", (BenchmarkExpectation(
            id="missing", path="demo.py", source_contains="return missing",
        ),))
        finding = ObservedFinding("wrong", "function", "demo.py", 1, 1, "error", "type", "Missing name", "model")
        for sources in ({}, {"demo.py": "return other"},
                        {"a/demo.py": "return missing", "b/demo.py": "return missing"}):
            with self.subTest(sources=sources):
                metrics = evaluate_findings([finding], manifest, analyzer_version="test", sources=sources)
                self.assertEqual(metrics.true_positive_count, 0)
                self.assertEqual(metrics.missed_count, 1)
                self.assertEqual(metrics.false_positive_count, 1)

    def test_advisory_cannot_satisfy_expected_defect(self) -> None:
        manifest = BenchmarkManifest("tiers", (BenchmarkExpectation(id="defect", path="demo.py", line=1),))
        finding = ObservedFinding("advice", "function", "demo.py", 1, 1, "info", "type", "Advice", "model", report_tier="advisory")
        metrics = evaluate_findings([finding], manifest, analyzer_version="test")
        self.assertEqual(metrics.true_positive_count, 0)
        self.assertEqual(metrics.advisory_count, 1)

    def test_syntax_mutations_and_clean_controls_use_real_grammars(self) -> None:
        from language_adapters.registry import get_adapter
        for recipe in default_mutation_recipes():
            with self.subTest(language=recipe.language, recipe=recipe.name):
                adapter = get_adapter(recipe.language)
                self.assertFalse(adapter.parse_text(recipe.clean_source).has_syntax_errors)
                if recipe.expectation.category == "syntax":
                    parsed = adapter.parse_text(recipe.mutated_source)
                    self.assertTrue(parsed.has_syntax_errors)
                    self.assertTrue(parsed.diagnostics, "Syntax errors must have a visible location, including hidden missing tokens")

    def test_fixed_manifest_resolves_source_anchors_without_fixed_line_numbers(self) -> None:
        manifest = load_manifest(
            Path(__file__).parents[1] / "analysis_benchmark_fixed.json"
        )
        metrics = evaluate_findings(
            [
                ObservedFinding(
                    id="fact",
                    analyzer="function",
                    path="Fixed/analysis_engine.py",
                    start_line=3,
                    end_line=3,
                    severity="error",
                    category="type",
                    title="Possibly undefined variable",
                    provenance="deterministic",
                    failure_type="NameError",
                )
            ],
            BenchmarkManifest(manifest.name, (manifest.expectations[0],)),
            analyzer_version="test-v2",
            sources={
                "Fixed/analysis_engine.py": (
                    "def target(facts):\n"
                    "    values = set()\n"
                    "    values.update(str(value) for value in fact[key])\n"
                )
            },
        )

        self.assertEqual(metrics.true_positive_count, 1)
        self.assertEqual(metrics.missed_count, 0)


if __name__ == "__main__":
    unittest.main()
