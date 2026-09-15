from __future__ import annotations

import ast
import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch

import main
from analysis_engine import (
    FunctionAnalysisResult,
    FunctionParameterContract,
    FunctionReturnContract,
    FunctionReturnType,
)
from language_adapters.python_adapter import PythonAdapter
from migrations import migration_030_analysis_signal_metrics
from project_call_compatibility import (
    check_project_call_compatibility,
    normalize_type,
    types_compatible,
)
from project_function_analysis import (
    deterministic_python_contract,
    load_function_analysis_task,
    persist_function_analysis,
)
from project_inventory import inventory_project_database
from project_parsing import parse_project_database
from tests.helpers import DatabaseTestCase


def contract(
    *,
    parameter: bool = False,
    returns: str | None = "int",
) -> FunctionAnalysisResult:
    parameters = []
    if parameter:
        parameters.append(
            FunctionParameterContract(
                name="value",
                kind="positional_or_keyword",
                required=True,
                accepted_types=["int"],
                description="Input integer.",
            )
        )
    return FunctionAnalysisResult(
        contract_version="1.0",
        summary="Test contract.",
        syntax_valid=True,
        parameters=parameters,
        returns=FunctionReturnContract(
            may_return_value=returns is not None,
            possible_types=(
                [FunctionReturnType(type=returns, description="Returned value.")]
                if returns is not None
                else []
            ),
            nullable=False,
            description="Test return contract.",
        ),
        confidence=0.95,
    )


