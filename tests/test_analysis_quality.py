from __future__ import annotations

import json
import unittest
from unittest.mock import Mock

import analysis_engine
import main
from analysis_benchmark import BenchmarkManifest, CleanRegion, project_benchmark_details
from analysis_quality import project_review_quality, response_quality
from project_function_analysis import analyze_project_functions, merge_function_chunk_analyses
from tests.helpers import DatabaseTestCase
from tests import test_function_analysis as function_tests

valid_result = function_tests.valid_result


class ResponseQualityTests(unittest.TestCase):
    def test_empty_schema_echo_and_generic_payloads_are_rejected(self):
        generic = valid_result().model_dump()
        generic["summary"] = "Function analysis completed."
        for payload in ({}, {"properties": {}}, generic, {"summary": "Returns a value."}):
            with self.subTest(payload=payload), self.assertRaises(analysis_engine.FunctionAnalysisResponseError):
                analysis_engine.normalize_function_analysis_payload(json.dumps(payload))

    def test_bad_proof_does_not_discard_valid_contract_or_manufacture_reasoning(self):
        for value in ([], "", ["if value is None:"]):
            raw = valid_result(issue_line=2).model_dump()
            raw["issues"][0]["guard_check"] = value
            result = analysis_engine.normalize_function_analysis_payload(json.dumps(raw))
            self.assertEqual(result.parameters[0].accepted_types, ["int"])
            self.assertEqual(result.review_status, "partial")
            self.assertIsNone(result.issues[0].guard_check)
            self.assertTrue(result.validation_notes)
            self.assertLessEqual(result.confidence, .5)

    def test_observed_nested_summary_keeps_behavior_and_return_contract(self):
        raw = {"summary": {"behavior": "Constructs a dotted name from an AST Name or Attribute node.",
                           "parameters": ["node"], "return_contract": "str", "syntax_valid": True,
                           "issues": [], "confidence": 1.0, "assessment": "none"}}
        result = analysis_engine.normalize_function_analysis_payload(json.dumps(raw))
        self.assertEqual(result.summary, raw["summary"]["behavior"])
        self.assertEqual(result.returns.possible_types[0].type, "str")
        self.assertEqual(result.parameters[0].name, "node")
        self.assertEqual(result.review_status, "complete")
        self.assertEqual(result.confidence, 1.0)

    def test_empty_return_contract_is_rejected(self):
        for returns in ({}, "", [], False, 42, {"properties": {"type": "str"}}):
            raw = valid_result().model_dump(); raw["returns"] = returns
            with self.subTest(returns=returns), self.assertRaises(analysis_engine.FunctionAnalysisResponseError):
                analysis_engine.normalize_function_analysis_payload(json.dumps(raw))

    def test_observed_returns_type_alias_preserves_dictionary_return(self):
        raw = valid_result().model_dump()
        raw["returns"] = {"returns_type": "dict[str,str]"}
        result = analysis_engine.normalize_function_analysis_payload(json.dumps(raw))
        self.assertTrue(result.returns.may_return_value)
        self.assertEqual(result.returns.possible_types[0].type, "dict[str,str]")

    def test_invalid_issue_is_isolated_and_logged(self):
        raw = valid_result(issue_line=2).model_dump()
        raw["issues"].append({"title": "broken", "start_line": 5, "end_line": 1})
        result = analysis_engine.normalize_function_analysis_payload(json.dumps(raw))
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.review_status, "partial")
        self.assertIn("Discarded issue 2", result.validation_notes[-1])

    def test_quality_metadata_cannot_be_forged_by_model_or_requested_in_schema(self):
        raw = valid_result().model_dump()
        raw.update(analysis_method="deterministic", review_status="failed", validation_notes=["forged"], response_sha256="forged")
        result = analysis_engine.normalize_function_analysis_payload(json.dumps(raw))
        self.assertEqual(result.analysis_method, "model")
        self.assertEqual(result.review_status, "complete")
        self.assertEqual(result.validation_notes, [])
        self.assertEqual(len(result.response_sha256), 64)
        for schema in (analysis_engine.function_analysis_schema(), analysis_engine.function_analysis_batch_schema()):
            self.assertNotIn('"analysis_method"', json.dumps(schema))
        batch = analysis_engine.normalize_function_analysis_batch_payload(json.dumps({"results": [{"request_id": "one", "analysis": raw}]}), expected_request_ids=["one"])
        self.assertEqual(batch["one"].analysis_method, "model")

    def test_confidence_is_preserved_not_raised_to_target(self):
        for score in (.61, .97):
            raw = valid_result().model_dump(); raw["confidence"] = score
            result = analysis_engine.normalize_function_analysis_payload(json.dumps(raw))
            self.assertEqual(result.confidence, score)
            self.assertFalse(response_quality(result.model_dump_json(), "completed")["confidence_calibrated"])

    def test_chunk_merge_preserves_incomplete_review(self):
        partial = valid_result().model_copy(update={"review_status": "partial", "validation_notes": ["Missing guard proof"]})
        result = merge_function_chunk_analyses([valid_result(), partial])
        self.assertEqual(result.review_status, "partial")
        self.assertIn("Missing guard proof", result.validation_notes)


