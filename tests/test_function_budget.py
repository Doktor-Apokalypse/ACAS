import json
import unittest
from unittest.mock import patch

import analysis_engine as llm
import function_budget as budget
import main
import project_function_analysis as engine
from tests.helpers import DatabaseTestCase
from tests import test_function_analysis as function_tests
from tests.test_analysis import _StreamingResponse


class BudgetFormulaTests(unittest.TestCase):
    def test_python_counts_structure_but_not_comments_strings_or_nested_functions(self):
        source = '''def review(value):
    """if while return try"""
    def nested(x):
        while x:
            return x
    if value:
        for item in value:
            try:
                consume(item)
            except ValueError:
                return None
    return len(value)
'''
        f = budget.structural_features("python", source)
        self.assertEqual((f["branches"],f["loops"],f["handlers"],f["returns"]), (1,1,1,2))
        self.assertEqual(f["parameters"], ["value"])
        self.assertIn("len", f["known_builtins"])
        self.assertNotIn("len", budget.structural_features("python", "def f(len): return len(1)")["known_builtins"])
        scored = budget.estimate_output(f,dict(resolved=2,unresolved=1,cross_file=1,recursive=True))
        self.assertEqual(scored["complexity_score"], 27)
        self.assertEqual(scored["dependency_score"], 32)
        self.assertEqual(scored["dependency_count"], 3)

    def test_other_codebases_use_language_parsers_and_unknown_languages_report_uncertainty(self):
        for language, source in (
            ("javascript", "function f(x) { if (x) { while (x > 1) { x--; } } return x; }"),
            ("cpp", "int f(int x) { if (x) { while (x > 1) { x--; } } return x; }"),
            ("rust", "fn f(mut x: i32) -> i32 { if x > 0 { while x > 1 { x -= 1; } } return x; }"),
        ):
            with self.subTest(language=language):
                f=budget.structural_features(language,source)
                self.assertEqual((f["branches"],f["loops"],f["returns"]),(1,1,1))
                self.assertFalse(f["uncertainty"])
        unknown=budget.estimate_output(budget.structural_features("unknown", "if loop whatever"), {})
        self.assertGreaterEqual(unknown["complexity_score"],50)
        self.assertTrue(unknown["uncertainty"])

    def test_tiers_and_truncation_history_override(self):
        f=budget.structural_features("python","def f(x): return x")
        small=budget.estimate_output(f,{})
        self.assertEqual(small["output_tokens"],4096)
        medium=budget.estimate_output({**f,"branches":10,"loops":2,"nesting":4},dict(unresolved=3))
        self.assertEqual(medium["output_tokens"],8192)
        large=budget.estimate_output({**f,"branches":100,"parameters":[str(i) for i in range(80)]},dict(unresolved=50))
        self.assertEqual(large["output_tokens"],16384)
        self.assertTrue(large["ceiling_exceeded"])
        retry=budget.estimate_output(f,{},[dict(exact=True,truncated=True,output_limit=8192)])
        self.assertEqual(retry["output_tokens"],16384)
        self.assertIn("previous truncation",retry["reasons"])

    def test_cross_project_calibration_needs_eight_comparable_samples(self):
        f=budget.structural_features("python","def f(x): return x")
        h=dict(exact=False,truncated=False,output_limit=16384,generated_tokens=11000,outcome="valid_response")
        self.assertEqual(budget.estimate_output(f,{},[h]*7)["output_tokens"],4096)
        self.assertEqual(budget.estimate_output(f,{},[h]*8)["output_tokens"],16384)
        self.assertEqual(budget.estimate_output(f,{},[{**h,"exact":True}])["output_tokens"],16384)


