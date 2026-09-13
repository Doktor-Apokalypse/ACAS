from __future__ import annotations

import hashlib
import json
import threading
from unittest.mock import patch

import analysis_engine
import main
import project_function_analysis
from analysis_engine import (
    FunctionAnalysisResult,
    FunctionIssue,
    FunctionParameterContract,
    FunctionReturnContract,
    FunctionReturnType,
)
from migrations import migration_025_oversized_function_chunking
from project_function_analysis import (
    analyze_project_functions,
    deterministic_python_contract,
    filter_source_proven_false_issues,
    load_function_analysis_task,
    merge_function_chunk_analyses,
    split_function_source,
    store_cached_function_analysis,
)
from project_inventory import inventory_project_database
from project_parsing import parse_project_database
from tests.helpers import DatabaseTestCase


def valid_result(*, issue_line: int | None = None) -> FunctionAnalysisResult:
    issues = []
    if issue_line is not None:
        issues.append(
            FunctionIssue(
                severity="warning",
                category="logic",
                title="Check the boundary",
                description="The boundary behavior should be verified by a caller.",
                start_line=issue_line,
                end_line=issue_line,
                proof="source-v1",
                evidence="value",
                failure_type="Incorrect boundary behavior",
                trigger="The boundary value reaches this expression.",
                reachability="Boundary input reaches the return expression.",
                guard_check="No guard prevents evaluation of the return expression.",
            )
        )
    return FunctionAnalysisResult(
        contract_version="1.0",
        summary="Returns the supplied numeric value.",
        syntax_valid=True,
        parameters=[
            FunctionParameterContract(
                name="value",
                kind="positional_or_keyword",
                required=True,
                accepted_types=["int"],
                description="Numeric value to return.",
            )
        ],
        returns=FunctionReturnContract(
            may_return_value=True,
            possible_types=[
                FunctionReturnType(type="int", description="The supplied integer.")
            ],
            nullable=False,
            description="Returns the input unchanged.",
        ),
        raised_errors=[],
        side_effects=[],
        issues=issues,
        confidence=0.92,
    )


def proven_issue(
    *,
    evidence: str,
    failure_type: str = "Incorrect behavior",
    trigger: str = "Execution reaches the evidenced source expression.",
    **values,
) -> FunctionIssue:
    return FunctionIssue(
        **values,
        proof="source-v1",
        evidence=evidence,
        failure_type=failure_type,
        trigger=trigger,
        reachability="Execution enters the function and reaches the evidenced expression.",
        guard_check="No visible guard prevents the evidenced expression.",
    )