class PersistedQualityTests(DatabaseTestCase):
    create_indexed_project = function_tests.FunctionAnalysisPersistenceTests.create_indexed_project

    def test_cpp_fallback_does_not_manufacture_a_python_syntax_error(self):
        self.create_indexed_project(b"int answer(int value) { return value + 1; }\n", path="demo/main.cpp")
        request = Mock(side_effect=analysis_engine.FunctionAnalysisResponseError("missing summary", "{}"))
        result = analyze_project_functions(main.connect_db, "analysis-project", analysis_request=request)
        self.assertEqual(result.failed_count, 1)
        request.assert_called_once()
        with main.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_symbol_issues").fetchone()[0], 0)
            row = db.execute("SELECT syntax_valid,response_json FROM project_symbol_analyses").fetchone()
            self.assertEqual(row["syntax_valid"], 1)
            self.assertTrue(any("language parser" in note for note in json.loads(row["response_json"])["validation_notes"]))

    def test_parser_contradiction_invalidates_confident_model_summary(self):
        self.create_indexed_project(b"def target(value):\n    return isinstance(value, str | int)\n")
        model = valid_result().model_copy(update={
            "summary": "This code does not compile because union types are invalid.",
            "syntax_valid": False, "confidence": .99,
        })
        result = analyze_project_functions(main.connect_db, "analysis-project", analysis_request=lambda **kwargs: model)
        self.assertEqual(result.failed_count, 1)
        with main.connect_db() as db:
            row = db.execute("SELECT * FROM project_symbol_analyses").fetchone()
            self.assertEqual(row["syntax_valid"], 1)
            self.assertNotIn("does not compile", row["summary"])
            self.assertLessEqual(row["confidence"], .5)
            quality = response_quality(row["response_json"], "failed")
            self.assertEqual(quality["status"], "partial")
            self.assertTrue(any("contradicted" in note for note in quality["notes"]))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM function_analysis_cache").fetchone()[0], 0)

    def test_invalid_model_payload_keeps_static_findings_and_marks_review_failed(self):
        self.create_indexed_project(b"def broken(value):\n    return missing_name + value\n")
        request = Mock(side_effect=analysis_engine.FunctionAnalysisResponseError("missing summary", "{}"))
        result = analyze_project_functions(main.connect_db, "analysis-project", analysis_request=request)
        self.assertEqual(result.failed_count, 1)
        request.assert_called_once()
        with main.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM function_analysis_cache").fetchone()[0], 0)
            self.assertGreater(db.execute("SELECT COUNT(*) FROM project_symbol_issues").fetchone()[0], 0)
            quality = project_review_quality(db, "analysis-project")
            self.assertEqual(quality["complete_count"], 0)
            self.assertEqual(quality["methods"], {"fallback": 1})
            manifest = BenchmarkManifest("quality", (), (CleanRegion("clean", "main.py", "def broken"),))
            score = project_benchmark_details(db, "analysis-project", manifest, analyzer_version="test")
            self.assertEqual(score["incomplete_clean_region_ids"], ["clean"])

    def test_small_typed_branch_is_reviewed_locally_with_specific_summary(self):
        self.create_indexed_project(b"def label(enabled: bool) -> str:\n    if enabled:\n        return 'ready'\n    return 'off'\n")
        request = Mock(side_effect=AssertionError("Unexpected model request"))
        result = analyze_project_functions(main.connect_db, "analysis-project", analysis_request=request)
        self.assertEqual(result.deterministic_count, 1)
        request.assert_not_called()
        with main.connect_db() as db:
            quality = project_review_quality(db, "analysis-project")
            self.assertEqual(quality["complete_count"], 1)
            self.assertEqual(quality["model_confidence_at_least_95_count"], 0)
            self.assertIsNone(quality["calibrated_confidence"])

    def test_partial_issue_review_is_persisted_but_not_cached(self):
        self.create_indexed_project(b"def answer(value):\n    return value\n")
        raw = valid_result(issue_line=2).model_dump(); raw["issues"][0]["guard_check"] = []
        result = analyze_project_functions(main.connect_db, "analysis-project", analysis_request=lambda **kwargs: analysis_engine.normalize_function_analysis_payload(json.dumps(raw)))
        self.assertEqual(result.failed_count, 1)
        with main.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_symbol_analyses").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM function_analysis_cache").fetchone()[0], 0)
            user_id = db.execute("SELECT user_id FROM projects").fetchone()[0]
        report = main.get_project_analysis_report("analysis-project", self.authenticated_request(user_id))
        self.assertEqual(report["review_quality"]["incomplete_count"], 1)
        self.assertEqual(report["functions"][0]["review_quality"]["status"], "partial")

    def test_legacy_success_is_not_reported_as_verified_confidence(self):
        raw = valid_result().model_dump()
        for key in ("analysis_method", "review_status", "validation_notes", "response_sha256"):
            raw.pop(key)
        quality = response_quality(json.dumps(raw), "completed")
        self.assertEqual(quality["status"], "unknown")
        self.assertIsNone(quality["model_confidence"])