class ProjectCallCompatibilityTests(DatabaseTestCase):
    def create_source_project(
        self,
        project_id: str,
        content: bytes,
    ) -> tuple[int, str]:
        user_id = self.create_user(project_id)
        chat_id = f"{project_id}-chat"
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES (?, ?, 'Calls')",
                (chat_id, user_id),
            )
            db.execute(
                """
                INSERT INTO projects(id, user_id, chat_id, name, source_kind, file_count, total_bytes)
                VALUES (?, ?, ?, 'calls', 'folder', 1, ?)
                """,
                (project_id, user_id, chat_id, len(content)),
            )
            db.execute(
                """
                INSERT INTO project_files(project_id, path, content, size_bytes, sha256, is_binary)
                VALUES (?, 'calls.py', ?, ?, ?, 0)
                """,
                (project_id, content, len(content), hashlib.sha256(content).hexdigest()),
            )
            inventory_project_database(db, project_id)
            parse_project_database(db, project_id)
        return user_id, project_id

    def test_optional_union_type_labels_accept_member_types(self) -> None:
        self.assertTrue(types_compatible(("float",), ("float | None",)))
        self.assertTrue(types_compatible(("None",), ("float | None",)))
        self.assertTrue(types_compatible(("int",), ("Optional[float]",)))
        self.assertFalse(types_compatible(("str",), ("float | None",)))
        self.assertTrue(types_compatible(("HTMLResponse",), ("Response",)))
        self.assertTrue(types_compatible(("ast.Attribute",), ("ast.AST",)))
        self.assertTrue(types_compatible(("FunctionDef",), ("AST",)))
        self.assertTrue(types_compatible(("str",), ("Literal['user', 'assistant']",)))
        self.assertTrue(types_compatible(("None",), ("'dict | None'",)))
        self.assertTrue(types_compatible(("LivePressureGuard",), ("'LivePressureGuard'",)))

    def test_generic_container_types_preserve_outer_type_and_compare_elements(self) -> None:
        self.assertEqual(normalize_type("tuple[object, ...]"), "tuple")
        self.assertEqual(normalize_type("typing.Tuple[str, ...]"), "tuple")
        self.assertTrue(
            types_compatible(("tuple[str, ...]",), ("tuple[object, ...]",))
        )
        self.assertTrue(
            types_compatible(("tuple[str, int]",), ("tuple[object, ...]",))
        )
        self.assertTrue(types_compatible(("list[str]",), ("Sequence[str]",)))
        self.assertFalse(
            types_compatible(("list[str]",), ("tuple[str, ...]",))
        )
        self.assertFalse(
            types_compatible(("tuple[int, ...]",), ("tuple[str, ...]",))
        )
        self.assertIsNone(
            types_compatible(("tuple",), ("tuple[str, ...]",))
        )
        self.assertTrue(
            types_compatible(
                ("FunctionAnalysisTask",),
                ("object with attributes file_id, source",),
            )
        )
        self.assertTrue(
            types_compatible(
                ("FunctionAnalysisTask",),
                ("object with attribute symbol_id",),
            )
        )

    def test_python_flow_narrowing_slices_and_callable_aliases_are_inferred(self) -> None:
        module = ast.parse(
            "from typing import Callable\n"
            "BatchRequest = Callable[..., dict[str, int]]\n"
            "def review(node: ast.AST, candidates: list[Item], request: BatchRequest | None):\n"
            "    if not isinstance(node, ast.Call):\n"
            "        return\n"
            "    target(node)\n"
            "    target(candidates[:2])\n"
            "    target(request)\n"
            "def review_many(nodes: list[ast.AST]):\n"
            "    for node in nodes:\n"
            "        if not isinstance(node, ast.Raise):\n"
            "            continue\n"
            "        target(node)\n"
            "def make_item() -> Item: ...\n"
            "def review_optional(item: Item | None):\n"
            "    with context():\n"
            "        item = make_item()\n"
            "    target(item)\n"
        )

        inferred = PythonAdapter._ast_argument_type_index(module)
        calls = sorted([
            node for node in ast.walk(module)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "target"
        ], key=lambda node: node.lineno)

        self.assertEqual(inferred[id(calls[0].args[0])], ("ast.Call",))
        self.assertEqual(inferred[id(calls[1].args[0])], ("list[Item]",))
        self.assertEqual(
            inferred[id(calls[2].args[0])],
            ("BatchRequest", "Callable", "None"),
        )
        self.assertEqual(inferred[id(calls[3].args[0])], ("ast.Raise",))
        self.assertEqual(inferred[id(calls[4].args[0])], ("Item",))

    def test_python_union_isinstance_and_comprehension_filters_narrow_types(self) -> None:
        module = ast.parse(
            "def inspect_nodes(nodes: list[ast.AST], current: ast.AST | None):\n"
            "    if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):\n"
            "        target(current)\n"
            "    for selected in (node for node in nodes "
            "if isinstance(node, ast.Try | ast.TryStar)):\n"
            "        target(selected)\n"
        )
        inferred = PythonAdapter._ast_argument_type_index(module)
        calls = sorted(
            (
                node for node in ast.walk(module)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "target"
            ),
            key=lambda node: node.lineno,
        )
        self.assertEqual(
            inferred[id(calls[0].args[0])],
            ("ast.FunctionDef", "ast.AsyncFunctionDef"),
        )
        self.assertEqual(
            inferred[id(calls[1].args[0])],
            ("ast.Try", "ast.TryStar"),
        )

    def test_python_tuple_literals_record_homogeneous_and_fixed_element_types(self) -> None:
        homogeneous = PythonAdapter._ast_literal_types(
            ast.parse("target(('a', 'b'))").body[0].value.args[0]
        )
        fixed = PythonAdapter._ast_literal_types(
            ast.parse("target(('a', 1))").body[0].value.args[0]
        )
        self.assertEqual(homogeneous, ("tuple[str, ...]",))
        self.assertEqual(fixed, ("tuple[str, int]",))

    def test_nested_calls_resolve_to_their_own_lexical_closure(self) -> None:
        _user_id, project_id = self.create_source_project(
            "lexical-call-project",
            (
                b"def first():\n"
                b"    def update_progress():\n"
                b"        return 1\n"
                b"    return update_progress()\n\n"
                b"def second():\n"
                b"    def update_progress():\n"
                b"        return 2\n"
                b"    return update_progress()\n"
            ),
        )
        with main.connect_db() as db:
            rows = db.execute(
                """
                SELECT caller.qualified_name, target.qualified_name,
                       call.resolution_status
                FROM project_calls AS call
                JOIN project_symbols AS caller ON caller.id = call.caller_symbol_id
                LEFT JOIN project_symbols AS target ON target.id = call.resolved_symbol_id
                WHERE call.project_id = ? AND call.callee = 'update_progress'
                ORDER BY call.start_line
                """,
                (project_id,),
            ).fetchall()

        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("first", "first.update_progress", "internal"),
                ("second", "second.update_progress", "internal"),
            ],
        )

    def test_bare_method_name_does_not_resolve_through_python_class_scope(self) -> None:
        _user_id, project_id = self.create_source_project(
            "bare-method-project",
            (
                b"def helper():\n"
                b"    return 1\n\n"
                b"class Box:\n"
                b"    def helper(self):\n"
                b"        return 2\n\n"
                b"    def run(self):\n"
                b"        return helper()\n"
            ),
        )
        with main.connect_db() as db:
            row = db.execute(
                """
                SELECT target.qualified_name, call.resolution_status
                FROM project_calls AS call
                LEFT JOIN project_symbols AS target ON target.id = call.resolved_symbol_id
                WHERE call.project_id = ? AND call.callee = 'helper'
                """,
                (project_id,),
            ).fetchone()

        self.assertEqual(tuple(row), ("helper", "internal"))

    def test_inherited_self_method_resolves_and_uses_the_base_contract(self) -> None:
        _user_id, project_id = self.create_source_project(
            "inherited-method-project",
            (
                b"class Base:\n"
                b"    def close(self, value: int) -> int:\n"
                b"        return value\n\n"
                b"class Child(Base):\n"
                b"    def run(self) -> int:\n"
                b"        return self.close(1)\n"
            ),
        )
        with main.connect_db() as db:
            for symbol in db.execute(
                """
                SELECT id, qualified_name FROM project_symbols
                WHERE project_id = ? AND symbol_kind IN ('function', 'method')
                """,
                (project_id,),
            ).fetchall():
                result = (
                    contract(parameter=True)
                    if symbol["qualified_name"] == "Base.close"
                    else contract()
                )
                task = load_function_analysis_task(db, int(symbol["id"]))
                persist_function_analysis(db, task, result)
            summary = check_project_call_compatibility(db, project_id)
            row = db.execute(
                """
                SELECT target.qualified_name, compatibility.status
                FROM project_calls AS call
                JOIN project_call_compatibility AS compatibility
                  ON compatibility.call_id = call.id
                LEFT JOIN project_symbols AS target ON target.id = call.resolved_symbol_id
                WHERE call.project_id = ? AND call.callee = 'self.close'
                """,
                (project_id,),
            ).fetchone()

        self.assertEqual(tuple(row), ("Base.close", "compatible"))
        self.assertEqual(summary.unknown_count, 0)

    def test_explicit_and_generated_constructor_calls_are_distinguished(self) -> None:
        _user_id, project_id = self.create_source_project(
            "constructor-call-project",
            (
                b"class Explicit:\n"
                b"    def __init__(self, value: int) -> None:\n"
                b"        self.value = value\n\n"
                b"class Generated:\n"
                b"    pass\n\n"
                b"class Inherited(Explicit):\n"
                b"    pass\n\n"
                b"def build() -> Explicit:\n"
                b"    explicit = Explicit(1)\n"
                b"    inherited: Inherited = Inherited(2)\n"
                b"    Generated()\n"
                b"    return explicit\n"
            ),
        )
        with main.connect_db() as db:
            symbols = db.execute(
                """
                SELECT id, qualified_name FROM project_symbols
                WHERE project_id = ? AND symbol_kind IN ('function', 'method')
                """,
                (project_id,),
            ).fetchall()
            for symbol in symbols:
                result = (
                    contract(parameter=True, returns=None)
                    if symbol["qualified_name"] == "Explicit.__init__"
                    else contract(returns="Explicit")
                )
                task = load_function_analysis_task(db, int(symbol["id"]))
                persist_function_analysis(db, task, result)
            summary = check_project_call_compatibility(db, project_id)
            rows = db.execute(
                """
                SELECT call.callee, target.qualified_name, compatibility.status,
                       compatibility.scope_status, finding.finding_kind
                FROM project_calls AS call
                JOIN project_call_compatibility AS compatibility
                  ON compatibility.call_id = call.id
                LEFT JOIN project_symbols AS target ON target.id = call.resolved_symbol_id
                LEFT JOIN project_call_findings AS finding ON finding.call_id = call.id
                WHERE call.project_id = ?
                  AND call.callee IN ('Explicit', 'Inherited', 'Generated')
                ORDER BY call.start_line
                """,
                (project_id,),
            ).fetchall()

        self.assertEqual(
            tuple(rows[0]),
            ("Explicit", "Explicit.__init__", "compatible", "in_scope", None),
        )
        self.assertEqual(
            tuple(rows[1]),
            ("Inherited", "Explicit.__init__", "compatible", "in_scope", None),
        )
        self.assertEqual(
            tuple(rows[2]),
            ("Generated", "Generated", "unknown", "out_of_scope", "internal_constructor_call"),
        )
        self.assertEqual(summary.not_checked_count, 1)

    def test_callable_parameters_and_receiver_methods_are_outside_local_scope(self) -> None:
        _user_id, project_id = self.create_source_project(
            "dynamic-call-project",
            (
                b"import ast\n\n"
                b"def run(callback, database):\n"
                b"    callback(1)\n"
                b"    database.execute('SELECT 1')\n"
                b"\n"
                b"class Worker(ast.NodeVisitor):\n"
                b"    def dispatch(self, callback):\n"
                b"        local = callback\n"
                b"        def inner():\n"
                b"            callback()\n"
                b"            local()\n"
                b"            self.queue.put_nowait(1)\n"
                b"            self.generic_visit(None)\n"
                b"        return inner\n"
            ),
        )
        with main.connect_db() as db:
            symbols = db.execute(
                """
                SELECT id FROM project_symbols
                WHERE project_id = ? AND symbol_kind IN ('function', 'method')
                """,
                (project_id,),
            ).fetchall()
            for symbol in symbols:
                task = load_function_analysis_task(db, int(symbol["id"]))
                persist_function_analysis(
                    db,
                    task,
                    deterministic_python_contract(task, contract(returns=None)),
                )
            summary = check_project_call_compatibility(db, project_id)
            kinds = {
                str(row["callee"]): str(row["finding_kind"])
                for row in db.execute(
                    """
                    SELECT call.callee, finding.finding_kind
                    FROM project_calls AS call
                    JOIN project_call_findings AS finding ON finding.call_id = call.id
                    WHERE call.project_id = ?
                    """,
                    (project_id,),
                ).fetchall()
            }

        self.assertEqual(kinds["callback"], "callable_parameter_call")
        self.assertEqual(kinds["database.execute"], "dynamic_method_call")
        self.assertEqual(kinds["local"], "dynamic_local_call")
        self.assertEqual(
            kinds["self.queue.put_nowait"],
            "dynamic_receiver_method_call",
        )
        self.assertEqual(
            kinds["self.generic_visit"],
            "external_inherited_method_call",
        )
        self.assertEqual(summary.unknown_count, 0)
        self.assertEqual(summary.not_checked_count, 6)

    def create_project(self) -> tuple[int, str]:
        user_id = self.create_user("compatibility")
        project_id = "compatibility-project"
        content = (
            b"def expects(value):\n    return value\n\n"
            b"def no_value(value):\n    print(value)\n\n"
            b"def caller():\n"
            b"    good: int = expects(1)\n"
            b"    bad_type = expects('bad')\n"
            b"    missing = expects()\n"
            b"    extra = expects(1, 2)\n"
            b"    void_value: int = no_value(1)\n"
            b"    external(1)\n"
            b"    return no_value(1)\n\n"
            b"def recurse(value):\n    return recurse(value - 1)\n"
        )
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('compat-chat', ?, 'Calls')",
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO projects(id, user_id, chat_id, name, source_kind, file_count, total_bytes)
                VALUES (?, ?, 'compat-chat', 'calls', 'folder', 1, ?)
                """,
                (project_id, user_id, len(content)),
            )
            db.execute(
                """
                INSERT INTO project_files(project_id, path, content, size_bytes, sha256, is_binary)
                VALUES (?, 'calls.py', ?, ?, ?, 0)
                """,
                (project_id, content, len(content), hashlib.sha256(content).hexdigest()),
            )
            inventory_project_database(db, project_id)
            parse_project_database(db, project_id)
            symbols = db.execute(
                "SELECT id, name FROM project_symbols WHERE symbol_kind = 'function' ORDER BY id"
            ).fetchall()
            for symbol in symbols:
                name = str(symbol["name"])
                result = {
                    "expects": contract(parameter=True),
                    "no_value": contract(parameter=True, returns=None),
                    "caller": contract(),
                    "recurse": contract(parameter=True),
                }[name]
                task = load_function_analysis_task(db, int(symbol["id"]))
                persist_function_analysis(db, task, result)
        return user_id, project_id

    def test_resolved_calls_are_checked_without_guessing_unknown_values(self) -> None:
        _user_id, project_id = self.create_project()
        with main.connect_db() as db:
            summary = check_project_call_compatibility(db, project_id)
            repeated_summary = check_project_call_compatibility(db, project_id)
            rows = db.execute(
                """
                SELECT call.callee, call.start_line, call.resolution_status,
                       compatibility.status
                FROM project_calls AS call
                JOIN project_call_compatibility AS compatibility
                  ON compatibility.call_id = call.id
                WHERE call.project_id = ? ORDER BY call.start_byte
                """,
                (project_id,),
            ).fetchall()
            finding_kinds = {
                str(row[0])
                for row in db.execute(
                    """
                    SELECT finding_kind FROM project_call_findings AS finding
                    JOIN project_call_compatibility AS compatibility
                      ON compatibility.call_id = finding.call_id
                    WHERE compatibility.project_id = ?
                    """,
                    (project_id,),
                ).fetchall()
            }
            recursive = db.execute(
                """
                SELECT call.caller_symbol_id, call.resolved_symbol_id
                FROM project_calls AS call WHERE call.callee = 'recurse'
                """
            ).fetchone()

        by_line = {int(row["start_line"]): tuple(row) for row in rows}
        self.assertEqual(by_line[8][3], "compatible")
        self.assertEqual(by_line[9][3], "incompatible")
        self.assertEqual(by_line[10][3], "incompatible")
        self.assertEqual(by_line[11][3], "incompatible")
        self.assertEqual(by_line[12][3], "incompatible")
        self.assertEqual(by_line[13][2:], ("unresolved", "unknown"))
        self.assertEqual(by_line[14][3], "incompatible")
        self.assertEqual(by_line[17][3], "unknown")
        self.assertEqual(recursive["caller_symbol_id"], recursive["resolved_symbol_id"])
        self.assertGreaterEqual(summary.incompatible_count, 5)
        self.assertEqual(repeated_summary, summary)
        self.assertIn("argument_type", finding_kinds)
        self.assertIn("missing_argument", finding_kinds)
        self.assertIn("unexpected_argument", finding_kinds)
        self.assertIn("void_value_used", finding_kinds)
        self.assertIn("unresolved_internal_call", finding_kinds)

    def test_bound_method_self_is_not_required_from_model_contract(self) -> None:
        user_id = self.create_user("bound-method")
        project_id = "bound-method-project"
        content = (
            b"class Box:\n"
            b"    def close(self):\n"
            b"        return None\n\n"
            b"def caller(box):\n"
            b"    box.close()\n"
        )
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('bound-chat', ?, 'Bound')",
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO projects(id, user_id, chat_id, name, source_kind, file_count, total_bytes)
                VALUES (?, ?, 'bound-chat', 'bound', 'folder', 1, ?)
                """,
                (project_id, user_id, len(content)),
            )
            db.execute(
                """
                INSERT INTO project_files(project_id, path, content, size_bytes, sha256, is_binary)
                VALUES (?, 'bound.py', ?, ?, ?, 0)
                """,
                (project_id, content, len(content), hashlib.sha256(content).hexdigest()),
            )
            inventory_project_database(db, project_id)
            parse_project_database(db, project_id)
            close_symbol = db.execute(
                "SELECT id FROM project_symbols WHERE qualified_name = 'Box.close'"
            ).fetchone()
            task = load_function_analysis_task(db, int(close_symbol["id"]))
            persist_function_analysis(
                db,
                task,
                FunctionAnalysisResult(
                    contract_version="1.0",
                    summary="Closes the box.",
                    syntax_valid=True,
                    parameters=[
                        FunctionParameterContract(
                            name="self",
                            kind="unknown",
                            required=True,
                            accepted_types=["object"],
                            description="Receiver.",
                        )
                    ],
                    returns=FunctionReturnContract(
                        may_return_value=False,
                        possible_types=[],
                        nullable=False,
                        description="No return value.",
                    ),
                    confidence=0.5,
                ),
            )
            check_project_call_compatibility(db, project_id)
            row = db.execute(
                """
                SELECT compatibility.status
                FROM project_calls AS call
                JOIN project_call_compatibility AS compatibility
                  ON compatibility.call_id = call.id
                WHERE call.callee = 'box.close'
                """
            ).fetchone()
            findings = [
                str(item["finding_kind"])
                for item in db.execute(
                    """
                    SELECT finding.finding_kind
                    FROM project_call_findings AS finding
                    JOIN project_calls AS call ON call.id = finding.call_id
                    WHERE call.callee = 'box.close'
                    """
                ).fetchall()
            ]

        self.assertNotIn("missing_argument", findings)
        self.assertNotEqual(row["status"], "incompatible")

    def test_value_returning_calls_are_compatible_in_conditions(self) -> None:
        user_id = self.create_user("condition-call")
        project_id = "condition-call-project"
        content = (
            b"def maybe_user():\n"
            b"    return object()\n\n"
            b"def page():\n"
            b"    if maybe_user():\n"
            b"        return 'yes'\n"
            b"    return 'no'\n"
        )
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('condition-chat', ?, 'Condition')",
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO projects(id, user_id, chat_id, name, source_kind, file_count, total_bytes)
                VALUES (?, ?, 'condition-chat', 'condition', 'folder', 1, ?)
                """,
                (project_id, user_id, len(content)),
            )
            db.execute(
                """
                INSERT INTO project_files(project_id, path, content, size_bytes, sha256, is_binary)
                VALUES (?, 'condition.py', ?, ?, ?, 0)
                """,
                (project_id, content, len(content), hashlib.sha256(content).hexdigest()),
            )
            inventory_project_database(db, project_id)
            parse_project_database(db, project_id)
            maybe = db.execute(
                "SELECT id FROM project_symbols WHERE qualified_name = 'maybe_user'"
            ).fetchone()
            task = load_function_analysis_task(db, int(maybe["id"]))
            persist_function_analysis(
                db,
                task,
                contract(returns="object | None"),
            )
            check_project_call_compatibility(db, project_id)
            row = db.execute(
                """
                SELECT call.usage_kind, compatibility.status
                FROM project_calls AS call
                JOIN project_call_compatibility AS compatibility
                  ON compatibility.call_id = call.id
                WHERE call.callee = 'maybe_user'
                """
            ).fetchone()

        self.assertEqual(row["usage_kind"], "condition")
        self.assertEqual(row["status"], "compatible")

    def test_async_no_value_calls_are_valid_until_they_are_awaited_as_values(self) -> None:
        content = (
            b"async def no_value():\n    pass\n\n"
            b"async def run():\n"
            b"    await no_value()\n"
            b"    task = create_task(no_value())\n"
            b"    result = await no_value()\n"
        )
        _user_id, project_id = self.create_source_project("async-usage", content)
        with main.connect_db() as db:
            symbol = db.execute(
                "SELECT id FROM project_symbols WHERE project_id = ? AND qualified_name = 'no_value'",
                (project_id,),
            ).fetchone()
            task = load_function_analysis_task(db, int(symbol["id"]))
            persist_function_analysis(db, task, contract(returns=None))
            check_project_call_compatibility(db, project_id)
            rows = db.execute(
                """
                SELECT call.usage_kind, compatibility.status
                FROM project_calls AS call
                JOIN project_call_compatibility AS compatibility ON compatibility.call_id = call.id
                WHERE call.project_id = ? AND call.callee = 'no_value'
                ORDER BY call.start_byte
                """,
                (project_id,),
            ).fetchall()

        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("statement", "compatible"),
                ("argument", "compatible"),
                ("assignment", "incompatible"),
            ],
        )

    def test_variable_receiver_method_call_does_not_resolve_to_unrelated_function(self) -> None:
        user_id = self.create_user("receiver-method")
        project_id = "receiver-method-project"
        files = {
            "main.py": (
                b"def caller(content):\n"
                b"    return content.encode('utf-8', errors='ignore')\n"
            ),
            "export.py": b"def encode(value):\n    return value\n",
        }
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('receiver-chat', ?, 'Receiver')",
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO projects(id, user_id, chat_id, name, source_kind, file_count, total_bytes)
                VALUES (?, ?, 'receiver-chat', 'receiver', 'folder', 2, ?)
                """,
                (project_id, user_id, sum(map(len, files.values()))),
            )
            for path, content in files.items():
                db.execute(
                    """
                    INSERT INTO project_files(project_id, path, content, size_bytes, sha256, is_binary)
                    VALUES (?, ?, ?, ?, ?, 0)
                    """,
                    (project_id, path, content, len(content), hashlib.sha256(content).hexdigest()),
                )
            inventory_project_database(db, project_id)
            parse_project_database(db, project_id)
            check_project_call_compatibility(db, project_id)
            row = db.execute(
                """
                SELECT call.resolution_status, call.resolved_symbol_id,
                       compatibility.status, finding.finding_kind, finding.message
                FROM project_calls AS call
                JOIN project_call_compatibility AS compatibility
                  ON compatibility.call_id = call.id
                JOIN project_call_findings AS finding ON finding.call_id = call.id
                WHERE call.callee = 'content.encode'
                """
            ).fetchone()

        self.assertEqual(row["resolution_status"], "unresolved")
        self.assertIsNone(row["resolved_symbol_id"])
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["finding_kind"], "dynamic_method_call")
        self.assertIn("Dynamic receiver method / not checked", row["message"])

    def test_imported_external_call_is_reported_as_not_checked(self) -> None:
        user_id = self.create_user("external-call")
        project_id = "external-call-project"
        content = (
            b"from pydantic import ConfigDict\n\n"
            b"class StrictAnalysisModel:\n"
            b"    model_config = ConfigDict(extra='forbid')\n"
        )
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('external-chat', ?, 'External')",
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO projects(id, user_id, chat_id, name, source_kind, file_count, total_bytes)
                VALUES (?, ?, 'external-chat', 'external', 'folder', 1, ?)
                """,
                (project_id, user_id, len(content)),
            )
            db.execute(
                """
                INSERT INTO project_files(project_id, path, content, size_bytes, sha256, is_binary)
                VALUES (?, 'external.py', ?, ?, ?, 0)
                """,
                (project_id, content, len(content), hashlib.sha256(content).hexdigest()),
            )
            inventory_project_database(db, project_id)
            parse_project_database(db, project_id)
            check_project_call_compatibility(db, project_id)
            row = db.execute(
                """
                SELECT finding.finding_kind, finding.message
                FROM project_call_findings AS finding
                JOIN project_calls AS call ON call.id = finding.call_id
                WHERE call.callee = 'ConfigDict'
                """
            ).fetchone()

        self.assertEqual(row["finding_kind"], "external_call")
        self.assertIn("External call / not checked", row["message"])

    def test_unresolved_calls_are_classified_by_likely_target_type(self) -> None:
        user_id = self.create_user("unresolved-kinds")
        project_id = "unresolved-kinds-project"
        content = (
            b"import json as json_lib\n\n"
            b"def factory():\n"
            b"    return lambda value: value\n\n"
            b"def caller(values):\n"
            b"    count = len(values)\n"
            b"    decoded = json_lib.loads('[]')\n"
            b"    generated = factory()(count)\n"
            b"    missing = expected_internal(count)\n"
            b"    return decoded, generated, missing\n"
        )
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('unresolved-chat', ?, 'Unresolved')",
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO projects(id, user_id, chat_id, name, source_kind, file_count, total_bytes)
                VALUES (?, ?, 'unresolved-chat', 'unresolved', 'folder', 1, ?)
                """,
                (project_id, user_id, len(content)),
            )
            db.execute(
                """
                INSERT INTO project_files(project_id, path, content, size_bytes, sha256, is_binary)
                VALUES (?, 'unresolved.py', ?, ?, ?, 0)
                """,
                (project_id, content, len(content), hashlib.sha256(content).hexdigest()),
            )
            inventory_project_database(db, project_id)
            parse_project_database(db, project_id)
            check_project_call_compatibility(db, project_id)
            rows = db.execute(
                """
                SELECT call.callee, finding.finding_kind, finding.message
                FROM project_call_findings AS finding
                JOIN project_calls AS call ON call.id = finding.call_id
                WHERE call.project_id = ?
                ORDER BY call.start_line, call.start_column
                """,
                (project_id,),
            ).fetchall()

        by_callee = {str(row["callee"]): row for row in rows}
        self.assertEqual(by_callee["len"]["finding_kind"], "builtin_call")
        self.assertIn("Builtin call / not checked", by_callee["len"]["message"])
        self.assertEqual(by_callee["json_lib.loads"]["finding_kind"], "external_call")
        self.assertIn("External call / not checked", by_callee["json_lib.loads"]["message"])
        self.assertEqual(by_callee["factory()"]["finding_kind"], "dynamic_call")
        self.assertIn("Dynamic call / not checked", by_callee["factory()"]["message"])
        self.assertEqual(
            by_callee["expected_internal"]["finding_kind"],
            "unresolved_internal_call",
        )
        self.assertIn(
            "Unresolved internal call",
            by_callee["expected_internal"]["message"],
        )

        with main.connect_db() as db:
            scopes = {
                str(row["callee"]): str(row["scope_status"])
                for row in db.execute(
                    """
                    SELECT call.callee, compatibility.scope_status
                    FROM project_calls AS call
                    JOIN project_call_compatibility AS compatibility
                      ON compatibility.call_id = call.id
                    WHERE call.project_id = ?
                    """,
                    (project_id,),
                ).fetchall()
            }
            project = db.execute(
                """
                SELECT call_compatibility_status,
                       call_compatibility_unknown_count,
                       call_compatibility_not_checked_count
                FROM projects WHERE id = ?
                """,
                (project_id,),
            ).fetchone()
        self.assertEqual(scopes["len"], "out_of_scope")
        self.assertEqual(scopes["json_lib.loads"], "out_of_scope")
        self.assertEqual(scopes["factory()"], "out_of_scope")
        self.assertEqual(scopes["expected_internal"], "in_scope")
        self.assertEqual(project["call_compatibility_status"], "completed")
        self.assertEqual(project["call_compatibility_unknown_count"], 2)
        self.assertEqual(project["call_compatibility_not_checked_count"], 3)

    def test_out_of_scope_calls_do_not_make_a_project_partial(self) -> None:
        user_id = self.create_user("out-of-scope")
        project_id = "out-of-scope-project"
        content = b"def count(values):\n    print(len(values))\n"
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) "
                "VALUES ('out-of-scope-chat', ?, 'Out of scope')",
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO projects(id, user_id, chat_id, name, source_kind, file_count, total_bytes)
                VALUES (?, ?, 'out-of-scope-chat', 'scope', 'folder', 1, ?)
                """,
                (project_id, user_id, len(content)),
            )
            db.execute(
                """
                INSERT INTO project_files(project_id, path, content, size_bytes, sha256, is_binary)
                VALUES (?, 'scope.py', ?, ?, ?, 0)
                """,
                (project_id, content, len(content), hashlib.sha256(content).hexdigest()),
            )
            inventory_project_database(db, project_id)
            parse_project_database(db, project_id)
            summary = check_project_call_compatibility(db, project_id)

        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.unknown_count, 0)
        self.assertEqual(summary.not_checked_count, 2)
        response = main.get_project_compatibility(
            project_id,
            self.authenticated_request(
                user_id,
                path=f"/api/projects/{project_id}/compatibility",
            ),
        )
        self.assertEqual(
            {call["status"] for call in response["calls"]},
            {"not_checked"},
        )
        self.assertEqual(
            response["project"]["call_compatibility_not_checked_count"],
            2,
        )
        self.assertEqual(response["project"]["call_compatibility_in_scope_count"], 0)
        self.assertEqual(response["project"]["call_compatibility_resolved_count"], 0)
        self.assertEqual(response["project"]["call_compatibility_coverage_percent"], 100.0)

        with main.connect_db() as db:
            db.execute(
                """
                UPDATE project_call_compatibility
                SET scope_status = 'in_scope'
                WHERE project_id = ?
                """,
                (project_id,),
            )
            db.execute(
                """
                UPDATE projects
                SET call_compatibility_status = 'partial',
                    call_compatibility_unknown_count = 2,
                    call_compatibility_not_checked_count = 0
                WHERE id = ?
                """,
                (project_id,),
            )
            migration_030_analysis_signal_metrics(db)
            backfilled = db.execute(
                """
                SELECT call_compatibility_status,
                       call_compatibility_unknown_count,
                       call_compatibility_not_checked_count
                FROM projects WHERE id = ?
                """,
                (project_id,),
            ).fetchone()
        self.assertEqual(tuple(backfilled), ("completed", 0, 2))

    def test_ambiguous_targets_remain_unknown_and_api_is_owner_scoped(self) -> None:
        user_id = self.create_user("ambiguous")
        project_id = "ambiguous-project"
        files = {
            "a.py": b"def duplicate():\n    return 1\n",
            "b.py": b"def duplicate():\n    return 2\n",
            "main.py": b"duplicate()\n",
        }
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('amb-chat', ?, 'Ambiguous')",
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO projects(id, user_id, chat_id, name, source_kind, file_count, total_bytes)
                VALUES (?, ?, 'amb-chat', 'ambiguous', 'folder', 3, ?)
                """,
                (project_id, user_id, sum(map(len, files.values()))),
            )
            for path, content in files.items():
                db.execute(
                    """
                    INSERT INTO project_files(project_id, path, content, size_bytes, sha256, is_binary)
                    VALUES (?, ?, ?, ?, ?, 0)
                    """,
                    (project_id, path, content, len(content), hashlib.sha256(content).hexdigest()),
                )
            inventory_project_database(db, project_id)
            parse_project_database(db, project_id)
            call = db.execute(
                "SELECT resolution_status, resolved_symbol_id FROM project_calls"
            ).fetchone()
            summary = check_project_call_compatibility(db, project_id)
        self.assertEqual(tuple(call), ("ambiguous", None))
        self.assertEqual(summary.unknown_count, 1)

        result = main.get_project_compatibility(
            project_id,
            self.authenticated_request(
                user_id, path=f"/api/projects/{project_id}/compatibility"
            ),
        )
        self.assertEqual(result["calls"][0]["status"], "unknown")
        stranger_id = self.create_user("compat-stranger")
        with self.assertRaises(main.HTTPException) as hidden:
            main.get_project_compatibility(
                project_id,
                self.authenticated_request(
                    stranger_id, path=f"/api/projects/{project_id}/compatibility"
                ),
            )
        self.assertEqual(hidden.exception.status_code, 404)

    def test_analysis_report_is_bounded_nested_and_never_returns_source(self) -> None:
        user_id, project_id = self.create_project()
        with main.connect_db() as db:
            check_project_call_compatibility(db, project_id)
        request = self.authenticated_request(
            user_id, path=f"/api/projects/{project_id}/analysis-report"
        )
        with patch.object(main, "PROJECT_REPORT_PAGE_SIZE", 1):
            first = main.get_project_analysis_report(project_id, request)
        self.assertEqual(len(first["functions"]), 1)
        self.assertEqual(len(first["calls"]), 1)
        self.assertEqual(first["next_function_offset"], 1)
        self.assertEqual(first["next_call_offset"], 1)
        function = first["functions"][0]
        self.assertIn("parameters", function)
        self.assertIn("return_types", function)
        self.assertIn("issues", function)
        self.assertIn("advisories", function)
        self.assertIn("raised_errors", function)
        self.assertIn("side_effects", function)
        self.assertNotIn("content", function)
        self.assertNotIn("source", function)
        self.assertIn("findings", first["calls"][0])
        self.assertIn("source_integrity", first)
        self.assertNotIn("content", first["source_integrity"]["files"][0])
        self.assertNotIn("source", first["source_integrity"]["files"][0])
        self.assertEqual(first["source_integrity"]["status"], "stored_only")
        self.assertEqual(
            first["source_integrity"]["files"][0]["workspace_status"],
            "not_linked",
        )
        self.assertIn(
            "no linked workspace file",
            first["source_integrity"]["files"][0]["workspace_note"],
        )

        with main.connect_db() as db:
            symbol_id = db.execute(
                """
                SELECT id FROM project_symbols
                WHERE project_id = ? AND qualified_name = ?
                """,
                (project_id, function["qualified_name"]),
            ).fetchone()["id"]
            db.execute(
                """
                INSERT INTO project_symbol_issues(
                    symbol_id, ordinal, severity, category, title,
                    description, start_line, end_line, provenance
                ) VALUES (?, 0, 'unsafe', 'logic', 'Provenance check',
                         'Report should expose provenance.', 1, 1, 'deterministic')
                """,
                (symbol_id,),
            )
            db.execute(
                """
                UPDATE project_symbol_analyses
                SET raised_errors_json = '["ValueError"]'
                WHERE symbol_id = ?
                """,
                (symbol_id,),
            )
            db.execute(
                """
                INSERT INTO project_symbol_issues(
                    symbol_id, ordinal, severity, category, title,
                    description, start_line, end_line, provenance,
                    proof, evidence, failure_type, trigger, report_tier
                ) VALUES (?, 1, 'warning', 'maintainability', 'Advisory check',
                         'Report should collapse this advisory.', 1, 1, 'model',
                         'source-v1', 'def caller', 'Maintainability concern',
                         'A programmer reviews this function.', 'advisory')
                """,
                (symbol_id,),
            )
        with patch.object(main, "PROJECT_REPORT_PAGE_SIZE", 1):
            with_issue = main.get_project_analysis_report(project_id, request)
        self.assertEqual(
            with_issue["functions"][0]["issues"][0]["provenance"],
            "deterministic",
        )
        self.assertEqual(
            with_issue["functions"][0]["advisories"][0]["title"],
            "Advisory check",
        )
        self.assertEqual(
            with_issue["functions"][0]["advisories"][0]["failure_type"],
            "Maintainability concern",
        )
        self.assertEqual(with_issue["functions"][0]["raised_errors"], ["ValueError"])

        stranger_id = self.create_user("report-stranger")
        with self.assertRaises(main.HTTPException) as hidden:
            main.get_project_analysis_report(
                project_id,
                self.authenticated_request(
                    stranger_id, path=f"/api/projects/{project_id}/analysis-report"
                ),
            )
        self.assertEqual(hidden.exception.status_code, 404)

    def test_analysis_report_omits_function_issue_duplicated_by_call_finding(self) -> None:
        user_id, project_id = self.create_source_project(
            "deduplicated-report",
            (
                b"def target(value):\n"
                b"    return value\n"
                b"\n"
                b"def caller():\n"
                b"    return target(value=1, extra=True)\n"
            ),
        )
        with main.connect_db() as db:
            caller = db.execute(
                "SELECT id FROM project_symbols WHERE project_id = ? AND qualified_name = 'caller'",
                (project_id,),
            ).fetchone()
            call = db.execute(
                "SELECT id FROM project_calls WHERE project_id = ? AND start_line = 5",
                (project_id,),
            ).fetchone()
            caller_task = load_function_analysis_task(db, int(caller["id"]))
            persist_function_analysis(db, caller_task, contract(returns="object"))
            db.execute(
                """
                INSERT INTO project_symbol_issues(
                    symbol_id, ordinal, severity, category, title,
                    description, start_line, end_line, provenance
                ) VALUES (?, 0, 'error', 'type', 'Unexpected keyword argument',
                          'Duplicate function-level call issue.', 5, 5, 'deterministic')
                """,
                (caller["id"],),
            )
            db.execute(
                """
                INSERT INTO project_call_compatibility(
                    call_id, project_id, caller_symbol_id, callee_symbol_id,
                    status, argument_status, return_status, scope_status
                )
                SELECT id, project_id, caller_symbol_id, resolved_symbol_id,
                       'incompatible', 'incompatible', 'unknown', 'in_scope'
                FROM project_calls WHERE id = ?
                """,
                (call["id"],),
            )
            db.execute(
                """
                INSERT INTO project_call_findings(
                    call_id, ordinal, severity, finding_kind, message
                ) VALUES (?, 0, 'error', 'unexpected_keyword',
                          'Unexpected keyword argument extra.')
                """,
                (call["id"],),
            )

        report = main.get_project_analysis_report(
            project_id,
            self.authenticated_request(
                user_id, path=f"/api/projects/{project_id}/analysis-report"
            ),
        )
        caller_report = next(
            item for item in report["functions"] if item["qualified_name"] == "caller"
        )
        self.assertEqual(caller_report["issues"], [])
        call_report = next(item for item in report["calls"] if item["start_line"] == 5)
        self.assertEqual(
            [item["finding_kind"] for item in call_report["findings"]],
            ["unexpected_keyword"],
        )

    def test_analysis_report_flags_changed_matching_workspace_source(self) -> None:
        user_id, project_id = self.create_project()
        request = self.authenticated_request(
            user_id, path=f"/api/projects/{project_id}/analysis-report"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "calls.py").write_text("def changed():\n    return 1\n", encoding="utf-8")
            with patch.object(main, "SOURCE_INTEGRITY_WORKSPACE_ROOT", root):
                report = main.get_project_analysis_report(project_id, request)

        integrity = report["source_integrity"]
        self.assertEqual(integrity["status"], "warning")
        self.assertEqual(integrity["warning_count"], 1)
        self.assertEqual(integrity["files"][0]["workspace_status"], "changed")
        self.assertEqual(integrity["files"][0]["workspace_path"], "calls.py")
        self.assertNotEqual(
            integrity["files"][0]["stored_sha256"],
            integrity["files"][0]["workspace_sha256"],
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