class FunctionAnalysisContractTests(DatabaseTestCase):
    def test_request_uses_strict_schema_numbered_source_and_low_temperature(self) -> None:
        calls: list[tuple[list[dict[str, str]], dict[str, object]]] = []

        def fake_ask(messages, **kwargs):
            calls.append((messages, kwargs))
            return valid_result(issue_line=11).model_dump_json()

        with patch.object(analysis_engine, "ask_ollama", fake_ask):
            result = analysis_engine.request_function_analysis(
                language="python",
                file_path="demo/main.py",
                symbol_kind="function",
                qualified_name="answer",
                start_line=10,
                end_line=11,
                source="def answer(value):\n    return value",
                analysis_context="def helper(value: int) -> int: ...",
            )

        self.assertEqual(result.parameters[0].accepted_types, ["int"])
        self.assertEqual(len(calls), 1)
        messages, options = calls[0]
        self.assertIn("    10: def answer(value):", messages[0]["content"])
        self.assertIn("    11:     return value", messages[0]["content"])
        self.assertIn("BEGIN PROJECT CONTEXT", messages[0]["content"])
        self.assertIn("def helper(value: int) -> int: ...", messages[0]["content"])
        self.assertEqual(options["temperature"], 0)
        self.assertEqual(
            options["response_format"]["properties"]["contract_version"]["const"],
            "1.0",
        )
        issue_schema = options["response_format"]["$defs"]["FunctionIssue"]
        self.assertTrue(
            {"start_line", "proof", "evidence", "failure_type", "trigger"}
            <= set(issue_schema["required"])
        )
        self.assertEqual(issue_schema["properties"]["proof"]["const"], "source-v1")

    def test_batch_request_analyses_multiple_functions_in_one_model_call(self) -> None:
        calls: list[tuple[list[dict[str, str]], dict[str, object]]] = []
        batch_payload = analysis_engine.FunctionAnalysisBatchResult(
            results=[
                analysis_engine.FunctionAnalysisBatchItem(
                    request_id="symbol-1",
                    analysis=valid_result(issue_line=10),
                ),
                analysis_engine.FunctionAnalysisBatchItem(
                    request_id="symbol-2",
                    analysis=valid_result(issue_line=20),
                ),
            ]
        ).model_dump_json()

        def fake_ask(messages, **kwargs):
            calls.append((messages, kwargs))
            return batch_payload

        functions = [
            {
                "request_id": "symbol-1",
                "language": "python",
                "file_path": "demo/one.py",
                "symbol_kind": "function",
                "qualified_name": "one",
                "start_line": 10,
                "end_line": 11,
                "source": "def one(value):\n    return value",
                "analysis_context": "def helper(value: int) -> int: ...",
            },
            {
                "request_id": "symbol-2",
                "language": "python",
                "file_path": "demo/two.py",
                "symbol_kind": "function",
                "qualified_name": "two",
                "start_line": 20,
                "end_line": 21,
                "source": "def two(value):\n    return value",
                "analysis_context": "",
            },
        ]
        with patch.object(analysis_engine, "ask_ollama", fake_ask):
            results = analysis_engine.request_function_analysis_batch(functions=functions)

        self.assertEqual(list(results), ["symbol-1", "symbol-2"])
        self.assertEqual(results["symbol-1"].issues[0].start_line, 10)
        self.assertEqual(results["symbol-2"].issues[0].start_line, 20)
        self.assertEqual(len(calls), 1)
        prompt = calls[0][0][0]["content"]
        self.assertIn("BEGIN FUNCTION symbol-1", prompt)
        self.assertIn("BEGIN FUNCTION symbol-2", prompt)
        self.assertIn("def helper(value: int) -> int: ...", prompt)
        self.assertIn("results", calls[0][1]["response_format"]["properties"])

    def test_batch_payload_recovers_flattened_ordered_and_keyed_results(self) -> None:
        payload_one = valid_result().model_dump(mode="json")
        payload_two = valid_result().model_dump(mode="json")
        expected = ["symbol-1", "symbol-2"]
        payloads = (
            {"results": [
                {"request_id": "symbol-1", **payload_one},
                {"request_id": "symbol-2", **payload_two},
            ]},
            {"results": [payload_one, payload_two]},
            {"symbol-1": payload_one, "symbol-2": payload_two},
        )

        for payload in payloads:
            with self.subTest(shape=tuple(payload)):
                results = analysis_engine.normalize_function_analysis_batch_payload(
                    json.dumps(payload),
                    expected_request_ids=expected,
                )
                self.assertEqual(list(results), expected)

    def test_batch_payload_rejects_mixed_positional_mapping(self) -> None:
        payload = valid_result().model_dump(mode="json")
        with self.assertRaises(analysis_engine.FunctionAnalysisBatchFormatError):
            analysis_engine.normalize_function_analysis_batch_payload(
                json.dumps({
                    "results": [
                        {"request_id": "symbol-1", "analysis": payload},
                        payload,
                    ]
                }),
                expected_request_ids=["symbol-1", "symbol-2"],
            )

    def test_deterministic_python_contract_overrides_model_shape(self) -> None:
        task = project_function_analysis.FunctionAnalysisTask(
            symbol_id=1,
            project_id="p",
            user_id=1,
            file_id=1,
            file_path="demo.py",
            language="python",
            symbol_kind="function",
            qualified_name="function_analysis_schema",
            start_line=1,
            end_line=2,
            source_sha256="0" * 64,
            function_sha256="1" * 64,
            source="def function_analysis_schema() -> dict[str, object]:\n    return {}\n",
        )
        model_result = valid_result()
        model_result = model_result.model_copy(
            update={
                "parameters": [],
                "returns": FunctionReturnContract(
                    may_return_value=False,
                    possible_types=[],
                    nullable=False,
                    description="Wrong model result.",
                ),
            }
        )

        result = deterministic_python_contract(task, model_result)

        self.assertTrue(result.returns.may_return_value)
        self.assertEqual(result.returns.possible_types[0].type, "dict[str, object]")

    def test_deterministic_python_contract_treats_yield_as_value_producing(self) -> None:
        task = project_function_analysis.FunctionAnalysisTask(
            symbol_id=1,
            project_id="p",
            user_id=1,
            file_id=1,
            file_path="demo.py",
            language="python",
            symbol_kind="function",
            qualified_name="connect_db",
            start_line=1,
            end_line=4,
            source_sha256="0" * 64,
            function_sha256="1" * 64,
            source=(
                "@contextmanager\n"
                "def connect_db():\n"
                "    with connect_database() as connection:\n"
                "        yield connection\n"
            ),
        )

        model_result = valid_result().model_copy(
            update={
                "returns": FunctionReturnContract(
                    may_return_value=False,
                    possible_types=[],
                    nullable=False,
                    description="No value.",
                )
            }
        )

        result = deterministic_python_contract(task, model_result)

        self.assertTrue(result.returns.may_return_value)
        self.assertEqual(result.returns.possible_types[0].type, "contextmanager")

    def test_deterministic_python_contract_flags_fixture_style_source_issues(self) -> None:
        source = (
            "async def fixture_unawaited_async_database_update(chat_id: str) -> None:\n"
            "    seen = []\n"
            "    fixture_update_title_async(chat_id, 'Fixture title')\n"
            "    return None\n"
        )
        task = project_function_analysis.FunctionAnalysisTask(
            symbol_id=1,
            project_id="p",
            user_id=1,
            file_id=1,
            file_path="main_analysis_error_fixture.py",
            language="python",
            symbol_kind="function",
            qualified_name="fixture_unawaited_async_database_update",
            start_line=6147,
            end_line=6150,
            source_sha256="0" * 64,
            function_sha256="1" * 64,
            source=source,
        )

        result = deterministic_python_contract(task, valid_result())

        self.assertIn("Async call is not awaited", {issue.title for issue in result.issues})

    def test_deterministic_call_contract_uses_arbitrary_indexed_signature(self) -> None:
        source = (
            "def caller(event_name: str) -> None:\n"
            "    archive_event(event_name)\n"
        )
        task = project_function_analysis.FunctionAnalysisTask(
            symbol_id=1,
            project_id="p",
            user_id=1,
            file_id=1,
            file_path="events.py",
            language="python",
            symbol_kind="function",
            qualified_name="caller",
            start_line=1,
            end_line=2,
            source_sha256="0" * 64,
            function_sha256="1" * 64,
            source=source,
            analysis_context=(
                "def archive_event(event_name: str, payload: dict[str, object]) -> None: ..."
            ),
        )

        result = deterministic_python_contract(task, valid_result())

        issue = next(
            item for item in result.issues if item.title == "Missing required call arguments"
        )
        self.assertEqual(issue.start_line, 2)
        self.assertIn("payload", issue.description)

    def test_deterministic_python_contract_flags_name_call_resource_security_and_type_issues(self) -> None:
        cases = {
            "fixture_undefined_variable_path": (
                "def fixture_undefined_variable_path(user_id: int) -> str:\n"
                "    audit_key = f'user-{account_id}'\n"
                "    return f'{audit_key}:{user_id}'\n",
                {"Possibly undefined variable"},
            ),
            "fixture_wrong_function_call_signature": (
                "def fixture_wrong_function_call_signature(request) -> dict[str, object]:\n"
                "    saved_id = save_message(request, 'fixture-chat')\n"
                "    return {'message_id': saved_id}\n",
                {"Missing required call arguments"},
            ),
            "fixture_bad_attribute_access": (
                "def fixture_bad_attribute_access(user: sqlite3.Row | None) -> str:\n"
                "    return user.email.casefold()\n",
                {"Possible None dereference", "Invalid sqlite3.Row attribute access"},
            ),
            "fixture_sql_injection_candidate": (
                "def fixture_sql_injection_candidate(username: str):\n"
                "    with connect_db() as db:\n"
                "        return db.execute(f\"SELECT * FROM users WHERE username = '{username}'\").fetchone()\n",
                {"SQL built with f-string"},
            ),
            "fixture_leaked_file_handle": (
                "def fixture_leaked_file_handle(path: str) -> str:\n"
                "    handle = open(path, 'r', encoding='utf-8')\n"
                "    return handle.read()\n",
                {"File opened without context manager"},
            ),
            "fixture_mutable_default_accumulates": (
                "def fixture_mutable_default_accumulates(value: str, seen: list[str] = []) -> list[str]:\n"
                "    seen.append(value)\n"
                "    return seen\n",
                {"Mutable default argument"},
            ),
            "fixture_return_type_mismatch": (
                "def fixture_return_type_mismatch(enabled: bool) -> int:\n"
                "    if enabled:\n"
                "        return 1\n"
                "    return 'disabled'\n",
                {"Return type does not match annotation"},
            ),
            "fixture_generic_return_type_mismatch": (
                "def fixture_generic_return_type_mismatch() -> dict[str, object]:\n"
                "    return ['complete', 'model']\n",
                {"Return type does not match annotation"},
            ),
            "fixture_builtin_return_type_mismatch": (
                "def fixture_builtin_return_type_mismatch(compact: str) -> str:\n"
                "    return len(compact)\n",
                {"Return type does not match annotation"},
            ),
            "fixture_broad_exception_swallowing": (
                "def fixture_broad_exception_swallowing(payload: dict[str, object]) -> int:\n"
                "    try:\n"
                "        return int(payload['count'])\n"
                "    except Exception:\n"
                "        return 0\n",
                {"Broad exception handler"},
            ),
            "fixture_missing_required_call_arguments": (
                "def fixture_missing_required_call_arguments() -> dict[str, str]:\n"
                "    actor = require_user()\n"
                "    return {'username': actor['username']}\n",
                {"Missing required call arguments"},
            ),
        }
        for qualified_name, (source, expected_titles) in cases.items():
            with self.subTest(qualified_name=qualified_name):
                task = project_function_analysis.FunctionAnalysisTask(
                    symbol_id=1,
                    project_id="p",
                    user_id=1,
                    file_id=1,
                    file_path="main_analysis_error_fixture.py",
                    language="python",
                    symbol_kind="function",
                    qualified_name=qualified_name,
                    start_line=1,
                    end_line=source.count("\n"),
                    source_sha256="0" * 64,
                    function_sha256="1" * 64,
                    source=source,
                    analysis_context=(
                        "def save_message(session_id, role, content): ...\n"
                        "def require_user(request): ..."
                    ),
                )

                result = deterministic_python_contract(task, valid_result())

                self.assertTrue(expected_titles <= {issue.title for issue in result.issues})
                for issue in result.issues:
                    if issue.title in expected_titles:
                        self.assertEqual(issue.provenance, "deterministic")

    def test_deterministic_return_check_respects_generic_compatibility_and_shadowing(self) -> None:
        cases = (
            "def compatible() -> list[str]:\n    return []\n",
            "def shadowed(len) -> str:\n    return len('value')\n",
        )
        for source in cases:
            with self.subTest(source=source):
                task = project_function_analysis.FunctionAnalysisTask(
                    symbol_id=1,
                    project_id="p",
                    user_id=1,
                    file_id=1,
                    file_path="returns.py",
                    language="python",
                    symbol_kind="function",
                    qualified_name="example",
                    start_line=1,
                    end_line=source.count("\n"),
                    source_sha256="0" * 64,
                    function_sha256="1" * 64,
                    source=source,
                )

                result = deterministic_python_contract(task, valid_result())

                self.assertNotIn(
                    "Return type does not match annotation",
                    {issue.title for issue in result.issues},
                )

    def test_deterministic_python_contract_respects_none_guards_and_safe_sql_fragments(self) -> None:
        source = (
            "def guarded(value: str | None, db, ids: list[int]) -> list[object]:\n"
            "    if value is None:\n"
            "        return []\n"
            "    bind_marks = ','.join('?' for _ in ids)\n"
            "    rows = db.execute(f'SELECT * FROM items WHERE id IN ({bind_marks})', ids).fetchall()\n"
            "    return [value.casefold(), rows]\n"
        )
        task = project_function_analysis.FunctionAnalysisTask(
            symbol_id=1,
            project_id="p",
            user_id=1,
            file_id=1,
            file_path="safe.py",
            language="python",
            symbol_kind="function",
            qualified_name="guarded",
            start_line=1,
            end_line=source.count("\n"),
            source_sha256="0" * 64,
            function_sha256="1" * 64,
            source=source,
        )

        result = deterministic_python_contract(task, valid_result())

        titles = {issue.title for issue in result.issues}
        self.assertNotIn("Possible None dereference", titles)
        self.assertNotIn("SQL built with f-string", titles)

    def test_deterministic_python_contract_flags_sql_fstring_variable_execution(self) -> None:
        source = (
            "def unsafe(username: str, db):\n"
            "    query = f\"SELECT * FROM users WHERE username = '{username}'\"\n"
            "    return db.execute(query).fetchone()\n"
        )
        task = project_function_analysis.FunctionAnalysisTask(
            symbol_id=1,
            project_id="p",
            user_id=1,
            file_id=1,
            file_path="unsafe.py",
            language="python",
            symbol_kind="function",
            qualified_name="unsafe",
            start_line=1,
            end_line=source.count("\n"),
            source_sha256="0" * 64,
            function_sha256="1" * 64,
            source=source,
        )

        result = deterministic_python_contract(task, valid_result())

        self.assertIn("SQL built with f-string", {issue.title for issue in result.issues})

    def test_sequence_index_rule_distinguishes_weak_and_sufficient_guards(self) -> None:
        source = (
            "def select_part(parts: tuple[str, ...], safe_parts: list[str]) -> str:\n"
            "    risky = parts[1] if parts else 'fallback'\n"
            "    safe = safe_parts[1] if len(safe_parts) >= 2 else 'fallback'\n"
            "    return risky + safe\n"
        )
        task = project_function_analysis.FunctionAnalysisTask(
            symbol_id=1,
            project_id="p",
            user_id=1,
            file_id=1,
            file_path="sequence.py",
            language="python",
            symbol_kind="function",
            qualified_name="select_part",
            start_line=1,
            end_line=source.count("\n"),
            source_sha256="0" * 64,
            function_sha256="1" * 64,
            source=source,
        )

        result = deterministic_python_contract(task, valid_result())

        index_issues = [
            issue for issue in result.issues if issue.failure_type == "IndexError"
        ]
        self.assertEqual(len(index_issues), 1)
        self.assertEqual(index_issues[0].start_line, 2)
        self.assertEqual(index_issues[0].severity, "unsafe")

    def test_deterministic_python_contract_flags_invalid_function_syntax(self) -> None:
        task = project_function_analysis.FunctionAnalysisTask(
            symbol_id=1,
            project_id="p",
            user_id=1,
            file_id=1,
            file_path="main_analysis_error_fixture.py",
            language="python",
            symbol_kind="function",
            qualified_name="fixture_invalid_syntax_branch",
            start_line=6198,
            end_line=6201,
            source_sha256="0" * 64,
            function_sha256="1" * 64,
            source=(
                "def fixture_invalid_syntax_branch(value: int) -> int:\n"
                "    if value > 0\n"
                "        return value\n"
                "    return 0\n"
            ),
        )

        result = deterministic_python_contract(task, valid_result())

        self.assertFalse(result.syntax_valid)
        self.assertIn("Python syntax error", {issue.title for issue in result.issues})

    def test_definite_assignment_tracks_branches_loops_calls_and_nested_scopes(self) -> None:
        cases = {
            "branch_only": (
                "def branch_only(enabled: bool) -> str:\n"
                "    if enabled:\n"
                "        label = 'ready'\n"
                "    return label\n",
                {4: "UnboundLocalError"},
            ),
            "both_branches": (
                "def both_branches(enabled: bool) -> str:\n"
                "    if enabled:\n"
                "        label = 'ready'\n"
                "    else:\n"
                "        label = 'waiting'\n"
                "    return label\n",
                {},
            ),
            "loop_only": (
                "def loop_only(values: list[str]) -> str:\n"
                "    for value in values:\n"
                "        last = value\n"
                "    return last\n",
                {4: "UnboundLocalError"},
            ),
            "rhs_before_assignment": (
                "def rhs_before_assignment() -> int:\n"
                "    count = count + 1\n"
                "    return count\n",
                {2: "UnboundLocalError"},
            ),
            "undefined_call": (
                "def undefined_call() -> object:\n"
                "    return missing_factory()\n",
                {2: "NameError"},
            ),
            "nested_scope": (
                "def nested_scope() -> int:\n"
                "    def inner():\n"
                "        return inner_only\n"
                "    return 1\n",
                {},
            ),
            "try_path": (
                "def try_path(value: str) -> str:\n"
                "    try:\n"
                "        parsed = value.strip()\n"
                "    except ValueError:\n"
                "        pass\n"
                "    return parsed\n",
                {6: "UnboundLocalError"},
            ),
            "loop_else_assignment": (
                "def loop_else_assignment() -> str:\n"
                "    for _attempt in range(2):\n"
                "        try:\n"
                "            result = 'ok'\n"
                "            break\n"
                "        except ValueError:\n"
                "            continue\n"
                "    else:\n"
                "        result = 'fallback'\n"
                "    return result\n",
                {},
            ),
            "boolean_witness": (
                "def boolean_witness() -> str | None:\n"
                "    available = False\n"
                "    try:\n"
                "        resource = 'ready'\n"
                "        available = True\n"
                "    except ValueError:\n"
                "        pass\n"
                "    if available:\n"
                "        return resource\n"
                "    return None\n",
                {},
            ),
            "predicate_witness": (
                "def predicate_witness(action: str) -> str:\n"
                "    if action in {'enable', 'disable'}:\n"
                "        message = 'changed'\n"
                "    if action == 'enable':\n"
                "        return message\n"
                "    return 'unchanged'\n",
                {},
            ),
        }
        for qualified_name, (source, expected) in cases.items():
            with self.subTest(qualified_name=qualified_name):
                task = project_function_analysis.FunctionAnalysisTask(
                    symbol_id=1,
                    project_id="p",
                    user_id=1,
                    file_id=1,
                    file_path="scope.py",
                    language="python",
                    symbol_kind="function",
                    qualified_name=qualified_name,
                    start_line=1,
                    end_line=source.count("\n"),
                    source_sha256="0" * 64,
                    function_sha256="1" * 64,
                    source=source,
                )
                result = deterministic_python_contract(task, valid_result())
                failures = {
                    issue.start_line: issue.failure_type
                    for issue in result.issues
                    if issue.failure_type in {"NameError", "UnboundLocalError"}
                }
                self.assertEqual(failures, expected)

    def test_definite_assignment_uses_only_continuing_branch_state(self) -> None:
        source = (
            "def continuing(enabled: bool) -> str:\n"
            "    if enabled:\n"
            "        label = 'ready'\n"
            "    else:\n"
            "        return 'disabled'\n"
            "    return label\n"
        )
        task = project_function_analysis.FunctionAnalysisTask(
            symbol_id=1,
            project_id="p",
            user_id=1,
            file_id=1,
            file_path="scope.py",
            language="python",
            symbol_kind="function",
            qualified_name="continuing",
            start_line=1,
            end_line=source.count("\n"),
            source_sha256="0" * 64,
            function_sha256="1" * 64,
            source=source,
        )

        result = deterministic_python_contract(task, valid_result())

        self.assertFalse(
            any(
                issue.failure_type in {"NameError", "UnboundLocalError"}
                for issue in result.issues
            )
        )

    def test_request_drops_issue_lines_outside_the_indexed_function(self) -> None:
        with patch.object(
            analysis_engine,
            "ask_ollama",
            return_value=valid_result(issue_line=99).model_dump_json(),
        ):
            result = analysis_engine.request_function_analysis(
                language="python",
                file_path="demo/main.py",
                symbol_kind="function",
                qualified_name="answer",
                start_line=10,
                end_line=11,
                source="def answer(value):\n    return value",
            )

        self.assertEqual(result.issues, [])

    def test_request_normalizes_common_model_schema_drift(self) -> None:
        drifted = json.dumps(
            {
                "name": "answer",
                "summary": "Returns a boolean.",
                "syntax_valid": True,
                "parameters": [
                    {
                        "name": "value",
                        "type": "str",
                        "description": "Input value.",
                    }
                ],
                "return_type": "bool",
                "raised_exceptions": ["ValueError"],
                "side_effects": [],
                "issues": [],
                "confidence": 0.8,
            }
        )
        with patch.object(analysis_engine, "ask_ollama", return_value=drifted):
            result = analysis_engine.request_function_analysis(
                language="python",
                file_path="demo/main.py",
                symbol_kind="function",
                qualified_name="answer",
                start_line=10,
                end_line=11,
                source="def answer(value):\n    return bool(value)",
            )

        self.assertEqual(result.parameters[0].accepted_types, ["str"])
        self.assertEqual(result.returns.possible_types[0].type, "bool")
        self.assertEqual(result.raised_errors, ["ValueError"])

    def test_request_normalizes_camel_case_return_contract(self) -> None:
        drifted = json.dumps(
            {
                "summary": "Returns an integer.",
                "syntax_valid": True,
                "parameters": [],
                "returns": {
                    "mayReturnValue": True,
                    "possibleTypes": [
                        {"type": "int", "description": "Returned integer."}
                    ],
                    "nullable": False,
                    "description": "Integer result.",
                },
                "issues": [],
                "confidence": 0.8,
            }
        )

        result = analysis_engine.normalize_function_analysis_payload(drifted)

        self.assertTrue(result.returns.may_return_value)
        self.assertEqual(result.returns.possible_types[0].type, "int")

    def test_request_drops_extra_return_contract_fields(self) -> None:
        drifted = json.dumps(
            {
                "summary": "Builds a normalized return contract.",
                "syntax_valid": True,
                "parameters": [],
                "returns": {
                    "type": "FunctionReturnContract",
                    "description": "Normalized return contract.",
                    "notes": "Model commentary that is not part of the schema.",
                },
                "issues": [],
                "confidence": 0.8,
            }
        )

        result = analysis_engine.normalize_function_analysis_payload(drifted)

        self.assertTrue(result.returns.may_return_value)
        self.assertEqual(result.returns.possible_types[0].type, "FunctionReturnContract")
        self.assertEqual(result.returns.description, "Normalized return contract.")

    def test_request_resolves_contradictory_return_contract_flags(self) -> None:
        drifted = json.dumps(
            {
                "summary": "Returns status data.",
                "syntax_valid": True,
                "parameters": [],
                "returns": {
                    "may_return_value": False,
                    "possible_types": [
                        {"type": "dict[str, object]", "description": "Status data."}
                    ],
                    "nullable": False,
                    "description": "",
                },
                "issues": [],
                "confidence": 0.8,
            }
        )

        result = analysis_engine.normalize_function_analysis_payload(drifted)

        self.assertTrue(result.returns.may_return_value)
        self.assertEqual(result.returns.possible_types[0].type, "dict[str, object]")
        self.assertEqual(result.returns.description, "Returns dict[str, object]")

    def test_request_resolves_non_value_return_marked_nullable(self) -> None:
        drifted = json.dumps(
            {
                "summary": "Performs work and returns no explicit value.",
                "syntax_valid": True,
                "parameters": [],
                "returns": {
                    "may_return_value": False,
                    "possible_types": [],
                    "nullable": True,
                    "description": "None",
                },
                "issues": [],
                "confidence": 0.8,
            }
        )

        result = analysis_engine.normalize_function_analysis_payload(drifted)

        self.assertFalse(result.returns.may_return_value)
        self.assertFalse(result.returns.nullable)
        self.assertEqual(result.returns.possible_types, [])

    def test_normalizer_downgrades_contradictory_model_syntax_errors(self) -> None:
        raw = json.dumps(
            {
                "summary": "Lists chats.",
                "syntax_valid": True,
                "parameters": [],
                "returns": {"type": "dict[str, object]", "description": "Response."},
                "issues": [
                    {
                        "severity": "error",
                        "category": "syntax",
                        "title": "Syntax issue",
                        "description": "Model thinks there is invalid syntax.",
                    }
                ],
                "confidence": 0.85,
            }
        )

        result = analysis_engine.normalize_function_analysis_payload(raw)

        self.assertTrue(result.syntax_valid)
        self.assertEqual(result.issues[0].severity, "warning")
        self.assertEqual(result.issues[0].category, "syntax")

    def test_normalizer_relabels_contract_risk_errors_as_unsafe(self) -> None:
        raw = json.dumps(
            {
                "summary": "Collects source identifiers.",
                "syntax_valid": True,
                "parameters": [],
                "returns": {"type": "set[str]", "description": "Identifiers."},
                "issues": [
                    {
                        "severity": "error",
                        "category": "runtime",
                        "title": "Potential dictionary access failure",
                        "description": (
                            "The expression facts['identifiers'] assumes that the key exists "
                            "and its value is iterable; otherwise a KeyError or TypeError will occur."
                        ),
                    }
                ],
                "confidence": 0.82,
            }
        )

        result = analysis_engine.normalize_function_analysis_payload(raw)

        self.assertEqual(result.issues[0].severity, "unsafe")
        self.assertEqual(result.issues[0].category, "runtime")

    def test_normalizer_repairs_invalid_backslash_escapes_in_json_strings(self) -> None:
        raw = r"""
        {
          "summary": "Handles patterns like \s+ in source snippets.",
          "syntax_valid": true,
          "parameters": [],
          "returns": {
            "type": "str",
            "description": "Cleaned text."
          },
          "raised_errors": [],
          "side_effects": [],
          "issues": [],
          "confidence": 0.6
        }
        """

        result = analysis_engine.normalize_function_analysis_payload(raw)

        self.assertIn(r"\s+", result.summary)
        self.assertEqual(result.returns.possible_types[0].type, "str")

    def test_normalizer_extracts_embedded_json_object(self) -> None:
        raw = '''Here is the requested result:\n```json
        {"summary":"Returns the value.","syntax_valid":true,"parameters":[],
         "returns":{"type":"str","description":"Returned value."},
         "issues":[],"confidence":0.7}
        ```'''

        result = analysis_engine.normalize_function_analysis_payload(raw)

        self.assertEqual(result.summary, "Returns the value.")
        self.assertEqual(result.returns.possible_types[0].type, "str")

    def test_normalizer_accepts_python_style_mapping_from_local_backend(self) -> None:
        raw = """{'summary':'Returns the value.','syntax_valid':True,'parameters':[],
        'returns':{'type':'str','description':'Returned value.'},
        'issues':[],'confidence':0.7}"""

        result = analysis_engine.normalize_function_analysis_payload(raw)

        self.assertEqual(result.summary, "Returns the value.")
        self.assertEqual(result.returns.possible_types[0].type, "str")

    def test_normalizer_repairs_literal_control_characters_in_json_strings(self) -> None:
        raw = """{
          "summary": "Returns the\nvalue.\tSafely.",
          "syntax_valid": true,
          "parameters": [],
          "returns": {"type": "str", "description": "Returned value."},
          "issues": [],
          "confidence": 0.7
        }"""

        result = analysis_engine.normalize_function_analysis_payload(raw)

        self.assertEqual(result.summary, "Returns the\nvalue.\tSafely.")
        self.assertEqual(result.returns.possible_types[0].type, "str")

    def test_normalizer_ignores_extra_closing_brace_after_complete_object(self) -> None:
        raw = """{
          "summary": "Returns the value.",
          "syntax_valid": true,
          "parameters": [],
          "returns": {"type": "str", "description": "Returned value."},
          "issues": [],
          "confidence": 0.7
        }}"""

        result = analysis_engine.normalize_function_analysis_payload(raw)

        self.assertEqual(result.summary, "Returns the value.")
        self.assertEqual(result.returns.possible_types[0].type, "str")

    def test_normalizer_repairs_missing_commas_between_nested_json_values(self) -> None:
        raw = """
        {
          "contract_version": "1.0",
          "summary": "Checks a value.",
          "syntax_valid": true,
          "parameters": [
            {
              "name": "value",
              "kind": "positional_or_keyword",
              "required": true,
              "accepted_types": [
                "str"
                "None"
              ],
              "description": "Input value."
            }
          ],
          "returns": {
            "may_return_value": true,
            "possible_types": [
              {
                "type": "str",
                "description": "Returned text."
              }
              {
                "type": "None",
                "description": "No value."
              }
            ],
            "nullable": true,
            "description": "Text or null."
          },
          "raised_errors": [],
          "side_effects": [],
          "issues": [],
          "confidence": 0.6
        }
        """

        result = analysis_engine.normalize_function_analysis_payload(raw)

        self.assertEqual(result.parameters[0].accepted_types, ["str", "None"])
        self.assertEqual(
            [item.type for item in result.returns.possible_types],
            ["str", "None"],
        )

    def test_request_normalizes_list_valued_return_type(self) -> None:
        drifted = json.dumps(
            {
                "summary": "Returns unique strings.",
                "syntax_valid": True,
                "parameters": [
                    {
                        "name": "values",
                        "accepted_types": ["list[str]"],
                        "description": "Input strings.",
                    }
                ],
                "returns": {
                    "type": ["list[str]"],
                    "description": "Unique strings.",
                },
                "raised_errors": [],
                "side_effects": [],
                "issues": [],
                "confidence": 0.8,
            }
        )
        with patch.object(analysis_engine, "ask_ollama", return_value=drifted):
            result = analysis_engine.request_function_analysis(
                language="python",
                file_path="analysis_engine.py",
                symbol_kind="function",
                qualified_name="_string_list",
                start_line=199,
                end_line=219,
                source="def _string_list(value):\n    return []",
            )

        self.assertEqual(result.returns.possible_types[0].type, "list[str]")
        self.assertEqual(result.returns.description, "Unique strings.")

    def test_request_repairs_missing_comma_in_model_json(self) -> None:
        malformed = (
            "{\n"
            '  "summary": "Normalizes model output.",\n'
            '  "syntax_valid": true,\n'
            '  "parameters": []\n'
            '  "returns": {"type": "FunctionAnalysisResult", "description": "Normalized result."},\n'
            '  "raised_exceptions": [],\n'
            '  "side_effects": [],\n'
            '  "issues": [],\n'
            '  "confidence": 0.75\n'
            "}\n"
        )
        with patch.object(analysis_engine, "ask_ollama", return_value=malformed):
            result = analysis_engine.request_function_analysis(
                language="python",
                file_path="analysis_engine.py",
                symbol_kind="function",
                qualified_name="normalize_function_analysis_payload",
                start_line=343,
                end_line=387,
                source="def normalize_function_analysis_payload(raw):\n    return raw",
            )

        self.assertEqual(result.returns.possible_types[0].type, "FunctionAnalysisResult")
        self.assertEqual(result.confidence, 0.75)

    def test_request_repairs_missing_comma_inside_nested_object(self) -> None:
        malformed = (
            "{\n"
            '  "summary": "Requests analysis.",\n'
            '  "syntax_valid": true,\n'
            '  "parameters": [],\n'
            '  "returns": {\n'
            '    "possible_types": [\n'
            '      {\n'
            '        "type": "FunctionAnalysisResult"\n'
            '        "description": "Validated result."\n'
            "      }\n"
            "    ],\n"
            '    "may_return_value": true,\n'
            '    "nullable": false,\n'
            '    "description": "Returns the result."\n'
            "  },\n"
            '  "raised_errors": [],\n'
            '  "side_effects": [],\n'
            '  "issues": [],\n'
            '  "confidence": 0.7\n'
            "}\n"
        )
        with patch.object(analysis_engine, "ask_ollama", return_value=malformed):
            result = analysis_engine.request_function_analysis(
                language="python",
                file_path="analysis_engine.py",
                symbol_kind="function",
                qualified_name="request_function_analysis",
                start_line=454,
                end_line=498,
                source="def request_function_analysis():\n    return result\n",
            )

        self.assertEqual(result.returns.possible_types[0].type, "FunctionAnalysisResult")

    def test_request_retries_invalid_json_once_with_original_source_and_feedback(self) -> None:
        calls: list[tuple[list[dict[str, str]], dict[str, object]]] = []

        def fake_ask(messages, **kwargs):
            calls.append((messages, kwargs))
            return '{"summary": "broken", "returns": {'

        with patch.object(analysis_engine, "ask_ollama", fake_ask):
            with self.assertRaises(json.JSONDecodeError):
                analysis_engine.request_function_analysis(
                    language="python",
                    file_path="demo/main.py",
                    symbol_kind="function",
                    qualified_name="answer",
                    start_line=10,
                    end_line=11,
                    source="def answer(value):\n    return value",
                )

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][0], calls[1][0][0])
        self.assertIn("failed validation", calls[1][0][-1]["content"])

    def test_chunk_request_describes_fragment_boundaries_and_validates_issue_lines(self) -> None:
        calls: list[tuple[list[dict[str, str]], dict[str, object]]] = []

        def fake_ask(messages, **kwargs):
            calls.append((messages, kwargs))
            return valid_result(issue_line=21).model_dump_json()

        with patch.object(analysis_engine, "ask_ollama", fake_ask):
            result = analysis_engine.request_function_chunk_analysis(
                language="python",
                file_path="demo/main.py",
                symbol_kind="function",
                qualified_name="large",
                function_start_line=2,
                function_end_line=40,
                chunk_start_line=20,
                chunk_end_line=22,
                chunk_index=2,
                chunk_total=4,
                source="    value += 1\n    if value:\n        return value",
            )

        self.assertEqual(result.issues[0].start_line, 21)
        prompt = calls[0][0][0]["content"]
        self.assertIn("Fragment: 2 of 4", prompt)
        self.assertIn("Allowed issue lines: 20-22", prompt)
        self.assertIn("    20:     value += 1", prompt)
        self.assertEqual(calls[0][1]["temperature"], 0)

    def test_chunk_request_drops_issue_lines_outside_fragment(self) -> None:
        with patch.object(
            analysis_engine,
            "ask_ollama",
            return_value=valid_result(issue_line=99).model_dump_json(),
        ):
            result = analysis_engine.request_function_chunk_analysis(
                language="python",
                file_path="demo/main.py",
                symbol_kind="function",
                qualified_name="large",
                function_start_line=2,
                function_end_line=40,
                chunk_start_line=20,
                chunk_end_line=22,
                chunk_index=2,
                chunk_total=4,
                source="    value += 1\n    if value:\n        return value",
            )

        self.assertEqual(result.issues, [])


