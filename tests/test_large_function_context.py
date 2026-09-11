import ast
import json
import unittest
from unittest.mock import Mock

import main
import project_function_analysis as engine
from dependency_context import python_control_flow_items, resolved_context_items
from tests import test_function_analysis as function_tests
from tests.helpers import DatabaseTestCase


class ControlFlowTests(unittest.TestCase):
    def test_cleanup_is_under_exception_path_and_nested_functions_are_excluded(self):
        source = """def deliver(value):
    try:
        send(value)
    except OSError:
        cleanup(value)
    def nested():
        secret()
    return value
"""
        items = python_control_flow_items(source, 100)
        events = [json.loads(item.split("# Source control-flow fact ", 1)[1]) for item in items[1:]]
        cleanup = next(event for event in events if event["source"] == "cleanup(value)")
        self.assertEqual(cleanup["enclosing_blocks"], ["except OSError"])
        self.assertEqual(cleanup["relative_line"], 5)
        self.assertNotIn("secret", "\n".join(items))
        ast.parse("\n".join(items))


class LargeFunctionTests(DatabaseTestCase):
    create_indexed_project = function_tests.FunctionAnalysisPersistenceTests.create_indexed_project

    def test_function_at_20000_is_whole_and_20001_is_chunked(self):
        for size in (20000, 20001):
            with self.subTest(size=size):
                prefix, suffix = "def target(value):\n    #", "\n    return remote(value)"
                source = prefix + "x" * (size - len(prefix) - len(suffix)) + suffix
                pid = "boundary-" + str(size)
                self.create_indexed_project(source.encode(), pid)
                whole = Mock(return_value=function_tests.valid_result())
                chunk = Mock(return_value=function_tests.valid_result())
                result = engine.analyze_project_functions(main.connect_db, pid, analysis_request=whole, chunk_analysis_request=chunk)
                self.assertEqual(result.status, "completed")
                if size == 20000:
                    whole.assert_called_once(); chunk.assert_not_called()
                    self.assertEqual(len(whole.call_args.kwargs["source"]), 20000)
                else:
                    whole.assert_not_called(); self.assertGreater(chunk.call_count, 1)
                    self.assertEqual("".join(call.kwargs["source"] for call in chunk.call_args_list), source)
                    self.assertTrue(all(len(call.kwargs["source"]) <= 20000 for call in chunk.call_args_list))

    def test_long_callee_contract_survives_compaction_and_reaches_caller(self):
        self.create_indexed_project(b"def caller(value):\n    return helper(value)\ndef helper(value):\n    return remote(value)\n")
        calls = []
        def review(**kwargs):
            calls.append(kwargs)
            result = function_tests.valid_result()
            if kwargs["qualified_name"] == "helper":
                result.parameters[0].description = "Details " * 100
                result.side_effects = ["Side effect detail. " * 45]
            return result
        engine.analyze_project_functions(main.connect_db, "analysis-project", analysis_request=review)
        self.assertEqual([call["qualified_name"] for call in calls], ["helper", "caller"])
        self.assertIn("Inferred callee contract", calls[-1]["analysis_context"])
        self.assertIn('"descriptions_omitted":true', calls[-1]["analysis_context"])
        self.assertLessEqual(len(calls[-1]["analysis_context"]), 20000)

    def test_legacy_and_partial_callee_claims_are_not_forwarded_as_completed_reviews(self):
        self.create_indexed_project(b"function caller(value) { return helper(value); }\nfunction helper(value) { return value; }", path="main.js")
        engine.analyze_project_functions(main.connect_db, "analysis-project", analysis_request=lambda **kwargs: function_tests.valid_result())
        with main.connect_db() as db:
            caller = db.execute("SELECT id FROM project_symbols WHERE name='caller'").fetchone()[0]
            helper = db.execute("SELECT id FROM project_symbols WHERE name='helper'").fetchone()[0]
            raw = function_tests.valid_result().model_dump()
            for status in (None, "partial"):
                raw.pop("review_status", None)
                if status: raw["review_status"] = status
                db.execute("UPDATE project_symbol_analyses SET response_json=? WHERE symbol_id=?", (json.dumps(raw),helper))
                context = "\n".join(resolved_context_items(db,"analysis-project",caller,include_inferred=True))
                self.assertIn("Resolved source", context)
                self.assertNotIn("Inferred callee contract", context)