class BudgetPersistenceTests(DatabaseTestCase):
    create_indexed_project = function_tests.FunctionAnalysisPersistenceTests.create_indexed_project

    def test_adaptive_batch_splits_before_exceeding_shared_output_ceiling(self):
        self.create_indexed_project(('\n'.join(f'def f{i}(value): return remote(value)' for i in range(8))).encode())
        batches=[]
        def grouped(**kwargs):
            functions=kwargs['functions'];batches.append((len(functions),budget.selected_output_limit(0)))
            return {f['request_id']:function_tests.valid_result() for f in functions}
        result=engine.analyze_project_functions(main.connect_db,'analysis-project',batch_analysis_request=grouped)
        self.assertEqual(result.status,'completed')
        self.assertEqual([count for count,_ in batches],[4,4])
        self.assertTrue(all(limit<=16384 for _,limit in batches))
        self.assertEqual(result.model_request_count,2)

    def test_module_binding_prevents_builtin_exemption(self):
        self.create_indexed_project(b"len = remote\ndef caller(value): return len(value)\n")
        with main.connect_db() as db:
            sid=db.execute("SELECT id FROM project_symbols WHERE name='caller'").fetchone()[0]
            b=budget.prepare_budget(db,engine.load_function_analysis_task(db,sid))
        self.assertEqual(b['dependencies']['unresolved'],1)

    def test_fixed_mode_and_scope_cleanup(self):
        import app_config
        self.create_indexed_project(b"def caller(value): return remote(value)\n")
        limits=[]
        def call(**kwargs):
            limits.append(budget.selected_output_limit(0))
            return function_tests.valid_result()
        with patch.object(app_config,'FUNCTION_ANALYSIS_ADAPTIVE_OUTPUT',False):
            engine.analyze_project_functions(main.connect_db,'analysis-project',analysis_request=call)
        self.assertEqual(limits,[app_config.FUNCTION_ANALYSIS_MAX_OUTPUT_TOKENS])
        self.assertEqual(budget.selected_output_limit(123),123)

    def test_usage_is_recorded_and_truncation_increases_next_budget(self):
        from semantic_review import build_engine_facts
        from tests.test_semantic_review import response_for
        self.create_indexed_project(b"def caller(value):\n    return remote(value)\n")
        with main.connect_db() as db:
            sid=db.execute("SELECT id FROM project_symbols WHERE name='caller'").fetchone()[0]
            task=engine.load_function_analysis_task(db,sid)
            planned=budget.prepare_budget(db,task,semantic_review=True)
            packet=build_engine_facts(task,engine.deterministic_python_analysis(task))
        requests=[]
        def capture(request,**kwargs):
            request=json.loads(request.data);requests.append(request)
            event={"message":{"content":response_for(packet),"thinking":"reasoning"},
                   "done":True,"done_reason":"length" if len(requests)==1 else "stop",
                   "eval_count":request["options"]["num_predict"] if len(requests)==1 else 7000,"prompt_eval_count":500}
            return _StreamingResponse([(json.dumps(event)+'\n').encode()])
        with patch.object(llm.urllib.request,"urlopen",side_effect=capture):
            engine.analyze_project_functions(main.connect_db,"analysis-project")
        self.assertEqual(len(requests),2)
        self.assertEqual(requests[0]["options"]["num_predict"],planned["output_tokens"])
        self.assertGreaterEqual(requests[1]["options"]["num_predict"],requests[0]["options"]["num_predict"])
        self.assertLessEqual(requests[1]["options"]["num_predict"], budget.SEMANTIC_TIERS[-1])
        with main.connect_db() as db:
            observations=[dict(r) for r in db.execute("SELECT * FROM function_analysis_usage ORDER BY id")]
            future=budget.prepare_budget(db,task,semantic_review=True)
        self.assertEqual(len(observations),2)
        self.assertEqual(observations[0]["truncated"],1)
        self.assertEqual(observations[-1]["outcome"],"valid_response")
        self.assertEqual(observations[-1]["reasoning_characters"],9)
        self.assertGreaterEqual(future["output_tokens"],planned["output_tokens"])
        self.assertEqual(budget.selected_output_limit(123),123)

    def test_duplicate_dependencies_and_builtins_are_not_overcounted(self):
        self.create_indexed_project(b"def helper(x): return x\ndef caller(x):\n    helper(x)\n    helper(x)\n    return len(x)\n")
        with main.connect_db() as db:
            sid=db.execute("SELECT id FROM project_symbols WHERE name='caller'").fetchone()[0]
            task=engine.load_function_analysis_task(db,sid)
            b=budget.prepare_budget(db,task)
        self.assertEqual(b["dependencies"]["resolved"],1)
        self.assertEqual(b["dependencies"]["unresolved"],0)

    def test_plan_visible_in_tree_and_report_and_reset_preserves_usage(self):
        self.create_indexed_project(b"def caller(value): return remote(value)\n")
        def call(**kwargs):
            limit=budget.selected_output_limit(0)
            budget.observe_usage(dict(event="start",output_limit=limit))
            budget.observe_usage(dict(event="end",output_limit=limit,generated_tokens=300,prompt_tokens=100,truncated=False))
            return function_tests.valid_result()
        engine.analyze_project_functions(main.connect_db,"analysis-project",analysis_request=call)
        with main.connect_db() as db:
            user=db.execute("SELECT user_id FROM projects WHERE id='analysis-project'").fetchone()[0]
        request=self.authenticated_request(user)
        tree=main.get_project_tree("analysis-project",request)
        self.assertIn(tree["files"][0]["analysis_budget"]["output_tokens"],budget.TIERS)
        report=main.get_project_analysis_report("analysis-project",request)
        self.assertIn(report["functions"][0]["output_budget"]["output_tokens"],budget.TIERS)
        main.clear_project_analysis_cache("analysis-project",request)
        with main.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM function_analysis_usage").fetchone()[0],1)
            db.execute("DELETE FROM projects WHERE id='analysis-project'")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM function_analysis_usage").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM function_analysis_budgets").fetchone()[0],0)

    def test_history_isolated_by_user_language_model_and_batches(self):
        self.create_indexed_project(b"def caller(value): return remote(value)\n")
        other=self.create_user('other-budget-user')
        with main.connect_db() as db:
            sid=db.execute("SELECT id FROM project_symbols WHERE name='caller'").fetchone()[0]
            task=engine.load_function_analysis_task(db,sid)
            initial=budget.prepare_budget(db,task)
        with budget.budgeted_request(main.connect_db,[task],[initial],batch=True) as validated:
            budget.observe_usage(dict(event="end",output_limit=16384,generated_tokens=16000,truncated=True))
            validated(function_tests.valid_result())
        with main.connect_db() as db:
            self.assertEqual(budget.prepare_budget(db,task)["history_samples"],0)
            db.execute("UPDATE function_analysis_usage SET request_kind='single',model='other'")
            self.assertEqual(budget.prepare_budget(db,task)["history_samples"],0)
            db.execute("UPDATE function_analysis_usage SET model=?,language='rust'",(initial["model"],))
            self.assertEqual(budget.prepare_budget(db,task)["history_samples"],0)
            db.execute("UPDATE function_analysis_usage SET language='python',user_id=?",(other,))
            self.assertEqual(budget.prepare_budget(db,task)["history_samples"],0)