class FunctionChunkingTests(DatabaseTestCase):
    def test_splitter_preserves_source_order_size_and_absolute_lines(self) -> None:
        source = "first line\nsecond line is longer\nthird"
        chunks = split_function_source(source, 10, chunk_chars=12)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunk.source for chunk in chunks), source)
        self.assertTrue(all(len(chunk.source) <= 12 for chunk in chunks))
        self.assertEqual(chunks[0].start_line, 10)
        self.assertEqual(chunks[-1].end_line, 12)
        self.assertEqual(
            [(chunk.index, chunk.total) for chunk in chunks],
            [(index, len(chunks)) for index in range(1, len(chunks) + 1)],
        )

    def test_fragment_contracts_merge_without_another_model_call(self) -> None:
        first = valid_result(issue_line=2)
        second = FunctionAnalysisResult(
            contract_version="1.0",
            summary="Returns text on the alternate branch.",
            syntax_valid=True,
            parameters=[],
            returns=FunctionReturnContract(
                may_return_value=True,
                possible_types=[
                    FunctionReturnType(type="str", description="Alternate text.")
                ],
                nullable=True,
                description="Returns text or null.",
            ),
            raised_errors=["ValueError"],
            side_effects=["writes a log entry"],
            issues=[],
            confidence=0.71,
        )
        merged = merge_function_chunk_analyses([first, second])
        self.assertEqual(merged.parameters[0].name, "value")
        self.assertEqual(
            [item.type for item in merged.returns.possible_types], ["int", "str"]
        )
        self.assertTrue(merged.returns.nullable)
        self.assertEqual(merged.raised_errors, ["ValueError"])
        self.assertEqual(merged.confidence, 0.71)
        self.assertIn("2 bounded fragments", merged.summary)


class FunctionAnalysisPersistenceTests(DatabaseTestCase):
    def test_mutation_corpus_measures_engine_coverage_without_model_findings(self):
        from analysis_benchmark import (BenchmarkManifest, CleanRegion, default_mutation_recipes,
                                        evaluate_project, project_benchmark_details, load_project_observations)
        recipes = default_mutation_recipes()
        first = recipes[0]
        self.create_indexed_project(first.clean_source.encode(), path=f"clean/{first.name}.{first.extension}")
        with main.connect_db() as db:
            for recipe in recipes:
                for variant, source in (("clean", recipe.clean_source), ("mutated", recipe.mutated_source)):
                    if recipe is first and variant == "clean":
                        continue
                    content = source.encode()
                    db.execute(
                        "INSERT INTO project_files(project_id, path, content, size_bytes, sha256, is_binary) VALUES (?, ?, ?, ?, ?, 0)",
                        ("analysis-project", f"{variant}/{recipe.name}.{recipe.extension}", content, len(content), hashlib.sha256(content).hexdigest()),
                    )
            inventory_project_database(db, "analysis-project")
            parse_project_database(db, "analysis-project")
        manifest = BenchmarkManifest("engine coverage", tuple(recipe.expectation for recipe in recipes), tuple(
            CleanRegion(f"clean-{recipe.name}", f"clean/{recipe.name}.{recipe.extension}", recipe.clean_source.splitlines()[0])
            for recipe in recipes
        ))
        # The stand-in contributes no findings or inferred contracts. This tests
        # actual persistence/filtering and measures only deterministic coverage.
        summary = analyze_project_functions(main.connect_db, "analysis-project", analysis_request=lambda **kwargs: project_function_analysis._deterministic_seed_result())
        self.assertEqual(summary.failed_count, 0)
        with main.connect_db() as db:
            from project_call_compatibility import check_project_call_compatibility
            check_project_call_compatibility(db, "analysis-project")
            metrics = evaluate_project(db, "analysis-project", manifest, analyzer_version="engine-only-test")
            details = project_benchmark_details(db, "analysis-project", manifest, analyzer_version="engine-only-test")
            findings, _ = load_project_observations(db, "analysis-project")
        self.assertEqual(metrics.false_positive_count, 0, [finding for finding in findings if finding.id in metrics.false_positive_finding_ids])
        self.assertTrue(all(finding.report_tier == "advisory" for finding in findings if finding.id in metrics.clean_region_finding_ids))
        self.assertIn("javascript-null-member", metrics.matched_expectation_ids)
        self.assertIn("python-branch-assignment", metrics.matched_expectation_ids)
        self.assertEqual(metrics.missed_expectation_ids, ())
        self.assertEqual(len(details["by_language"]), 8)
        self.assertEqual(details["execution"]["function_analysis_model_request_count"], summary.model_request_count)

    def test_javascript_caller_receives_resolved_source_and_prior_callee_contract(self):
        self.create_indexed_project(
            b"function caller(value) { return helper(value); }\n"
            b"function helper(value) { return value; }\n", path="demo/main.js",
        )
        calls = []
        def review(**kwargs):
            calls.append(kwargs)
            return valid_result()
        summary = analyze_project_functions(main.connect_db, "analysis-project", analysis_request=review)
        self.assertEqual(summary.status, "completed")
        self.assertEqual([call["qualified_name"] for call in calls], ["helper", "caller"])
        self.assertIn("Resolved source", calls[1]["analysis_context"])
        self.assertIn("function helper", calls[1]["analysis_context"])
        self.assertIn("hypothesis; verify against source", calls[1]["analysis_context"])

    def test_recursive_functions_do_not_exchange_inferred_contracts(self):
        self.create_indexed_project(
            b"function first(value) { return second(value); }\n"
            b"function second(value) { return first(value); }\n", path="demo/main.js",
        )
        calls = []
        summary = analyze_project_functions(
            main.connect_db, "analysis-project",
            analysis_request=lambda **kwargs: calls.append(kwargs) or valid_result(),
        )
        self.assertEqual(summary.status, "completed")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("Inferred callee contract" not in call["analysis_context"] for call in calls))

    def test_typed_io_function_still_receives_semantic_review(self):
        self.create_indexed_project(b"def save(path: str) -> None:\n    open(path, 'w').write('data')\n")
        calls = []
        analyze_project_functions(main.connect_db, "analysis-project", analysis_request=lambda **kwargs: calls.append(kwargs) or valid_result())
        self.assertEqual(len(calls), 1)

    def test_shadowed_builtin_still_receives_semantic_review(self):
        self.create_indexed_project(b"from typing import Callable\ndef convert(str: Callable, value: int) -> str:\n    return str(value)\n")
        calls = []
        analyze_project_functions(main.connect_db, "analysis-project", analysis_request=lambda **kwargs: calls.append(kwargs) or valid_result())
        self.assertEqual(len(calls), 1)

    def test_missing_guard_proof_or_fabricated_guard_is_rejected(self):
        self.create_indexed_project(b"def answer(value):\n    return value\n")
        with main.connect_db() as db:
            symbol_id = db.execute("SELECT id FROM project_symbols WHERE name = 'answer'").fetchone()[0]
            task = load_function_analysis_task(db, symbol_id)
            for updates in ({"guard_check": None}, {"reachability": None}, {"guard_evidence": ["if value > 0:"]}):
                issue = valid_result(issue_line=2).issues[0].model_copy(update=updates)
                result = filter_source_proven_false_issues(db, task, valid_result().model_copy(update={"issues": [issue]}))
                self.assertEqual(result.issues, [])

    def test_changed_callee_body_invalidates_unchanged_javascript_caller(self):
        user_id = self.create_user("js-dependency-cache")
        caller = b"function caller(value) { return helper(value); }\n"
        self.create_indexed_project(caller + b"function helper(value) { return value + 1; }\n", "before", user_id=user_id, path="demo/main.js")
        self.create_indexed_project(caller + b"function helper(value) { return value + 2; }\n", "after", user_id=user_id, path="demo/main.js")
        with main.connect_db() as db:
            tasks = [load_function_analysis_task(db, row[0]) for row in db.execute("SELECT id FROM project_symbols WHERE name = 'caller' ORDER BY project_id")]
        self.assertEqual(tasks[0].source, tasks[1].source)
        self.assertNotEqual(tasks[0].cache_function_sha256, tasks[1].cache_function_sha256)

    def test_engine_null_access_rule_is_persisted_even_when_model_misses_it(self):
        self.create_indexed_project(b"function value() { return null.name; }\n", path="demo/main.js")
        summary = analyze_project_functions(main.connect_db, "analysis-project", analysis_request=lambda **kwargs: valid_result())
        self.assertEqual(summary.status, "completed")
        with main.connect_db() as db:
            issues = db.execute("SELECT title, provenance, evidence FROM project_symbol_issues").fetchall()
        self.assertEqual(len(issues), 1)
        self.assertEqual(tuple(issues[0]), ("Property access on null", "deterministic", "null.name"))

    def create_indexed_project(
        self,
        content: bytes,
        project_id: str = "analysis-project",
        *,
        user_id: int | None = None,
        path: str = "demo/main.py",
    ) -> int:
        if user_id is None:
            user_id = self.create_user(project_id)
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES (?, ?, 'Analysis')",
                (f"{project_id}-chat", user_id),
            )
            db.execute(
                """
                INSERT INTO projects(id, user_id, chat_id, name, source_kind, file_count, total_bytes)
                VALUES (?, ?, ?, 'demo', 'folder', 1, ?)
                """,
                (project_id, user_id, f"{project_id}-chat", len(content)),
            )
            cursor = db.execute(
                """
                INSERT INTO project_files(project_id, path, content, size_bytes, sha256, is_binary)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (project_id, path, content, len(content), hashlib.sha256(content).hexdigest()),
            )
            inventory_project_database(db, project_id)
            parse_project_database(db, project_id)
            return int(cursor.lastrowid)

    def test_source_task_is_exact_hash_checked_function_range(self) -> None:
        content = (
            b"# outside\n"
            b"def first(value):\n    return value\n\n"
            b"def second():\n    return 2\n"
        )
        self.create_indexed_project(content)
        with main.connect_db() as db:
            symbol_id = int(
                db.execute(
                    "SELECT id FROM project_symbols WHERE qualified_name = 'first'"
                ).fetchone()[0]
            )
            task = load_function_analysis_task(db, symbol_id)
        self.assertEqual(task.source, "def first(value):\n    return value")
        self.assertNotIn("outside", task.source)
        self.assertNotIn("second", task.source)
        self.assertEqual(task.start_line, 2)
        self.assertEqual(task.end_line, 3)

    def test_source_task_includes_only_referenced_project_dependency_context(self) -> None:
        content = (
            b"import sqlite3\n"
            b"SETTING = 7\n\n"
            b"def helper(session_id: str, role: str, content: str) -> None:\n"
            b"    return None\n\n"
            b"def unrelated(secret: str) -> str:\n"
            b"    return secret.upper()\n\n"
            b"def caller(db: sqlite3.Connection) -> int:\n"
            b"    helper('chat', 'user')\n"
            b"    return SETTING\n"
        )
        self.create_indexed_project(content, "dependency-context-project")
        with main.connect_db() as db:
            symbol = db.execute(
                "SELECT id FROM project_symbols WHERE project_id = ? AND qualified_name = ?",
                ("dependency-context-project", "caller"),
            ).fetchone()
            task = load_function_analysis_task(db, int(symbol["id"]))

        self.assertIn("import sqlite3", task.analysis_context)
        self.assertIn("SETTING = 7", task.analysis_context)
        self.assertIn(
            "def helper(session_id: str, role: str, content: str) -> None: ...",
            task.analysis_context,
        )
        self.assertNotIn("unrelated", task.analysis_context)
        self.assertNotIn("return secret.upper()", task.analysis_context)
        self.assertNotEqual(task.cache_function_sha256, task.function_sha256)
        self.assertNotEqual(
            task.semantic_cache_function_sha256,
            task.semantic_function_sha256,
        )

    def test_method_task_includes_matching_override_contract(self) -> None:
        content = (
            b"class Base:\n"
            b"    def convert(self, value: ParsedSource) -> Result:\n"
            b"        raise NotImplementedError\n\n"
            b"class Child(Base):\n"
            b"    def convert(self, value):\n"
            b"        return []\n"
        )
        self.create_indexed_project(content, "override-contract-project")
        with main.connect_db() as db:
            symbol = db.execute(
                "SELECT id FROM project_symbols WHERE project_id = ? AND qualified_name = ?",
                ("override-contract-project", "Child.convert"),
            ).fetchone()
            task = load_function_analysis_task(db, int(symbol["id"]))

        self.assertIn(
            "def convert(self, value: ParsedSource) -> Result: ...",
            task.analysis_context,
        )

    def test_dependency_context_change_invalidates_unchanged_function_cache(self) -> None:
        user_id = self.create_user("dependency-cache-owner")
        caller = b"def caller():\n    return helper(1)\n"
        self.create_indexed_project(
            b"def helper(value: int) -> int:\n    return value\n\n" + caller,
            "dependency-cache-source",
            user_id=user_id,
        )
        self.create_indexed_project(
            (
                b"def helper(value: int, fallback: int = 0) -> int:\n"
                b"    return value\n\n"
                + caller
            ),
            "dependency-cache-target",
            user_id=user_id,
        )
        first_calls: list[str] = []
        analyze_project_functions(
            main.connect_db,
            "dependency-cache-source",
            analysis_request=lambda **kwargs: (
                first_calls.append(kwargs["qualified_name"]) or valid_result()
            ),
        )
        second_calls: list[str] = []
        summary = analyze_project_functions(
            main.connect_db,
            "dependency-cache-target",
            analysis_request=lambda **kwargs: (
                second_calls.append(kwargs["qualified_name"]) or valid_result()
            ),
        )

        self.assertEqual(first_calls, ["caller"])
        self.assertEqual(second_calls, ["caller"])
        self.assertEqual(summary.cache_hit_count, 0)

    def test_source_proven_undefined_helper_errors_are_filtered(self) -> None:
        user_id = self.create_user("false-undefined")
        self.create_indexed_project(
            (
                b"import re\n"
                b"from app_config import (\n"
                b"    IMPORTED_SETTING,\n"
                b")\n"
                b"LOGGER = object()\n\n"
                b"def helper(value):\n"
                b"    return re.sub('x', 'y', value)\n\n"
                b"def caller(value):\n"
                b"    LOGGER\n"
                b"    return helper(value)\n"
            ),
            "false-undefined-project",
            user_id=user_id,
        )
        with main.connect_db() as db:
            symbol = db.execute(
                """
                SELECT id FROM project_symbols
                WHERE project_id = 'false-undefined-project'
                  AND qualified_name = 'caller'
                """
            ).fetchone()
            task = load_function_analysis_task(db, int(symbol["id"]))
            result = valid_result().model_copy(
                update={
                    "issues": [
                        FunctionIssue(
                            severity="error",
                            category="maintainability",
                            title="Undefined function 'helper'",
                            description="Function 'helper' is called but not imported or defined.",
                        ),
                        FunctionIssue(
                            severity="error",
                            category="maintainability",
                            title="Missing import for module 're'",
                            description="Missing import for module 're'.",
                        ),
                        FunctionIssue(
                            severity="error",
                            category="runtime",
                            title="Possibly undefined variable",
                            description="Name `IMPORTED_SETTING` is read before it is defined in this function.",
                            start_line=8,
                            end_line=8,
                        ),
                        proven_issue(
                            severity="warning",
                            category="logic",
                            title="Real warning",
                            description="This warning should remain.",
                            start_line=12,
                            end_line=12,
                            evidence="return helper(value)",
                        ),
                    ]
                }
            )
            filtered = filter_source_proven_false_issues(db, task, result)

        self.assertEqual([issue.title for issue in filtered.issues], ["Real warning"])

    def test_filter_removes_source_contradicted_global_name_errors(self) -> None:
        self.create_indexed_project(
            (
                b"import re\n"
                b"from app_config import (\n"
                b"    DB_PATH,\n"
                b"    LOGIN_MAX_FAILED_ATTEMPTS,\n"
                b"    CHAT_LIST_PAGE_SIZE,\n"
                b")\n"
                b"READINESS_CACHE = None\n"
                b"PROCESS_STARTED_AT = 1\n"
                b"PROCESS_STARTED_MONOTONIC = 2\n\n"
                b"def target(value):\n"
                b"    return f'{value}:{account_id}'\n"
            ),
            "global-false-positive-project",
        )
        with main.connect_db() as db:
            symbol = db.execute(
                """
                SELECT id FROM project_symbols
                WHERE project_id = 'global-false-positive-project'
                  AND qualified_name = 'target'
                """
            ).fetchone()
            task = load_function_analysis_task(db, int(symbol["id"]))
            result = valid_result().model_copy(
                update={
                    "issues": [
                        FunctionIssue(
                            severity="error",
                            category="maintainability",
                            title="Globals used without imports",
                            description=(
                                "The function uses the global names `re` and `DB_PATH` "
                                "without importing them or ensuring they exist."
                            ),
                        ),
                        FunctionIssue(
                            severity="error",
                            category="logic",
                            title="Potential NameError from Undefined Constants",
                            description=(
                                "Comparison failures >= LOGIN_MAX_FAILED_ATTEMPTS uses "
                                "global constant which must be defined elsewhere; if "
                                "undefined, NameError occurs."
                            ),
                        ),
                        FunctionIssue(
                            severity="error",
                            category="maintainability",
                            title='"READINESS_CACHE" referenced before assignment',
                            description=(
                                '"READINESS_CACHE" referenced before assignment if '
                                "never initialized elsewhere."
                            ),
                        ),
                        FunctionIssue(
                            severity="error",
                            category="runtime",
                            title="NameError for undefined constant",
                            description=(
                                "Hardcoded limit of `CHAT_LIST_PAGE_SIZE + 1`; if this "
                                "constant is not defined elsewhere, NameError occurs."
                            ),
                        ),
                        FunctionIssue(
                            severity="error",
                            category="runtime",
                            title="Global variables PROCESS_STARTED_AT/MONOTONIC undefined",
                            description=(
                                "Return value includes PROCESS_STARTED_AT and "
                                "PROCESS_STARTED_MONOTONIC which must exist globally; "
                                "otherwise NameError."
                            ),
                        ),
                        proven_issue(
                            severity="error",
                            category="runtime",
                            title="Possibly undefined variable",
                            description=(
                                "Name `account_id` is read before it is defined in this function."
                            ),
                            start_line=12,
                            end_line=12,
                            evidence="account_id",
                            failure_type="NameError",
                        ),
                    ]
                }
            )
            filtered = filter_source_proven_false_issues(db, task, result)

        self.assertEqual(
            [issue.title for issue in filtered.issues],
            ["Possibly undefined variable"],
        )

    def test_filter_rejects_unanchored_model_errors_and_keeps_proven_findings(self) -> None:
        self.create_indexed_project(
            b"def target(value):\n    return value\n",
            "unanchored-model-error-project",
        )
        with main.connect_db() as db:
            symbol = db.execute(
                """
                SELECT id FROM project_symbols
                WHERE project_id = 'unanchored-model-error-project'
                  AND qualified_name = 'target'
                """
            ).fetchone()
            task = load_function_analysis_task(db, int(symbol["id"]))
            result = valid_result().model_copy(
                update={
                    "issues": [
                        FunctionIssue(
                            severity="error",
                            category="maintainability",
                            title="Generic model critical",
                            description="Unanchored model claim should not remain critical.",
                        ),
                        proven_issue(
                            severity="error",
                            category="runtime",
                            title="Anchored model critical",
                            description="Line-anchored model claim should remain critical.",
                            start_line=2,
                            end_line=2,
                            evidence="return value",
                            failure_type="RuntimeError",
                        ),
                        FunctionIssue(
                            severity="error",
                            category="security",
                            title="Unanchored security critical",
                            description="Security findings are treated as strong evidence.",
                        ),
                        FunctionIssue(
                            severity="error",
                            category="maintainability",
                            title="Deterministic critical",
                            description="Deterministic findings should keep their severity.",
                            provenance="deterministic",
                        ),
                    ]
                }
            )
            filtered = filter_source_proven_false_issues(db, task, result)

        severities = {issue.title: issue.severity for issue in filtered.issues}
        self.assertNotIn("Generic model critical", severities)
        self.assertEqual(severities["Anchored model critical"], "unsafe")
        self.assertNotIn("Unanchored security critical", severities)
        self.assertEqual(severities["Deterministic critical"], "error")

    def test_filter_removes_unanchored_speculative_model_noise(self) -> None:
        self.create_indexed_project(
            b"def target(value):\n    result = helper\n    return value + result\n",
            "speculative-model-noise-project",
        )
        with main.connect_db() as db:
            symbol = db.execute(
                """
                SELECT id FROM project_symbols
                WHERE project_id = 'speculative-model-noise-project'
                  AND qualified_name = 'target'
                """
            ).fetchone()
            task = load_function_analysis_task(db, int(symbol["id"]))
            result = valid_result().model_copy(
                update={
                    "issues": [
                        FunctionIssue(
                            severity="unsafe",
                            category="maintainability",
                            title="Unknown helper contract",
                            description=(
                                "The helper result may fail if callers pass values whose "
                                "implementation details are unknown from context."
                            ),
                        ),
                        FunctionIssue(
                            severity="warning",
                            category="maintainability",
                            title="Missing context",
                            description=(
                                "The source does not guarantee that external callers provide "
                                "the required values, so this might fail depending on implementation."
                            ),
                        ),
                        FunctionIssue(
                            severity="info",
                            category="maintainability",
                            title="Type cannot be verified statically",
                            description="This cannot be verified statically from the provided source.",
                        ),
                        proven_issue(
                            severity="error",
                            category="runtime",
                            title="Possibly undefined variable",
                            description="Name `helper` is read before it is defined in this function.",
                            start_line=2,
                            end_line=2,
                            evidence="helper",
                            failure_type="NameError",
                        ),
                    ]
                }
            )
            filtered = filter_source_proven_false_issues(db, task, result)

        self.assertEqual(
            [(issue.title, issue.severity) for issue in filtered.issues],
            [("Possibly undefined variable", "unsafe")],
        )

    def test_filter_removes_placeholder_and_self_negating_model_noise(self) -> None:
        self.create_indexed_project(
            b"def target(row_count: int) -> int:\n    return row_count\n",
            "placeholder-model-noise-project",
        )
        with main.connect_db() as db:
            symbol = db.execute(
                """
                SELECT id FROM project_symbols
                WHERE project_id = 'placeholder-model-noise-project'
                  AND qualified_name = 'target'
                """
            ).fetchone()
            task = load_function_analysis_task(db, int(symbol["id"]))
            result = valid_result().model_copy(
                update={
                    "issues": [
                        FunctionIssue(
                            severity="warning",
                            category="maintainability",
                            title="Potential issue",
                            description="Potential issue",
                        ),
                        FunctionIssue(
                            severity="unsafe",
                            category="maintainability",
                            title="Tuple parameters are mitigated",
                            description=(
                                "SQL query uses placeholders and the code passes parameters via "
                                "tuple, which mitigates injection."
                            ),
                        ),
                        FunctionIssue(
                            severity="warning",
                            category="maintainability",
                            title="Return path",
                            description="This access is safe under current logic.",
                        ),
                        FunctionIssue(
                            severity="error",
                            category="runtime",
                            title="Anchored undefined name",
                            description="Name `helper` is read before it is defined in this function.",
                            start_line=1,
                            end_line=1,
                        ),
                    ]
                }
            )
            filtered = filter_source_proven_false_issues(db, task, result)

        self.assertEqual(filtered.issues, [])

    def test_filter_removes_safe_getattr_attributeerror_noise(self) -> None:
        self.create_indexed_project(
            (
                b"def target(request) -> str:\n"
                b"    return getattr(request.state, 'request_id', None) or 'missing'\n"
            ),
            "getattr-noise-project",
        )
        with main.connect_db() as db:
            symbol = db.execute(
                """
                SELECT id FROM project_symbols
                WHERE project_id = 'getattr-noise-project'
                  AND qualified_name = 'target'
                """
            ).fetchone()
            task = load_function_analysis_task(db, int(symbol["id"]))
            result = valid_result().model_copy(
                update={
                    "issues": [
                        proven_issue(
                            severity="error",
                            category="runtime",
                            title="AttributeError on request.state.request_id",
                            description=(
                                '"request.state" may not have attribute "request_id", causing '
                                "AttributeError when accessing via getattr."
                            ),
                            start_line=2,
                            end_line=2,
                            evidence="getattr(request.state, 'request_id', None)",
                            failure_type="AttributeError",
                            trigger="request.state lacks request_id.",
                        )
                    ]
                }
            )
            filtered = filter_source_proven_false_issues(db, task, result)

        self.assertEqual(filtered.issues, [])

    def test_filter_uses_arbitrary_none_guard_as_counterevidence(self) -> None:
        self.create_indexed_project(
            (
                b"def target(lookup: dict[str, int] | None) -> int:\n"
                b"    if lookup is not None:\n"
                b"        return lookup.get('count', 0)\n"
                b"    return 0\n"
            ),
            "generic-none-guard-project",
        )
        with main.connect_db() as db:
            symbol = db.execute(
                "SELECT id FROM project_symbols WHERE project_id = ? AND qualified_name = ?",
                ("generic-none-guard-project", "target"),
            ).fetchone()
            task = load_function_analysis_task(db, int(symbol["id"]))
            result = valid_result().model_copy(
                update={
                    "issues": [
                        proven_issue(
                            severity="unsafe",
                            category="runtime",
                            title="`lookup` may be None",
                            description="`lookup` may be None when `.get` is evaluated.",
                            start_line=3,
                            end_line=3,
                            evidence="lookup.get('count', 0)",
                            failure_type="AttributeError",
                            trigger="lookup is None.",
                        )
                    ]
                }
            )
            filtered = filter_source_proven_false_issues(db, task, result)

        self.assertEqual(filtered.issues, [])

    def test_filter_applies_proof_and_parser_checks_to_non_python_source(self) -> None:
        self.create_indexed_project(
            b"function target(value) {\n  return value;\n}\n",
            "javascript-proof-project",
            path="demo/main.js",
        )
        with main.connect_db() as db:
            symbol = db.execute(
                "SELECT id FROM project_symbols WHERE project_id = ? AND qualified_name = ?",
                ("javascript-proof-project", "target"),
            ).fetchone()
            task = load_function_analysis_task(db, int(symbol["id"]))
            result = valid_result().model_copy(
                update={
                    "issues": [
                        proven_issue(
                            severity="error",
                            category="syntax",
                            title="Missing JavaScript terminator",
                            description="The return statement is syntactically invalid.",
                            start_line=2,
                            end_line=2,
                            evidence="return value;",
                            failure_type="SyntaxError",
                        ),
                        proven_issue(
                            severity="error",
                            category="type",
                            title="Missing call argument",
                            description="A call at this line omits a required argument.",
                            start_line=2,
                            end_line=2,
                            evidence="return value;",
                            failure_type="TypeError",
                        ),
                    ]
                }
            )
            filtered = filter_source_proven_false_issues(db, task, result)

        self.assertEqual(filtered.issues, [])

    def test_filter_reanchors_unique_exact_model_evidence(self) -> None:
        self.create_indexed_project(
            b"def target(value: int) -> int:\n    result = value + 1\n    return result\n",
            "proof-reanchor-project",
        )
        with main.connect_db() as db:
            symbol = db.execute(
                "SELECT id FROM project_symbols WHERE project_id = ? AND qualified_name = ?",
                ("proof-reanchor-project", "target"),
            ).fetchone()
            task = load_function_analysis_task(db, int(symbol["id"]))
            result = valid_result().model_copy(
                update={
                    "issues": [
                        proven_issue(
                            severity="warning",
                            category="logic",
                            title="Concrete arithmetic finding",
                            description="The arithmetic behavior is source anchored.",
                            start_line=1,
                            end_line=1,
                            evidence="result = value + 1",
                            failure_type="Incorrect result",
                        )
                    ]
                }
            )
            filtered = filter_source_proven_false_issues(db, task, result)

        self.assertEqual(len(filtered.issues), 1)
        self.assertEqual(filtered.issues[0].start_line, 2)

    def test_filter_surfaces_parser_diagnostics_without_an_llm_claim(self) -> None:
        file_id = self.create_indexed_project(
            b"def target(value):\n    return value\n",
            "parser-proof-project",
        )
        with main.connect_db() as db:
            db.execute(
                """
                UPDATE project_files
                SET parser_status = 'syntax_error',
                    parser_diagnostics_json = ?
                WHERE id = ?
                """,
                (
                    json.dumps(
                        [
                            {
                                "kind": "error",
                                "message": "Synthetic parser failure",
                                "start_line": 2,
                                "end_line": 2,
                            }
                        ]
                    ),
                    file_id,
                ),
            )
            symbol = db.execute(
                "SELECT id FROM project_symbols WHERE project_id = ? AND qualified_name = ?",
                ("parser-proof-project", "target"),
            ).fetchone()
            task = load_function_analysis_task(db, int(symbol["id"]))
            filtered = filter_source_proven_false_issues(db, task, valid_result())

        self.assertFalse(filtered.syntax_valid)
        self.assertEqual(len(filtered.issues), 1)
        self.assertEqual(filtered.issues[0].title, "Parser syntax error")
        self.assertEqual(filtered.issues[0].provenance, "deterministic")

    def test_filter_rejects_model_syntax_claim_when_the_parser_accepts_the_file(self) -> None:
        self.create_indexed_project(
            b"def build_prompt(value):\n    return helper(value)\n",
            "parser-contradiction-project",
        )
        with main.connect_db() as db:
            symbol = db.execute(
                """
                SELECT id FROM project_symbols
                WHERE project_id = 'parser-contradiction-project'
                  AND qualified_name = 'build_prompt'
                """
            ).fetchone()
            task = load_function_analysis_task(db, int(symbol["id"]))
            result = valid_result().model_copy(
                update={
                    "issues": [
                        proven_issue(
                            severity="warning",
                            category="maintainability",
                            title="Missing closing parenthesis",
                            description=(
                                "The helper call has invalid syntax because its "
                                "closing parenthesis is missing."
                            ),
                            start_line=2,
                            end_line=2,
                            evidence="return helper(value)",
                            failure_type="SyntaxError",
                        )
                    ]
                }
            )
            filtered = filter_source_proven_false_issues(db, task, result)

        self.assertTrue(filtered.syntax_valid)
        self.assertEqual(filtered.issues, [])

    def test_filter_rejects_only_variable_flow_claims_contradicted_by_static_scope(self) -> None:
        self.create_indexed_project(
            (
                b"def safe_backup(source):\n"
                b"    destination = connect()\n"
                b"    try:\n"
                b"        source.backup(destination)\n"
                b"    except Exception:\n"
                b"        destination.close()\n"
                b"        raise\n\n"
                b"def unsafe_backup(source, enabled):\n"
                b"    if enabled:\n"
                b"        destination = connect()\n"
                b"    destination.close()\n"
            ),
            "flow-contradiction-project",
        )
        filtered = {}
        with main.connect_db() as db:
            for qualified_name, line in (("safe_backup", 6), ("unsafe_backup", 12)):
                symbol = db.execute(
                    """
                    SELECT id FROM project_symbols
                    WHERE project_id = 'flow-contradiction-project'
                      AND qualified_name = ?
                    """,
                    (qualified_name,),
                ).fetchone()
                task = load_function_analysis_task(db, int(symbol["id"]))
                result = valid_result().model_copy(
                    update={
                        "issues": [
                            proven_issue(
                                severity="warning",
                                category="logic",
                                title="`destination` referenced before assignment",
                                description=(
                                    "The local variable `destination` is referenced "
                                    "before assignment on this path."
                                ),
                                start_line=line,
                                end_line=line,
                                evidence="destination.close()",
                                failure_type="UnboundLocalError",
                            )
                        ]
                    }
                )
                filtered[qualified_name] = filter_source_proven_false_issues(
                    db,
                    task,
                    result,
                )

        self.assertEqual(filtered["safe_backup"].issues, [])
        self.assertEqual(len(filtered["unsafe_backup"].issues), 1)

    def test_filter_rejects_findings_contradicted_by_python_ast_guarantees(self) -> None:
        source = (
            b"def clean(values: object) -> list[str]:\n"
            b"    output: list[str] = []\n"
            b"    strings = [item for item in values if isinstance(item, str)]\n"
            b"    for item in strings:\n"
            b"        output.append(item.strip())\n"
            b"    return output\n\n"
            b"def validate(value: str) -> str:\n"
            b"    if not value:\n"
            b"        raise ValueError('value is required')\n"
            b"    return value\n\n"
            b"def window(items: list[str], start: int, end: int) -> list[str]:\n"
            b"    return items[start:end]\n\n"
            b"def remove(path) -> None:\n"
            b"    path.unlink(missing_ok=True)\n\n"
            b"def consume(name: str) -> str:\n"
            b"    return name.strip()\n"
            b"\n"
            b"def decode(raw: str):\n"
            b"    try:\n"
            b"        return json.loads(raw)\n"
            b"    except (TypeError, ValueError, json.JSONDecodeError):\n"
            b"        return None\n"
            b"\n"
            b"def latest(events: list[str]):\n"
            b"    return not events or events[-1]\n"
            b"\n"
            b"def second(events: list[str]):\n"
            b"    return not events or events[1]\n"
        )
        self.create_indexed_project(source, "ast-counterevidence-project")
        claims = {
            "clean": proven_issue(
                severity="warning",
                category="type",
                title="Non-string item may fail",
                description="`item` may be non-str when strip is called, causing TypeError.",
                start_line=5,
                end_line=5,
                evidence="item.strip()",
                failure_type="TypeError",
            ),
            "validate": proven_issue(
                severity="warning",
                category="runtime",
                title="ValueError may escape",
                description="ValueError is raised here and may terminate the caller.",
                start_line=10,
                end_line=10,
                evidence="raise ValueError('value is required')",
                failure_type="ValueError",
            ),
            "window": proven_issue(
                severity="error",
                category="runtime",
                title="Slice can be out of range",
                description="The out-of-range indices can cause IndexError.",
                start_line=14,
                end_line=14,
                evidence="items[start:end]",
                failure_type="IndexError",
            ),
            "remove": proven_issue(
                severity="warning",
                category="runtime",
                title="Missing file can fail",
                description="A missing file causes FileNotFoundError.",
                start_line=17,
                end_line=17,
                evidence="path.unlink(missing_ok=True)",
                failure_type="FileNotFoundError",
            ),
            "consume": proven_issue(
                severity="warning",
                category="type",
                title="Caller may pass the wrong type",
                description="A caller may pass a non-str `name`, causing TypeError.",
                start_line=20,
                end_line=20,
                evidence="name.strip()",
                failure_type="TypeError",
            ),
            "decode": proven_issue(
                severity="warning",
                category="runtime",
                title="Malformed JSON can fail",
                description="json.loads can raise ValueError for malformed input.",
                start_line=24,
                end_line=24,
                evidence="return json.loads(raw)",
                failure_type="ValueError",
            ),
            "latest": proven_issue(
                severity="warning",
                category="runtime",
                title="Empty list index",
                description="events[-1] can raise IndexError when events is empty.",
                start_line=29,
                end_line=29,
                evidence="return not events or events[-1]",
                failure_type="IndexError",
            ),
            "second": proven_issue(
                severity="unsafe",
                category="runtime",
                title="Short list index",
                description="events[1] can raise IndexError when events has one item.",
                start_line=32,
                end_line=32,
                evidence="return not events or events[1]",
                failure_type="IndexError",
            ),
        }
        filtered: dict[str, FunctionAnalysisResult] = {}
        with main.connect_db() as db:
            for qualified_name, issue in claims.items():
                symbol = db.execute(
                    "SELECT id FROM project_symbols WHERE project_id = ? AND qualified_name = ?",
                    ("ast-counterevidence-project", qualified_name),
                ).fetchone()
                task = load_function_analysis_task(db, int(symbol["id"]))
                deterministic = deterministic_python_contract(task, valid_result())
                result = deterministic.model_copy(update={"issues": [issue]})
                filtered[qualified_name] = filter_source_proven_false_issues(
                    db,
                    task,
                    result,
                )

        self.assertEqual(
            [
                name
                for name, result in filtered.items()
                if name != "second" and result.issues
            ],
            [],
        )
        self.assertEqual(len(filtered["second"].issues), 1)
        self.assertIn("ValueError", filtered["validate"].raised_errors)

    def test_deterministic_broad_exception_warning_skips_intentional_boundaries(self) -> None:
        cases = {
            "reraising": (
                "def reraising():\n"
                "    try:\n"
                "        work()\n"
                "    except Exception:\n"
                "        cleanup()\n"
                "        raise\n",
                False,
            ),
            "event_worker": (
                "def event_worker():\n"
                "    try:\n"
                "        work()\n"
                "    except Exception:\n"
                "        LOGGER.exception('worker failed')\n"
                "        return\n",
                False,
            ),
            "per_item": (
                "def per_item(items):\n"
                "    for item in items:\n"
                "        try:\n"
                "            consume(item)\n"
                "        except Exception:\n"
                "            continue\n",
                False,
            ),
            "ast_fallback": (
                "def ast_fallback(node):\n"
                "    try:\n"
                "        return ast.unparse(node)\n"
                "    except Exception:\n"
                "        return None\n",
                False,
            ),
            "swallowing": (
                "def swallowing(payload):\n"
                "    try:\n"
                "        return int(payload['count'])\n"
                "    except Exception:\n"
                "        return 0\n",
                True,
            ),
        }
        for qualified_name, (source, expected) in cases.items():
            with self.subTest(qualified_name=qualified_name):
                task = project_function_analysis.FunctionAnalysisTask(
                    symbol_id=1,
                    project_id="p",
                    user_id=1,
                    file_id=1,
                    file_path="boundaries.py",
                    language="python",
                    symbol_kind="function",
                    qualified_name=qualified_name,
                    start_line=1,
                    end_line=source.count("\n"),
                    source_sha256="0" * 64,
                    function_sha256="1" * 64,
                    source=source,
                    analysis_context=(
                        "def work(): ...\n"
                        "def cleanup(): ...\n"
                        "def consume(value): ...\n"
                        "LOGGER = object()"
                    ),
                )
                result = deterministic_python_contract(task, valid_result())
                has_warning = any(
                    issue.title == "Broad exception handler"
                    for issue in result.issues
                )
                self.assertEqual(has_warning, expected)

    def test_filter_deduplicates_model_and_deterministic_overlaps(self) -> None:
        self.create_indexed_project(
            (
                b"def target(enabled: bool) -> int:\n"
                b"    marker = account_id\n"
                b"    if enabled:\n"
                b"        return 1\n"
                b"    return 'disabled'\n"
            ),
            "dedupe-overlap-project",
        )
        with main.connect_db() as db:
            symbol = db.execute(
                """
                SELECT id FROM project_symbols
                WHERE project_id = 'dedupe-overlap-project'
                  AND qualified_name = 'target'
                """
            ).fetchone()
            task = load_function_analysis_task(db, int(symbol["id"]))
            model_result = valid_result().model_copy(
                update={
                    "issues": [
                        FunctionIssue(
                            severity="info",
                            category="maintainability",
                            title=(
                                "The variable `account_id` is referenced but never defined "
                                "within the scope of the function, leading to a NameError at runtime."
                            ),
                            description=(
                                "`account_id` is used without prior definition; it may have "
                                "been intended as a global variable or parameter but is missing."
                            ),
                        ),
                        FunctionIssue(
                            severity="error",
                            category="maintainability",
                            title="Return type contradiction",
                            description=(
                                "The function returns a string on one path, which "
                                "contradicts the declared return type of int."
                            ),
                        ),
                        proven_issue(
                            severity="warning",
                            category="logic",
                            title="Distinct model warning",
                            description="This non-overlapping issue should remain.",
                            start_line=3,
                            end_line=3,
                            evidence="if enabled:",
                        ),
                    ]
                }
            )
            deterministic = deterministic_python_contract(task, model_result)
            filtered = filter_source_proven_false_issues(db, task, deterministic)

        by_title = {issue.title: issue for issue in filtered.issues}
        self.assertNotIn("account_id is not defined", by_title)
        self.assertNotIn("Return type contradiction", by_title)
        self.assertEqual(
            by_title["Possibly undefined variable"].provenance,
            "deterministic",
        )
        self.assertEqual(by_title["Possibly undefined variable"].start_line, 2)
        self.assertEqual(
            by_title["Return type does not match annotation"].provenance,
            "deterministic",
        )
        self.assertEqual(
            by_title["Return type does not match annotation"].start_line,
            5,
        )
        self.assertIn("Distinct model warning", by_title)

    def test_filter_deduplicates_generic_none_dereference_model_overlap(self) -> None:
        self.create_indexed_project(
            b"def target(row: sqlite3.Row | None) -> str:\n    return row.username\n",
            "dedupe-none-overlap-project",
        )
        with main.connect_db() as db:
            symbol = db.execute(
                """
                SELECT id FROM project_symbols
                WHERE project_id = 'dedupe-none-overlap-project'
                  AND qualified_name = 'target'
                """
            ).fetchone()
            task = load_function_analysis_task(db, int(symbol["id"]))
            model_result = valid_result().model_copy(
                update={
                    "issues": [
                        FunctionIssue(
                            severity="info",
                            category="maintainability",
                            title="Potential dereference of None leading to AttributeError",
                            description="Potential dereference of None leading to AttributeError.",
                        )
                    ]
                }
            )
            deterministic = deterministic_python_contract(task, model_result)
            filtered = filter_source_proven_false_issues(db, task, deterministic)

        titles = [issue.title for issue in filtered.issues]
        self.assertNotIn("Potential dereference of None leading to AttributeError", titles)
        self.assertIn("Possible None dereference", titles)
        self.assertIn("Invalid sqlite3.Row attribute access", titles)

    def test_filter_removes_type_proven_tuple_and_timeout_model_noise(self) -> None:
        content = (
            b"NtfyNotification = tuple[str, str, str]\n\n"
            b"def deliver_ntfy_notification(notification: NtfyNotification) -> None:\n"
            b"    title, message, priority = notification\n"
            b"    with urllib.request.urlopen(request, timeout=NTFY_TIMEOUT_SECONDS) as response:\n"
            b"        response.read(1)\n"
        )
        self.create_indexed_project(content, "ntfy-noise-project")
        with main.connect_db() as db:
            symbol = db.execute(
                """
                SELECT id FROM project_symbols
                WHERE project_id = 'ntfy-noise-project'
                  AND qualified_name = 'deliver_ntfy_notification'
                """
            ).fetchone()
            task = load_function_analysis_task(db, int(symbol["id"]))
            result = valid_result().model_copy(
                update={
                    "parameters": [
                        FunctionParameterContract(
                            name="notification",
                            kind="positional_or_keyword",
                            required=True,
                            accepted_types=["NtfyNotification"],
                            description="Notification tuple.",
                        )
                    ],
                    "issues": [
                        FunctionIssue(
                            severity="warning",
                            category="maintainability",
                            title="Unpacking risk",
                            description="The function assumes that 'notification' can be directly unpacked into three values.",
                        ),
                        FunctionIssue(
                            severity="info",
                            category="maintainability",
                            title="Priority header value may need conversion to string",
                            description="Priority header value may need conversion to string; if it is an int, implicit conversion occurs.",
                        ),
                        FunctionIssue(
                            severity="warning",
                            category="maintainability",
                            title="No timeout handling beyond the provided constant",
                            description="No timeout handling beyond the provided constant; network delays longer than NTFY_TIMEOUT_SECONDS will cause a timeout exception.",
                        ),
                        proven_issue(
                            severity="warning",
                            category="logic",
                            title="Real warning",
                            description="This warning should remain.",
                            start_line=4,
                            end_line=4,
                            evidence="notification",
                        ),
                    ],
                }
            )
            filtered = filter_source_proven_false_issues(db, task, result)

        self.assertEqual([issue.title for issue in filtered.issues], ["Real warning"])

    def test_filter_trusts_annotations_class_fields_and_selected_database_columns(self) -> None:
        self.create_indexed_project(
            (
                b"from dataclasses import dataclass\n\n"
                b"@dataclass\n"
                b"class Profile:\n"
                b"    name: str\n\n"
                b"def render(profile: Profile, name: str) -> str:\n"
                b"    return profile.name + name.upper()\n\n"
                b"def selected_name(db) -> str:\n"
                b"    row = db.execute('SELECT id, display_name AS name FROM users').fetchone()\n"
                b"    return row['name']\n\n"
                b"def reassigned(name: str, replacement) -> str:\n"
                b"    name = replacement\n"
                b"    return name.upper()\n"
            ),
            "trusted-contract-project",
        )
        with main.connect_db() as db:
            tasks = {}
            for qualified_name in ("render", "selected_name", "reassigned"):
                symbol = db.execute(
                    """
                    SELECT id FROM project_symbols
                    WHERE project_id = 'trusted-contract-project'
                      AND qualified_name = ?
                    """,
                    (qualified_name,),
                ).fetchone()
                tasks[qualified_name] = load_function_analysis_task(db, int(symbol["id"]))

            render_model = valid_result().model_copy(
                update={
                    "issues": [
                        proven_issue(
                            severity="error",
                            category="runtime",
                            title="Name may be None",
                            description="Parameter `name` may be None before upper is called.",
                            start_line=8,
                            end_line=8,
                            evidence="name.upper()",
                            failure_type="AttributeError",
                        ),
                        proven_issue(
                            severity="error",
                            category="runtime",
                            title="Profile field may be absent",
                            description="Parameter `profile` may have no attribute `name`.",
                            start_line=8,
                            end_line=8,
                            evidence="profile.name",
                            failure_type="AttributeError",
                        ),
                    ]
                }
            )
            render_result = deterministic_python_contract(tasks["render"], render_model)
            render_filtered = filter_source_proven_false_issues(
                db, tasks["render"], render_result
            )

            selected_model = valid_result().model_copy(
                update={
                    "issues": [
                        proven_issue(
                            severity="error",
                            category="runtime",
                            title="Selected key may be missing",
                            description="row['name'] may raise KeyError if the column is missing.",
                            start_line=12,
                            end_line=12,
                            evidence="row['name']",
                            failure_type="KeyError",
                        )
                    ]
                }
            )
            selected_result = deterministic_python_contract(
                tasks["selected_name"], selected_model
            )
            selected_filtered = filter_source_proven_false_issues(
                db, tasks["selected_name"], selected_result
            )

            reassigned_model = valid_result().model_copy(
                update={
                    "issues": [
                        proven_issue(
                            severity="error",
                            category="runtime",
                            title="Reassigned value may be None",
                            description="Reassigned `name` may be None before upper is called.",
                            start_line=16,
                            end_line=16,
                            evidence="name.upper()",
                            failure_type="AttributeError",
                        )
                    ]
                }
            )
            reassigned_result = deterministic_python_contract(
                tasks["reassigned"], reassigned_model
            )
            reassigned_filtered = filter_source_proven_false_issues(
                db, tasks["reassigned"], reassigned_result
            )

        self.assertEqual(render_filtered.issues, [])
        self.assertEqual(selected_filtered.issues, [])
        self.assertEqual(len(reassigned_filtered.issues), 1)
        self.assertEqual(reassigned_filtered.issues[0].severity, "unsafe")

    def test_filter_removes_provably_false_exception_and_float_return_noise(self) -> None:
        self.create_indexed_project(
            (
                b"def emit(record):\n"
                b"    try:\n"
                b"        record.getMessage()\n"
                b"    except Exception:\n"
                b"        return None\n\n"
                b"def seconds_until_periodic_backup() -> float:\n"
                b"    return 0\n"
            ),
            "proven-noise-project",
        )
        with main.connect_db() as db:
            emit_symbol = db.execute(
                """
                SELECT id FROM project_symbols
                WHERE project_id = 'proven-noise-project'
                  AND qualified_name = 'emit'
                """
            ).fetchone()
            emit_task = load_function_analysis_task(db, int(emit_symbol["id"]))
            emit_result = valid_result().model_copy(
                update={
                    "issues": [
                        FunctionIssue(
                            severity="error",
                            category="logic",
                            title="Generic except Exception swallows critical exceptions",
                            description=(
                                "Catching all Exceptions includes KeyboardInterrupt and "
                                "SystemExit, masking critical failures."
                            ),
                        ),
                        proven_issue(
                            severity="warning",
                            category="maintainability",
                            title="Broad exception handler",
                            description="This warning should remain.",
                            start_line=4,
                            end_line=4,
                            evidence="except Exception:",
                        ),
                    ]
                }
            )
            filtered_emit = filter_source_proven_false_issues(db, emit_task, emit_result)

            seconds_symbol = db.execute(
                """
                SELECT id FROM project_symbols
                WHERE project_id = 'proven-noise-project'
                  AND qualified_name = 'seconds_until_periodic_backup'
                """
            ).fetchone()
            seconds_task = load_function_analysis_task(db, int(seconds_symbol["id"]))
            seconds_result = valid_result().model_copy(
                update={
                    "issues": [
                        FunctionIssue(
                            severity="error",
                            category="type",
                            title="Return type does not match annotation",
                            description=(
                                "Annotated return type is `float` but this path returns `int`."
                            ),
                            start_line=8,
                            end_line=8,
                        ),
                        proven_issue(
                            severity="warning",
                            category="logic",
                            title="Real warning",
                            description="This warning should remain.",
                            start_line=8,
                            end_line=8,
                            evidence="return 0",
                        ),
                    ]
                }
            )
            filtered_seconds = filter_source_proven_false_issues(
                db, seconds_task, seconds_result
            )

        self.assertEqual(
            [issue.title for issue in filtered_emit.issues],
            ["Broad exception handler"],
        )
        self.assertEqual(
            [issue.title for issue in filtered_seconds.issues],
            ["Real warning"],
        )

    def test_filter_removes_generic_database_oserror_and_cursor_shape_noise(self) -> None:
        self.create_indexed_project(
            (
                b"def decode_chat_list_cursor(value: str) -> tuple[str, str, str]:\n"
                b"    return 'updated', 'created', value\n\n"
                b"def list_chats(cursor):\n"
                b"    boundary = decode_chat_list_cursor(cursor)\n"
                b"    updated_at, created_at, chat_id = boundary\n"
                b"    return updated_at, created_at, chat_id\n\n"
                b"def admin_system_health(backups, counts):\n"
                b"    try:\n"
                b"        latest_backup_at = int(backups[0].stat().st_mtime)\n"
                b"    except OSError:\n"
                b"        latest_backup_at = None\n"
                b"    return counts['queued_jobs'], latest_backup_at\n\n"
                b"def retry_chat_verification(db):\n"
                b"    return db.execute('SELECT 1').fetchone()\n"
                b"\n"
                b"def login(user):\n"
                b"    if user is None:\n"
                b"        raise ValueError('bad credentials')\n"
                b"    return user[\"password_hash\"]\n"
                b"\n"
                b"def stream_account_export(db):\n"
                b"    while True:\n"
                b"        rows = db.execute('SELECT id FROM users LIMIT 50').fetchall()\n"
                b"        if not rows:\n"
                b"            break\n"
                b"        yield rows\n"
                b"\n"
                b"def list_users(row, throttle):\n"
                b"    return str(row[\"scope_hash\"]), throttle[\"failed_attempts\"]\n"
                b"\n"
                b"def export_admin_audit_events():\n"
                b"    actor = None\n"
                b"    where_sql = ''\n"
                b"    parameters = []\n"
                b"    return actor, where_sql, parameters\n"
                b"\n"
                b"def clear_admin_announcement(previous):\n"
                b"    return previous['id']\n"
            ),
            "generic-noise-project",
        )
        with main.connect_db() as db:
            list_symbol = db.execute(
                """
                SELECT id FROM project_symbols
                WHERE project_id = 'generic-noise-project'
                  AND qualified_name = 'list_chats'
                """
            ).fetchone()
            list_task = load_function_analysis_task(db, int(list_symbol["id"]))
            list_result = valid_result().model_copy(
                update={
                    "issues": [
                        FunctionIssue(
                            severity="error",
                            category="runtime",
                            title="ValueError due to unexpected cursor shape",
                            description=(
                                "Unpacking `boundary` into three variables expects it to be "
                                "a tuple/list of length 3. If `decode_chat_list_cursor(cursor)` "
                                "returns a different shape, ValueError arises."
                            ),
                        ),
                        proven_issue(
                            severity="warning",
                            category="logic",
                            title="Real warning",
                            description="This warning should remain.",
                            start_line=7,
                            end_line=7,
                            evidence="return updated_at",
                        ),
                    ]
                }
            )
            filtered_list = filter_source_proven_false_issues(db, list_task, list_result)

            health_symbol = db.execute(
                """
                SELECT id FROM project_symbols
                WHERE project_id = 'generic-noise-project'
                  AND qualified_name = 'admin_system_health'
                """
            ).fetchone()
            health_task = load_function_analysis_task(db, int(health_symbol["id"]))
            health_result = valid_result().model_copy(
                update={
                    "issues": [
                        FunctionIssue(
                            severity="error",
                            category="resource",
                            title="backups[0].stat().st_mtime error handling",
                            description=(
                                "backups[0].stat().st_mtime may raise OSError; "
                                "caught but then latest_backup_at set to None."
                            ),
                        ),
                        FunctionIssue(
                            severity="error",
                            category="maintainability",
                            title="counts dictionary access by string keys",
                            description=(
                                "Counts dictionary values accessed via string keys; "
                                "if DB schema changes or queries fail, KeyError may occur."
                            ),
                        ),
                    ]
                }
            )
            filtered_health = filter_source_proven_false_issues(
                db, health_task, health_result
            )

            retry_symbol = db.execute(
                """
                SELECT id FROM project_symbols
                WHERE project_id = 'generic-noise-project'
                  AND qualified_name = 'retry_chat_verification'
                """
            ).fetchone()
            retry_task = load_function_analysis_task(db, int(retry_symbol["id"]))
            retry_result = valid_result().model_copy(
                update={
                    "issues": [
                        FunctionIssue(
                            severity="error",
                            category="runtime",
                            title="Missing Exception Handling for DB Operations",
                            description=(
                                "All database operations assume success; any SQLite errors "
                                "would propagate uncaught, potentially exposing internal details."
                            ),
                        )
                    ]
                }
            )
            filtered_retry = filter_source_proven_false_issues(
                db, retry_task, retry_result
            )

            noise_cases = {
                "login": FunctionIssue(
                    severity="error",
                    category="maintainability",
                    title="Potential KeyError if 'user' is None",
                    description=(
                        "'user' may be None after the database query; subsequent "
                        'accesses like user["password_hash"] will raise a KeyError '
                        "unless handled explicitly."
                    ),
                ),
                "stream_account_export": FunctionIssue(
                    severity="error",
                    category="logic",
                    title="Unbounded Loop Risk",
                    description=(
                        "While loops rely on breaking when queries return empty results. "
                        "If data corruption causes infinite non-empty result sets, the "
                        "function could run indefinitely."
                    ),
                    start_line=23,
                    end_line=23,
                ),
                "list_users": FunctionIssue(
                    severity="error",
                    category="runtime",
                    title=(
                        'Key lookup using str(row["scope_hash"]) may raise KeyError '
                        "if row lacks scope_hash key; however SELECT ensures it exists."
                    ),
                    description=(
                        'Key lookup using str(row["scope_hash"]) may raise KeyError '
                        "if row lacks scope_hash key; however SELECT ensures it exists."
                    ),
                    start_line=30,
                    end_line=30,
                ),
                "list_users_missing_columns": FunctionIssue(
                    severity="error",
                    category="runtime",
                    title=(
                        "Accessing throttle['failed_attempts'] and throttle['locked_until'] "
                        "assumes these keys exist; missing columns would cause KeyError."
                    ),
                    description=(
                        "Accessing throttle['failed_attempts'] and throttle['locked_until'] "
                        "assumes these keys exist; missing columns would cause KeyError."
                    ),
                    start_line=30,
                    end_line=30,
                ),
                "export_admin_audit_events": FunctionIssue(
                    severity="error",
                    category="maintainability",
                    title=(
                        "Missing type annotations for local variables such as actor, "
                        "where_sql, parameters."
                    ),
                    description=(
                        "Missing type annotations for local variables such as actor, "
                        "where_sql, parameters."
                    ),
                ),
                "clear_admin_announcement": FunctionIssue(
                    severity="error",
                    category="runtime",
                    title="Potential KeyError on accessing previous['id']",
                    description=(
                        "Accesses previous['id'] without confirming presence of 'id', "
                        "risking KeyError if schema changes."
                    ),
                    start_line=38,
                    end_line=38,
                ),
            }
            filtered_noise = {}
            for case_name, issue in noise_cases.items():
                qualified_name = case_name.removesuffix("_missing_columns")
                symbol = db.execute(
                    """
                    SELECT id FROM project_symbols
                    WHERE project_id = 'generic-noise-project'
                      AND qualified_name = ?
                    """,
                    (qualified_name,),
                ).fetchone()
                task = load_function_analysis_task(db, int(symbol["id"]))
                filtered_noise[qualified_name] = filter_source_proven_false_issues(
                    db,
                    task,
                    valid_result().model_copy(update={"issues": [issue]}),
                )

        self.assertEqual(
            [issue.title for issue in filtered_list.issues],
            ["Real warning"],
        )
        self.assertEqual(filtered_health.issues, [])
        self.assertEqual(filtered_retry.issues, [])
        self.assertTrue(
            all(not result.issues for result in filtered_noise.values()),
            filtered_noise,
        )

    def test_indented_class_method_fragment_is_not_marked_unexpected_indent(self) -> None:
        self.create_indexed_project(
            (
                b"class Demo:\n"
                b"    @staticmethod\n"
                b"    def helper(value: int) -> int:\n"
                b"        return value\n"
            ),
            "indented-method-project",
        )

        summary = analyze_project_functions(
            main.connect_db,
            "indented-method-project",
            analysis_request=lambda **_kwargs: valid_result(),
        )

        self.assertEqual(summary.status, "completed")
        with main.connect_db() as db:
            issues = db.execute(
                """
                SELECT issue.title, issue.description
                FROM project_symbol_issues AS issue
                JOIN project_symbols AS symbol ON symbol.id = issue.symbol_id
                WHERE symbol.project_id = 'indented-method-project'
                """
            ).fetchall()

        issue_text = "\n".join(f"{row['title']} {row['description']}" for row in issues)
        self.assertNotIn("unexpected indent", issue_text.lower())
        self.assertNotIn("Python syntax error", issue_text)

    def test_chunking_migration_makes_previously_skipped_function_resumable(self) -> None:
        self.create_indexed_project(
            b"def formerly_large(value):\n    return value\n",
            "legacy-skipped-project",
        )
        with main.connect_db() as db:
            db.execute(
                """
                UPDATE project_symbols
                SET analysis_status = 'skipped', analysis_error = 'Function was too large'
                WHERE project_id = 'legacy-skipped-project' AND symbol_kind = 'function'
                """
            )
            db.execute(
                """
                UPDATE projects
                SET function_analysis_status = 'failed',
                    function_analysis_skipped_count = 1
                WHERE id = 'legacy-skipped-project'
                """
            )
            migration_025_oversized_function_chunking(db)
            symbol = db.execute(
                "SELECT analysis_status, analysis_error FROM project_symbols "
                "WHERE project_id = 'legacy-skipped-project' AND symbol_kind = 'function'"
            ).fetchone()
            project = db.execute(
                "SELECT function_analysis_status, function_analysis_skipped_count "
                "FROM projects WHERE id = 'legacy-skipped-project'"
            ).fetchone()
        self.assertEqual(tuple(symbol), ("pending", None))
        self.assertEqual(tuple(project), ("pending", 0))

    @patch.object(project_function_analysis, "FUNCTION_ANALYSIS_ADAPTIVE_OUTPUT", False)
    def test_runner_batches_eight_model_bound_functions_into_one_call(self) -> None:
        content = b"GLOBAL_VALUE = 3\n\n" + b"\n\n".join(
            (
                f"def function_{index}(value, transform):\n"
                f"    return transform(value, GLOBAL_VALUE)\n"
            ).encode("utf-8")
            for index in range(1, 9)
        )
        self.create_indexed_project(content, "batch-runner-project")
        batch_calls: list[list[dict[str, object]]] = []
        single_calls: list[str] = []
        progress: list[tuple[object, ...]] = []

        def analyze_batch(**kwargs):
            functions = kwargs["functions"]
            batch_calls.append(functions)
            return {
                str(function["request_id"]): valid_result()
                for function in functions
            }

        summary = analyze_project_functions(
            main.connect_db,
            "batch-runner-project",
            analysis_request=lambda **kwargs: (
                single_calls.append(kwargs["qualified_name"]) or valid_result()
            ),
            batch_analysis_request=analyze_batch,
            progress_callback=lambda *values: progress.append(values),
        )

        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.completed_count, 8)
        self.assertEqual(single_calls, [])
        self.assertEqual(len(batch_calls), 1)
        self.assertEqual(len(batch_calls[0]), 8)
        self.assertEqual(summary.model_request_count, 1)
        self.assertEqual(summary.batch_request_count, 1)
        self.assertEqual(summary.deterministic_count, 0)
        self.assertTrue(
            all(function["analysis_context"] for function in batch_calls[0])
        )
        self.assertEqual(
            [event[0] for event in progress].count("analyzing_function_batch"),
            8,
        )

    def test_runner_falls_back_to_individual_calls_when_batch_is_invalid(self) -> None:
        content = (
            b"def first(value, transform):\n"
            b"    return transform(value)\n\n"
            b"def second(value, transform):\n"
            b"    return transform(value)\n"
        )
        self.create_indexed_project(content, "batch-fallback-project")
        batch_calls: list[bool] = []
        single_calls: list[str] = []

        def invalid_batch(**_kwargs):
            batch_calls.append(True)
            raise ValueError("malformed batch payload")

        summary = analyze_project_functions(
            main.connect_db,
            "batch-fallback-project",
            analysis_request=lambda **kwargs: (
                single_calls.append(kwargs["qualified_name"]) or valid_result()
            ),
            batch_analysis_request=invalid_batch,
        )

        self.assertEqual(batch_calls, [True])
        self.assertEqual(single_calls, ["first", "second"])
        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.model_request_count, 3)
        self.assertEqual(summary.batch_request_count, 1)
        self.assertEqual(summary.batch_fallback_count, 1)
        self.assertIn("malformed batch payload", summary.batch_error)

    @patch.object(project_function_analysis, "FUNCTION_ANALYSIS_ADAPTIVE_OUTPUT", False)
    def test_runner_does_not_bisect_structurally_invalid_batches(self) -> None:
        content = b"\n\n".join(
            (
                f"def function_{index}(value, transform):\n"
                f"    return transform(value)\n"
            ).encode("utf-8")
            for index in range(1, 9)
        )
        self.create_indexed_project(content, "batch-format-project")
        batch_sizes: list[int] = []
        single_calls: list[str] = []

        def invalid_batch(**kwargs):
            batch_sizes.append(len(kwargs["functions"]))
            raise analysis_engine.FunctionAnalysisBatchFormatError("wrong response shape")

        summary = analyze_project_functions(
            main.connect_db,
            "batch-format-project",
            analysis_request=lambda **kwargs: (
                single_calls.append(kwargs["qualified_name"]) or valid_result()
            ),
            batch_analysis_request=invalid_batch,
        )

        self.assertEqual(batch_sizes, [8])
        self.assertEqual(len(single_calls), 8)
        self.assertEqual(summary.model_request_count, 9)
        self.assertEqual(summary.batch_fallback_count, 1)

    @patch.object(project_function_analysis, "FUNCTION_ANALYSIS_ADAPTIVE_OUTPUT", False)
    def test_runner_bisects_large_failed_batches_and_keeps_valid_subgroups(self) -> None:
        content = b"\n\n".join(
            (
                f"def function_{index}(value, transform):\n"
                f"    return transform(value)\n"
            ).encode("utf-8")
            for index in range(1, 9)
        )
        self.create_indexed_project(content, "adaptive-batch-project")
        batch_sizes: list[int] = []
        single_calls: list[str] = []

        def size_limited_batch(**kwargs):
            functions = kwargs["functions"]
            batch_sizes.append(len(functions))
            if len(functions) > 2:
                raise ValueError("batch is too large for this model")
            return {
                str(function["request_id"]): valid_result()
                for function in functions
            }

        summary = analyze_project_functions(
            main.connect_db,
            "adaptive-batch-project",
            analysis_request=lambda **kwargs: (
                single_calls.append(kwargs["qualified_name"]) or valid_result()
            ),
            batch_analysis_request=size_limited_batch,
        )

        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.completed_count, 8)
        self.assertEqual(single_calls, [])
        self.assertEqual(batch_sizes, [8, 4, 2, 2, 4, 2, 2])
        self.assertEqual(summary.model_request_count, 7)
        self.assertEqual(summary.batch_request_count, 7)
        self.assertEqual(summary.batch_fallback_count, 3)
        self.assertIn("batch is too large", summary.batch_error)

    def test_backend_failure_preserves_completed_work_and_resumes_pending_functions(self):
        self.create_indexed_project(
            b"def first(value):\n    return value\n\n"
            b"def second(value):\n    return value + 1\n\n"
            b"def third(value):\n    return value + 2\n",
        )
        calls = []
        def review(**kwargs):
            calls.append(kwargs["qualified_name"])
            if len(calls) == 2:
                raise analysis_engine.OllamaUnavailableError("Backend could not start")
            return valid_result()
        with self.assertRaises(analysis_engine.OllamaUnavailableError):
            analyze_project_functions(main.connect_db, "analysis-project", analysis_request=review)
        self.assertEqual(calls, ["first", "second"])
        with main.connect_db() as db:
            states = db.execute("SELECT analysis_status FROM project_symbols WHERE symbol_kind='function' ORDER BY start_byte").fetchall()
            project = db.execute("SELECT function_analysis_status,function_analysis_completed_count,function_analysis_failed_count,function_analysis_error FROM projects WHERE id='analysis-project'").fetchone()
        self.assertEqual([r[0] for r in states], ["completed", "pending", "pending"])
        self.assertEqual(tuple(project)[:3], ("failed", 1, 0))
        self.assertIn("Backend could not start", project[3])
        resumed = []
        summary = analyze_project_functions(main.connect_db, "analysis-project", analysis_request=lambda **kwargs: (resumed.append(kwargs["qualified_name"]) or valid_result()))
        self.assertEqual(resumed, ["second", "third"])
        self.assertEqual(summary.completed_count, 3)

    @patch.object(project_function_analysis, "FUNCTION_ANALYSIS_ADAPTIVE_OUTPUT", False)
    def test_backend_batch_failure_does_not_split_or_retry_as_individual_functions(self):
        content = b"\n\n".join(
            f"def function_{index}(value, transform):\n    return transform(value)\n".encode()
            for index in range(8)
        )
        self.create_indexed_project(content)
        with patch.object(analysis_engine, "request_function_analysis_batch", side_effect=analysis_engine.OllamaUnavailableError("Backend could not start")) as batch:
            with patch.object(analysis_engine, "request_function_analysis") as single:
                with self.assertRaises(analysis_engine.OllamaUnavailableError):
                    analyze_project_functions(main.connect_db, "analysis-project", batch_analysis_request=batch, analysis_request=single)
        batch.assert_called_once()
        single.assert_not_called()
        with main.connect_db() as db:
            states = db.execute("SELECT DISTINCT analysis_status FROM project_symbols WHERE symbol_kind='function'").fetchall()
            metrics = db.execute("SELECT function_analysis_model_request_count,function_analysis_batch_fallback_count FROM projects WHERE id='analysis-project'").fetchone()
        self.assertEqual([r[0] for r in states], ["pending"])
        self.assertEqual(tuple(metrics), (1, 0))

    def test_backend_chunk_failure_leaves_whole_function_pending(self):
        content = b"def first(value):\n" + b"    value += 1\n" * 80 + b"    return value\n"
        self.create_indexed_project(content)
        with patch.object(project_function_analysis, "FUNCTION_ANALYSIS_CHUNK_CHARS", 250):
            with patch.object(analysis_engine, "request_function_chunk_analysis", side_effect=analysis_engine.OllamaUnavailableError("Backend could not start")) as chunk:
                with self.assertRaises(analysis_engine.OllamaUnavailableError):
                    analyze_project_functions(main.connect_db, "analysis-project", analysis_request=lambda **kwargs: valid_result(), chunk_analysis_request=chunk)
        chunk.assert_called_once()
        with main.connect_db() as db:
            state = db.execute("SELECT analysis_status FROM project_symbols WHERE symbol_kind='function'").fetchone()[0]
        self.assertEqual(state, "pending")

    def test_runner_persists_normalized_contract_and_resumes_only_failures(self) -> None:
        content = (
            b"def first(value):\n    return value\n\n"
            b"def second(value):\n    return value + 1\n"
        )
        self.create_indexed_project(content)
        initial_calls: list[str] = []
        progress: list[tuple[str, int, int, str, str]] = []

        def first_pass(**kwargs):
            initial_calls.append(kwargs["qualified_name"])
            if kwargs["qualified_name"] == "second":
                raise RuntimeError("temporary Ollama failure")
            return valid_result(issue_line=2)

        summary = analyze_project_functions(
            main.connect_db,
            "analysis-project",
            analysis_request=first_pass,
            progress_callback=lambda *values: progress.append(values),
        )
        self.assertEqual(initial_calls, ["first", "second"])
        self.assertEqual(summary.status, "partial")
        self.assertEqual((summary.completed_count, summary.failed_count), (1, 1))
        self.assertEqual(progress[0][:3], ("analyzing_function", 1, 2))

        retry_calls: list[str] = []

        def retry(**kwargs):
            retry_calls.append(kwargs["qualified_name"])
            return valid_result(issue_line=5)

        summary = analyze_project_functions(
            main.connect_db,
            "analysis-project",
            analysis_request=retry,
            retry_failed=True,
        )
        self.assertEqual(retry_calls, ["second"])
        self.assertEqual(summary.status, "completed")
        with main.connect_db() as db:
            analyses = db.execute(
                "SELECT summary, syntax_valid, confidence FROM project_symbol_analyses"
            ).fetchall()
            parameters = db.execute(
                "SELECT name, parameter_kind, accepted_types_json FROM project_symbol_parameters"
            ).fetchall()
            returns = db.execute(
                "SELECT type_name FROM project_symbol_return_types"
            ).fetchall()
            issues = db.execute(
                """
                SELECT start_line, provenance, proof, evidence, failure_type,
                       trigger, report_tier
                FROM project_symbol_issues ORDER BY symbol_id
                """
            ).fetchall()
            attempts = db.execute(
                "SELECT qualified_name, analysis_attempt_count FROM project_symbols "
                "WHERE symbol_kind = 'function' ORDER BY start_byte"
            ).fetchall()
        self.assertEqual(len(analyses), 2)
        self.assertTrue(all(row["syntax_valid"] == 1 for row in analyses))
        self.assertEqual([json.loads(row["accepted_types_json"]) for row in parameters], [["int"], ["int"]])
        self.assertEqual([row["type_name"] for row in returns], ["int", "int"])
        self.assertEqual([row["start_line"] for row in issues], [2, 5])
        self.assertEqual([row["provenance"] for row in issues], ["model", "model"])
        self.assertEqual([row["proof"] for row in issues], ["source-v1", "source-v1"])
        self.assertEqual([row["evidence"] for row in issues], ["value", "value"])
        self.assertTrue(all(row["failure_type"] for row in issues))
        self.assertTrue(all(row["trigger"] for row in issues))
        self.assertEqual([row["report_tier"] for row in issues], ["defect", "defect"])
        self.assertEqual([tuple(row) for row in attempts], [("first", 1), ("second", 2)])

    def test_runner_retains_deterministic_issue_and_reviews_unresolved_behavior(self) -> None:
        self.create_indexed_project(
            b"def fixture_undefined_variable_path(user_name: str) -> str:\n"
            b"    normalized = user_name.strip().lower()\n"
            b"    return f'{normalized}:{account_id}'\n",
            "deterministic-skip-project",
        )
        calls: list[str] = []
        progress: list[tuple[object, ...]] = []

        def additional_review(**kwargs):
            calls.append(kwargs["qualified_name"])
            return valid_result()

        summary = analyze_project_functions(
            main.connect_db,
            "deterministic-skip-project",
            analysis_request=additional_review,
            progress_callback=lambda *values: progress.append(values),
        )

        self.assertEqual(calls, ["fixture_undefined_variable_path"])
        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.model_request_count, 1)
        self.assertEqual(summary.deterministic_count, 0)
        self.assertIn("analyzing_function", [event[0] for event in progress])
        with main.connect_db() as db:
            issue = db.execute(
                """
                SELECT issue.title, issue.severity, issue.provenance
                FROM project_symbol_issues AS issue
                JOIN project_symbols AS symbol ON symbol.id = issue.symbol_id
                WHERE symbol.project_id = 'deterministic-skip-project'
                """
            ).fetchone()
        self.assertEqual(issue["title"], "Possibly undefined variable")
        self.assertEqual(issue["severity"], "error")
        self.assertEqual(issue["provenance"], "deterministic")

    def test_runner_skips_llm_for_complete_deterministic_contract(self) -> None:
        self.create_indexed_project(
            b"def add_one(value: int) -> int:\n"
            b"    result = value + 1\n"
            b"    return result\n",
            "deterministic-contract-project",
        )
        progress: list[tuple[object, ...]] = []

        summary = analyze_project_functions(
            main.connect_db,
            "deterministic-contract-project",
            analysis_request=lambda **_kwargs: self.fail(
                "complete deterministic contract should skip the LLM"
            ),
            progress_callback=lambda *values: progress.append(values),
        )

        self.assertEqual(summary.status, "completed")
        self.assertIn("using_deterministic_function", [event[0] for event in progress])
        with main.connect_db() as db:
            analysis = db.execute(
                """
                SELECT analysis.summary, analysis.confidence, analysis.may_return_value
                FROM project_symbol_analyses AS analysis
                JOIN project_symbols AS symbol ON symbol.id = analysis.symbol_id
                WHERE symbol.project_id = 'deterministic-contract-project'
                """
            ).fetchone()
            parameters = db.execute(
                """
                SELECT accepted_types_json
                FROM project_symbol_parameters AS parameter
                JOIN project_symbols AS symbol ON symbol.id = parameter.symbol_id
                WHERE symbol.project_id = 'deterministic-contract-project'
                """
            ).fetchall()
            returns = db.execute(
                """
                SELECT type_name
                FROM project_symbol_return_types AS return_type
                JOIN project_symbols AS symbol ON symbol.id = return_type.symbol_id
                WHERE symbol.project_id = 'deterministic-contract-project'
                """
            ).fetchall()
        self.assertIn("without an LLM call", analysis["summary"])
        self.assertGreaterEqual(analysis["confidence"], 0.82)
        self.assertEqual(analysis["may_return_value"], 1)
        self.assertEqual([json.loads(row["accepted_types_json"]) for row in parameters], [["int"]])
        self.assertEqual([row["type_name"] for row in returns], ["int"])

    def test_runner_skips_llm_for_typed_pure_builtin_conversion(self) -> None:
        self.create_indexed_project(
            b"def normalize_identifier(value: int) -> str:\n"
            b"    return str(value)\n",
            "deterministic-pure-call-project",
        )

        summary = analyze_project_functions(
            main.connect_db,
            "deterministic-pure-call-project",
            analysis_request=lambda **_kwargs: self.fail(
                "a typed pure builtin conversion should not require the LLM"
            ),
        )

        self.assertEqual(summary.status, "completed")

    def test_runner_skips_llm_for_complete_validator_with_builtin_raise(self) -> None:
        self.create_indexed_project(
            b"class FunctionIssue:\n"
            b"    @model_validator(mode='after')\n"
            b"    def valid_line_order(self):\n"
            b"        if (\n"
            b"            self.start_line is not None\n"
            b"            and self.end_line is not None\n"
            b"            and self.end_line < self.start_line\n"
            b"        ):\n"
            b"            raise ValueError('issue end_line cannot precede start_line')\n"
            b"        return self\n",
            "deterministic-validator-project",
        )

        summary = analyze_project_functions(
            main.connect_db,
            "deterministic-validator-project",
            analysis_request=lambda **_kwargs: self.fail(
                "a complete validator with a built-in raise should not require the LLM"
            ),
        )

        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.completed_count, 1)
        self.assertEqual(summary.failed_count, 0)
        self.assertEqual(summary.model_request_count, 0)
        self.assertEqual(summary.deterministic_count, 1)
        with main.connect_db() as db:
            analysis = db.execute(
                """SELECT analysis.summary FROM project_symbol_analyses AS analysis
                   JOIN project_symbols AS symbol ON symbol.id=analysis.symbol_id
                   WHERE symbol.project_id='deterministic-validator-project'"""
            ).fetchone()
        self.assertIn("validates `self.start_line is not None", analysis["summary"])
        self.assertIn("raises ValueError", analysis["summary"])
        self.assertIn("returns FunctionIssue", analysis["summary"])

    def test_runner_skips_llm_for_complete_contract_with_calls(self) -> None:
        self.create_indexed_project(
            b"def normalize(value: str) -> str:\n"
            b"    return value.strip()\n",
            "deterministic-contract-call-project",
        )
        calls: list[str] = []

        summary = analyze_project_functions(
            main.connect_db,
            "deterministic-contract-call-project",
            analysis_request=lambda **kwargs: (
                calls.append(kwargs["qualified_name"]) or valid_result()
            ),
        )

        self.assertEqual(calls, [])
        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.deterministic_count, 1)

    def test_runner_skips_llm_for_noop_stub_functions(self) -> None:
        self.create_indexed_project(
            b"def lifecycle_hook() -> None:\n"
            b"    pass\n\n"
            b"def placeholder():\n"
            b"    ...\n",
            "deterministic-stub-project",
        )
        progress: list[tuple[object, ...]] = []

        summary = analyze_project_functions(
            main.connect_db,
            "deterministic-stub-project",
            analysis_request=lambda **_kwargs: self.fail(
                "no-op stubs should not require the LLM"
            ),
            progress_callback=lambda *values: progress.append(values),
        )

        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.completed_count, 2)
        self.assertEqual(
            [event[0] for event in progress].count("using_deterministic_function"),
            2,
        )
        with main.connect_db() as db:
            analyses = db.execute(
                """
                SELECT symbol.qualified_name, analysis.may_return_value,
                       analysis.return_nullable, analysis.summary
                FROM project_symbol_analyses AS analysis
                JOIN project_symbols AS symbol ON symbol.id = analysis.symbol_id
                WHERE symbol.project_id = 'deterministic-stub-project'
                ORDER BY symbol.start_byte
                """
            ).fetchall()
        self.assertEqual([row["qualified_name"] for row in analyses], ["lifecycle_hook", "placeholder"])
        self.assertEqual([row["may_return_value"] for row in analyses], [0, 0])
        self.assertTrue(all("without an LLM call" in row["summary"] for row in analyses))

    def test_runner_skips_llm_for_complete_contract_using_a_module_global(self) -> None:
        self.create_indexed_project(
            b"GLOBAL_VALUE = 1\n\n"
            b"def uses_global() -> int:\n"
            b"    return GLOBAL_VALUE\n",
            "deterministic-noise-project",
        )
        calls: list[str] = []

        summary = analyze_project_functions(
            main.connect_db,
            "deterministic-noise-project",
            analysis_request=lambda **kwargs: (
                calls.append(kwargs["qualified_name"]) or valid_result(issue_line=4)
            ),
        )

        self.assertEqual(calls, [])
        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.deterministic_count, 1)

    def test_malformed_model_json_uses_uncached_fallback_result(self) -> None:
        self.create_indexed_project(
            b"def broken_model_response(value):\n    return value\n",
            "malformed-json-project",
        )

        def malformed_response(**_kwargs):
            raise json.JSONDecodeError("Expecting ',' delimiter", '{"summary":"x"', 12)

        summary = analyze_project_functions(
            main.connect_db,
            "malformed-json-project",
            analysis_request=malformed_response,
        )

        self.assertEqual(summary.status, "failed")
        self.assertEqual((summary.completed_count, summary.failed_count), (0, 1))
        with main.connect_db() as db:
            symbol = db.execute(
                """
                SELECT symbol.analysis_status, symbol.analysis_error, analysis.summary,
                       analysis.confidence
                FROM project_symbols AS symbol
                JOIN project_symbol_analyses AS analysis ON analysis.symbol_id = symbol.id
                WHERE symbol.project_id = 'malformed-json-project'
                  AND symbol.qualified_name = 'broken_model_response'
                """
            ).fetchone()
            issue_titles = [
                row["title"]
                for row in db.execute(
                    """
                    SELECT issue.title
                    FROM project_symbol_issues AS issue
                    JOIN project_symbols AS symbol ON symbol.id = issue.symbol_id
                    WHERE symbol.project_id = 'malformed-json-project'
                    ORDER BY issue.ordinal
                    """
                ).fetchall()
            ]
            cache_count = db.execute(
                "SELECT COUNT(*) FROM function_analysis_cache"
            ).fetchone()[0]

        self.assertEqual(symbol["analysis_status"], "failed")
        self.assertIn("Incomplete model review", symbol["analysis_error"])
        self.assertIn("Fallback static analysis", symbol["summary"])
        self.assertLess(float(symbol["confidence"]), 0.5)
        self.assertEqual(issue_titles, [])
        self.assertEqual(cache_count, 0)

    def test_partial_model_contract_is_stored_as_completed_analysis(self) -> None:
        self.create_indexed_project(
            b"def passthrough(value):\n    return value\n",
            "partial-contract-project",
        )
        partial = valid_result().model_copy(
            update={
                "review_status": "partial",
                "validation_notes": ["Unresolved parameter type: value"],
            }
        )

        summary = analyze_project_functions(
            main.connect_db,
            "partial-contract-project",
            analysis_request=lambda **_kwargs: partial,
        )

        with main.connect_db() as db:
            symbol = db.execute(
                "SELECT analysis_status, analysis_error FROM project_symbols "
                "WHERE project_id = 'partial-contract-project'"
            ).fetchone()
        self.assertEqual(summary.status, "completed")
        self.assertEqual((summary.completed_count, summary.failed_count), (1, 0))
        self.assertEqual(tuple(symbol), ("completed", None))

    def test_changed_source_is_marked_stale_without_calling_ollama(self) -> None:
        content = b"def first(value):\n    return value\n"
        file_id = self.create_indexed_project(content, "stale-project")
        with main.connect_db() as db:
            db.execute(
                "UPDATE project_files SET content = ? WHERE id = ?",
                (b"def changed():\n    return 2\n", file_id),
            )
        calls: list[bool] = []
        summary = analyze_project_functions(
            main.connect_db,
            "stale-project",
            analysis_request=lambda **_kwargs: calls.append(True),
        )
        self.assertEqual(calls, [])
        self.assertEqual(summary.status, "failed")
        self.assertEqual(summary.failed_count, 1)
        with main.connect_db() as db:
            status = db.execute(
                "SELECT analysis_status FROM project_symbols WHERE project_id = 'stale-project'"
            ).fetchone()[0]
        self.assertEqual(status, "stale")

    def test_oversized_function_is_analysed_in_bounded_chunks(self) -> None:
        content = (
            b"def large(value):\n"
            b"    value += 1\n"
            b"    value += 2\n"
            b"    return value\n"
        )
        self.create_indexed_project(content, "large-project")
        calls: list[dict[str, object]] = []
        progress: list[tuple[object, ...]] = []

        def analyse_chunk(**kwargs):
            calls.append(kwargs)
            return valid_result(issue_line=kwargs["chunk_start_line"])

        with patch.object(
            project_function_analysis, "FUNCTION_ANALYSIS_CHUNK_CHARS", 24
        ), patch.object(
            project_function_analysis, "FUNCTION_ANALYSIS_MAX_SOURCE_CHARS", 30
        ):
            summary = analyze_project_functions(
                main.connect_db,
                "large-project",
                analysis_request=lambda **_kwargs: self.fail(
                    "whole-function request must not be used"
                ),
                chunk_analysis_request=analyse_chunk,
                progress_callback=lambda *values: progress.append(values),
            )
        self.assertGreater(len(calls), 1)
        self.assertTrue(all(len(str(call["source"])) <= 24 for call in calls))
        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.skipped_count, 0)
        self.assertTrue(
            all(event[0] == "analyzing_function_chunk" for event in progress)
        )
        self.assertIn("fragment 1 of", str(progress[0][4]))
        with main.connect_db() as db:
            analysis_count = db.execute(
                "SELECT COUNT(*) FROM project_symbol_analyses "
                "WHERE project_id = 'large-project'"
            ).fetchone()[0]
        self.assertEqual(analysis_count, 1)

    def test_oversized_function_cancellation_stops_between_chunks(self) -> None:
        content = (
            b"def large(value):\n"
            b"    value += 1\n"
            b"    value += 2\n"
            b"    return value\n"
        )
        self.create_indexed_project(content, "cancel-chunks-project")
        cancelled = threading.Event()
        calls: list[int] = []

        def first_chunk_only(**kwargs):
            calls.append(int(kwargs["chunk_index"]))
            cancelled.set()
            return valid_result(issue_line=kwargs["chunk_start_line"])

        with patch.object(
            project_function_analysis, "FUNCTION_ANALYSIS_CHUNK_CHARS", 24
        ), patch.object(
            project_function_analysis, "FUNCTION_ANALYSIS_MAX_SOURCE_CHARS", 30
        ):
            summary = analyze_project_functions(
                main.connect_db,
                "cancel-chunks-project",
                chunk_analysis_request=first_chunk_only,
                cancel_check=cancelled.is_set,
            )
        self.assertEqual(calls, [1])
        self.assertEqual(summary.status, "cancelled")
        with main.connect_db() as db:
            symbol = db.execute(
                "SELECT analysis_status, analysis_attempt_count FROM project_symbols "
                "WHERE project_id = 'cancel-chunks-project' AND symbol_kind = 'function'"
            ).fetchone()
        self.assertEqual(tuple(symbol), ("pending", 0))

    def test_cache_reuses_identical_function_and_rebases_issue_lines(self) -> None:
        user_id = self.create_user("cache-owner")
        self.create_indexed_project(
            b"# first project\ndef echo(value):\n    return value\n",
            "cache-source",
            user_id=user_id,
        )
        self.create_indexed_project(
            b"# moved\n# down\n# again\ndef echo(value):\n    return value\n",
            "cache-target",
            user_id=user_id,
        )
        first_calls: list[str] = []
        analyze_project_functions(
            main.connect_db,
            "cache-source",
            analysis_request=lambda **kwargs: (
                first_calls.append(kwargs["qualified_name"]) or valid_result(issue_line=3)
            ),
        )
        progress: list[tuple[object, ...]] = []

        def unexpected_request(**_kwargs):
            raise AssertionError("an identical cached function must not call Ollama")

        summary = analyze_project_functions(
            main.connect_db,
            "cache-target",
            analysis_request=unexpected_request,
            progress_callback=lambda *values: progress.append(values),
        )

        self.assertEqual(first_calls, ["echo"])
        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.cache_hit_count, 1)
        self.assertEqual(progress[0][0], "using_cached_function")
        with main.connect_db() as db:
            cached = db.execute(
                "SELECT use_count, response_json FROM function_analysis_cache"
            ).fetchone()
            target_issue_line = db.execute(
                """
                SELECT issue.start_line, issue.provenance
                FROM project_symbol_issues AS issue
                JOIN project_symbols AS symbol ON symbol.id = issue.symbol_id
                WHERE symbol.project_id = 'cache-target'
                """
            ).fetchone()
            project_hits = db.execute(
                "SELECT function_analysis_cache_hit_count FROM projects "
                "WHERE id = 'cache-target'"
            ).fetchone()[0]
        cached_result = FunctionAnalysisResult.model_validate_json(cached["response_json"])
        self.assertEqual(cached["use_count"], 1)
        self.assertEqual(cached_result.issues[0].start_line, 2)
        self.assertEqual(target_issue_line["start_line"], 5)
        self.assertEqual(target_issue_line["provenance"], "cache")
        self.assertEqual(project_hits, 1)

    def test_cache_preserves_deterministic_issue_provenance(self) -> None:
        user_id = self.create_user("deterministic-cache-owner")
        source = b"def wrong() -> dict:\n    return []\n"
        self.create_indexed_project(
            source,
            "deterministic-cache-source",
            user_id=user_id,
        )
        self.create_indexed_project(
            source,
            "deterministic-cache-target",
            user_id=user_id,
        )

        with main.connect_db() as db:
            source_symbol = db.execute(
                "SELECT id FROM project_symbols WHERE project_id = 'deterministic-cache-source'"
            ).fetchone()
            source_task = load_function_analysis_task(db, int(source_symbol["id"]))
            cached_result = valid_result().model_copy(
                update={
                    "issues": [
                        FunctionIssue(
                            severity="error",
                            category="type",
                            title="Return type contradicts annotation",
                            description="The list return contradicts the dict annotation.",
                            start_line=2,
                            end_line=2,
                            proof="source-v1",
                            evidence="return []",
                            failure_type="Return type mismatch",
                            trigger="The function returns the list literal.",
                            provenance="deterministic",
                        )
                    ]
                }
            )
            store_cached_function_analysis(db, source_task, cached_result)
        summary = analyze_project_functions(
            main.connect_db,
            "deterministic-cache-target",
            analysis_request=lambda **_kwargs: self.fail("cache should satisfy the analysis"),
        )

        with main.connect_db() as db:
            issues = db.execute(
                """SELECT issue.title, issue.provenance
                   FROM project_symbol_issues AS issue
                   JOIN project_symbols AS symbol ON symbol.id = issue.symbol_id
                   WHERE symbol.project_id = 'deterministic-cache-target'"""
            ).fetchall()
        self.assertEqual(summary.cache_hit_count, 1)
        cached_issue = next(
            row for row in issues if row["title"] == "Return type contradicts annotation"
        )
        self.assertEqual(cached_issue["provenance"], "deterministic")

    def test_cache_reuses_comment_only_function_changes_without_llm(self) -> None:
        user_id = self.create_user("semantic-cache-owner")
        self.create_indexed_project(
            b"def echo(value):\n    return value\n",
            "semantic-cache-source",
            user_id=user_id,
        )
        self.create_indexed_project(
            b"def echo(value):\n    # comment-only edit\n    return value\n",
            "semantic-cache-target",
            user_id=user_id,
        )
        analyze_project_functions(
            main.connect_db,
            "semantic-cache-source",
            analysis_request=lambda **_kwargs: valid_result(),
        )
        summary = analyze_project_functions(
            main.connect_db,
            "semantic-cache-target",
            analysis_request=lambda **_kwargs: self.fail(
                "comment-only semantic cache hit should avoid the LLM"
            ),
        )

        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.cache_hit_count, 1)
        with main.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM function_analysis_cache").fetchone()[0], 1)
            self.assertEqual(
                db.execute(
                    """
                    SELECT issue.provenance
                    FROM project_symbol_issues AS issue
                    JOIN project_symbols AS symbol ON symbol.id = issue.symbol_id
                    WHERE symbol.project_id = 'semantic-cache-target'
                    """
                ).fetchall(),
                [],
            )

    def test_semantic_cache_does_not_reuse_line_anchored_issues(self) -> None:
        user_id = self.create_user("semantic-cache-line-owner")
        self.create_indexed_project(
            b"def echo(value):\n    return value\n",
            "semantic-cache-line-source",
            user_id=user_id,
        )
        self.create_indexed_project(
            b"def echo(value):\n    # comment-only edit\n    return value\n",
            "semantic-cache-line-target",
            user_id=user_id,
        )
        analyze_project_functions(
            main.connect_db,
            "semantic-cache-line-source",
            analysis_request=lambda **_kwargs: valid_result(issue_line=2),
        )
        calls = 0

        def fresh_result(**_kwargs):
            nonlocal calls
            calls += 1
            return valid_result(issue_line=3)

        summary = analyze_project_functions(
            main.connect_db,
            "semantic-cache-line-target",
            analysis_request=fresh_result,
        )

        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.cache_hit_count, 0)
        self.assertEqual(calls, 1)
        with main.connect_db() as db:
            target_issue = db.execute(
                """
                SELECT issue.start_line
                FROM project_symbol_issues AS issue
                JOIN project_symbols AS symbol ON symbol.id = issue.symbol_id
                WHERE symbol.project_id = 'semantic-cache-line-target'
                """
            ).fetchone()
        self.assertEqual(target_issue["start_line"], 3)

    def test_clear_project_analysis_cache_resets_project_but_preserves_reusable_cache(self) -> None:
        user_id = self.create_user("clear-cache-owner")
        self.create_indexed_project(
            b"def echo(value):\n    return value\n",
            "clear-cache-source",
            user_id=user_id,
        )
        self.create_indexed_project(
            b"# moved\n\ndef echo(value):\n    return value\n",
            "clear-cache-target",
            user_id=user_id,
        )
        analyze_project_functions(
            main.connect_db,
            "clear-cache-source",
            analysis_request=lambda **_kwargs: valid_result(issue_line=2),
        )
        analyze_project_functions(
            main.connect_db,
            "clear-cache-target",
            analysis_request=lambda **_kwargs: valid_result(issue_line=4),
        )

        result = main.clear_project_analysis_cache(
            "clear-cache-target",
            self.authenticated_request(
                user_id,
                method="DELETE",
                path="/api/projects/clear-cache-target/analysis-cache",
            ),
        )

        self.assertEqual(result["project"]["function_analysis_status"], "pending")
        self.assertEqual(result["project"]["function_analysis_completed_count"], 0)
        self.assertEqual(result["project"]["function_analysis_cache_hit_count"], 0)
        self.assertTrue(result["cache_preserved"])
        self.assertEqual(result["cache_deleted"], 0)
        self.assertGreaterEqual(result["analysis_deleted"], 1)
        with main.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM function_analysis_cache").fetchone()[0], 1)
            self.assertEqual(
                db.execute(
                    """
                    SELECT COUNT(*) FROM project_symbol_analyses
                    WHERE project_id = 'clear-cache-target'
                    """
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute(
                    """
                    SELECT DISTINCT analysis_status FROM project_symbols
                    WHERE project_id = 'clear-cache-target'
                      AND symbol_kind IN ('function', 'method')
                    """
                ).fetchone()[0],
                "pending",
            )
        progress: list[tuple[object, ...]] = []
        summary = analyze_project_functions(
            main.connect_db,
            "clear-cache-target",
            analysis_request=lambda **_kwargs: self.fail(
                "preserved cache should satisfy the clean rerun"
            ),
            progress_callback=lambda *values: progress.append(values),
        )
        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.cache_hit_count, 1)
        self.assertEqual(progress[0][0], "using_cached_function")

    def test_purge_project_function_analysis_cache_removes_cache_but_keeps_report(self) -> None:
        user_id = self.create_user("purge-cache-owner")
        self.create_indexed_project(
            b"def echo(value):\n    return value\n",
            "purge-cache-source",
            user_id=user_id,
        )
        self.create_indexed_project(
            b"# moved\n\ndef echo(value):\n    return value\n",
            "purge-cache-target",
            user_id=user_id,
        )
        analyze_project_functions(
            main.connect_db,
            "purge-cache-source",
            analysis_request=lambda **_kwargs: valid_result(issue_line=2),
        )
        analyze_project_functions(
            main.connect_db,
            "purge-cache-target",
            analysis_request=lambda **_kwargs: self.fail(
                "target should be satisfied by the reusable cache"
            ),
        )
        with main.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM function_analysis_cache").fetchone()[0], 1)
            self.assertEqual(
                db.execute(
                    """
                    SELECT COUNT(*) FROM project_symbol_analyses
                    WHERE project_id = 'purge-cache-target'
                    """
                ).fetchone()[0],
                1,
            )

        result = main.purge_project_function_analysis_cache(
            "purge-cache-target",
            self.authenticated_request(
                user_id,
                method="DELETE",
                path="/api/projects/purge-cache-target/function-analysis-cache",
            ),
        )

        self.assertEqual(result["cache_deleted"], 1)
        self.assertTrue(result["analysis_preserved"])
        self.assertEqual(result["project"]["function_analysis_status"], "completed")
        with main.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM function_analysis_cache").fetchone()[0], 0)
            self.assertEqual(
                db.execute(
                    """
                    SELECT COUNT(*) FROM project_symbol_analyses
                    WHERE project_id = 'purge-cache-target'
                    """
                ).fetchone()[0],
                1,
            )

    def test_clear_project_removes_matching_function_cache_rows(self) -> None:
        user_id = self.create_user("clear-project-cache-owner")
        chat_id = "clear-project-cache-chat"
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES (?, ?, 'Clear Project')",
                (chat_id, user_id),
            )
        self.create_indexed_project(
            b"def echo(value):\n    return value\n",
            "clear-project-cache-source",
            user_id=user_id,
        )
        analyze_project_functions(
            main.connect_db,
            "clear-project-cache-source",
            analysis_request=lambda **_kwargs: valid_result(issue_line=2),
        )
        with main.connect_db() as db:
            db.execute(
                "UPDATE projects SET chat_id = ? WHERE id = 'clear-project-cache-source'",
                (chat_id,),
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM function_analysis_cache").fetchone()[0], 1)

        result = main.delete_project(
            "clear-project-cache-source",
            self.authenticated_request(
                user_id,
                method="DELETE",
                path="/api/projects/clear-project-cache-source",
            ),
        )

        self.assertIn("1 cached function result", result["message"])
        with main.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM function_analysis_cache").fetchone()[0], 0)

    def test_cache_reuses_the_merged_contract_for_an_oversized_function(self) -> None:
        user_id = self.create_user("chunk-cache-owner")
        function = (
            b"def large(value):\n"
            b"    value += 1\n"
            b"    value += 2\n"
            b"    return value\n"
        )
        self.create_indexed_project(
            function, "chunk-cache-source", user_id=user_id
        )
        self.create_indexed_project(
            b"# moved down\n" + function,
            "chunk-cache-target",
            user_id=user_id,
        )
        chunk_calls: list[int] = []

        def analyse_chunk(**kwargs):
            chunk_calls.append(int(kwargs["chunk_index"]))
            return valid_result(issue_line=kwargs["chunk_start_line"])

        with patch.object(
            project_function_analysis, "FUNCTION_ANALYSIS_CHUNK_CHARS", 24
        ), patch.object(
            project_function_analysis, "FUNCTION_ANALYSIS_MAX_SOURCE_CHARS", 30
        ):
            analyze_project_functions(
                main.connect_db,
                "chunk-cache-source",
                chunk_analysis_request=analyse_chunk,
            )
            first_call_count = len(chunk_calls)
            summary = analyze_project_functions(
                main.connect_db,
                "chunk-cache-target",
                analysis_request=lambda **_kwargs: self.fail(
                    "cached merged function must not call Ollama"
                ),
                chunk_analysis_request=lambda **_kwargs: self.fail(
                    "cached merged function must not call chunk analysis"
                ),
            )
        self.assertGreater(first_call_count, 1)
        self.assertEqual(len(chunk_calls), first_call_count)
        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.cache_hit_count, 1)

    def test_cache_is_not_shared_between_users(self) -> None:
        first_user = self.create_user("cache-user-one")
        second_user = self.create_user("cache-user-two")
        content = b"def echo(value):\n    return value\n"
        self.create_indexed_project(
            content, "cache-user-one-project", user_id=first_user
        )
        self.create_indexed_project(
            content, "cache-user-two-project", user_id=second_user
        )
        analyze_project_functions(
            main.connect_db,
            "cache-user-one-project",
            analysis_request=lambda **_kwargs: valid_result(),
        )
        second_calls: list[bool] = []
        summary = analyze_project_functions(
            main.connect_db,
            "cache-user-two-project",
            analysis_request=lambda **_kwargs: (
                second_calls.append(True) or valid_result()
            ),
        )
        self.assertEqual(second_calls, [True])
        self.assertEqual(summary.cache_hit_count, 0)
        with main.connect_db() as db:
            cache_users = db.execute(
                "SELECT user_id FROM function_analysis_cache ORDER BY user_id"
            ).fetchall()
        self.assertEqual([row["user_id"] for row in cache_users], [first_user, second_user])

    def test_corrupt_cache_entry_is_replaced_by_a_fresh_result(self) -> None:
        user_id = self.create_user("cache-corrupt-owner")
        content = b"def echo(value):\n    return value\n"
        self.create_indexed_project(
            content, "cache-corrupt-source", user_id=user_id
        )
        self.create_indexed_project(
            content, "cache-corrupt-target", user_id=user_id
        )
        analyze_project_functions(
            main.connect_db,
            "cache-corrupt-source",
            analysis_request=lambda **_kwargs: valid_result(),
        )
        with main.connect_db() as db:
            db.execute("UPDATE function_analysis_cache SET response_json = '{broken'")
        calls: list[bool] = []
        summary = analyze_project_functions(
            main.connect_db,
            "cache-corrupt-target",
            analysis_request=lambda **_kwargs: calls.append(True) or valid_result(),
        )
        self.assertEqual(calls, [True])
        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.cache_hit_count, 0)
        with main.connect_db() as db:
            repaired_json = db.execute(
                "SELECT response_json FROM function_analysis_cache"
            ).fetchone()[0]
        self.assertEqual(
            FunctionAnalysisResult.model_validate_json(repaired_json).contract_version,
            "1.0",
        )

    def test_cache_maintenance_removes_expired_and_excess_per_user_rows(self) -> None:
        user_id = self.create_user("cache-maintenance-owner")
        now = 2_000_000_000
        old = now - 10 * 86_400
        with patch.object(main, "FUNCTION_ANALYSIS_CACHE_RETENTION_DAYS", 2), patch.object(
            main, "FUNCTION_ANALYSIS_CACHE_MAX_ROWS_PER_USER", 2
        ):
            with main.connect_db() as db:
                for index, timestamp in enumerate((old, now - 10, now - 5, now - 1)):
                    db.execute(
                        """
                        INSERT INTO function_analysis_cache(
                            user_id, language, function_sha256, contract_version,
                            model_name, response_json, created_at, updated_at, last_used_at
                        ) VALUES (?, 'python', ?, '1.0', 'test-model', '{}',
                                  datetime(?, 'unixepoch'), datetime(?, 'unixepoch'),
                                  datetime(?, 'unixepoch'))
                        """,
                        (user_id, f"{index + 1:064x}", timestamp, timestamp, timestamp),
                    )
                deleted = main.prune_function_analysis_cache(db, now=now)
                remaining = db.execute(
                    "SELECT COUNT(*) FROM function_analysis_cache WHERE user_id = ?",
                    (user_id,),
                ).fetchone()[0]
        self.assertEqual(deleted, 2)
        self.assertEqual(remaining, 2)


if __name__ == "__main__":
    import unittest

    unittest.main()
