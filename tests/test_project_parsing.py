from __future__ import annotations

import hashlib

from analysis_engine import (
    FunctionAnalysisResult,
    FunctionParameterContract,
    FunctionReturnContract,
    FunctionReturnType,
)
import main
from language_adapters import adapter_statuses, get_adapter
from project_call_compatibility import check_project_call_compatibility
from project_function_analysis import analyze_project_functions
from project_inventory import inventory_project_database
from project_parsing import parse_project_database
from tests.helpers import DatabaseTestCase


class LanguageAdapterTests(DatabaseTestCase):
    def test_registry_exposes_all_installed_grammars(self) -> None:
        statuses = {status.language: status for status in adapter_statuses()}
        self.assertEqual(
            set(statuses),
            {
                "python", "cpp", "csharp", "javascript", "typescript",
                "rust", "shell", "powershell", "html", "sql",
            },
        )
        self.assertTrue(all(status.available for status in statuses.values()))
        self.assertEqual(statuses["python"].grammar_version, "0.25.0")
        self.assertEqual(statuses["cpp"].grammar_version, "0.23.4")
        self.assertTrue(statuses["python"].capabilities.definitions)
        self.assertIs(get_adapter("c++"), get_adapter("cpp"))
        self.assertIs(get_adapter("c"), get_adapter("cpp"))
        self.assertIs(get_adapter("js"), get_adapter("javascript"))
        self.assertIs(get_adapter("ps1"), get_adapter("powershell"))
        self.assertIsNone(get_adapter("pascal"))

    def test_additional_adapters_extract_functions_dependencies_and_calls(self) -> None:
        samples = {
            "javascript": ("import x from './x.js'; function run(v){return helper(v)}", "run", "helper"),
            "typescript": ("class Box { run(v: number){ return helper(v); } }", "Box.run", "helper"),
            "csharp": ("using Demo.Tools; class Box { int Run(int v){ return Helper(v); } }", "Box.Run", "Helper"),
            "rust": ("use crate::helper::run; fn top(v:i32)->i32 { run(v) }", "top", "run"),
            "shell": ("source ./helpers.sh\nrun() { helper \"$1\"; }\n", "run", "helper"),
            "powershell": ("Import-Module ./Helpers.psm1\nfunction Invoke-Thing($Value) { Get-Result $Value }\n", "Invoke-Thing", "Get-Result"),
            "html": ("<html>\n<script>\nfunction run(v){ return helper(v); }\n</script>\n</html>", "run", "helper"),
            "sql": ("CREATE FUNCTION add_one(value integer) RETURNS integer AS $$ SELECT value + 1 $$ LANGUAGE SQL; SELECT add_one(1);", "add_one", "add_one"),
        }
        for language, (text, definition_name, callee) in samples.items():
            with self.subTest(language=language):
                adapter = get_adapter(language)
                structure = adapter.extract_structure(adapter.parse_text(text), text)
                self.assertIn(definition_name, [item.qualified_name for item in structure.definitions])
                self.assertIn(callee, [item.callee for item in structure.calls])
        html = get_adapter("html").extract_structure(
            get_adapter("html").parse_text(samples["html"][0]), samples["html"][0]
        )
        self.assertEqual(html.definitions[0].span.start_line, 3)
        tsx_text = "const App=()=> <div>{helper(1)}</div>;"
        tsx_adapter = get_adapter("typescript")
        tsx_structure = tsx_adapter.extract_structure(
            tsx_adapter.parse_text(tsx_text), tsx_text
        )
        self.assertFalse(tsx_adapter.parse_text(tsx_text).has_syntax_errors)
        self.assertEqual(tsx_structure.definitions[0].name, "App")
        self.assertEqual(tsx_structure.calls[0].callee, "helper")

    def test_adapters_report_missing_and_error_nodes_with_one_based_lines(self) -> None:
        valid_python = get_adapter("python").parse_text("def answer():\n    return 42\n")
        self.assertFalse(valid_python.has_syntax_errors)
        self.assertEqual(valid_python.diagnostics, ())

        invalid_python = get_adapter("python").parse_text("def broken(:\n    pass\n")
        self.assertTrue(invalid_python.has_syntax_errors)
        self.assertEqual(invalid_python.error_count, 1)
        self.assertEqual(invalid_python.missing_count, 0)
        self.assertEqual(invalid_python.diagnostics[0].kind, "error")
        self.assertEqual(invalid_python.diagnostics[0].start_line, 1)

        invalid_cpp = get_adapter("cpp").parse_text("int main( { return 0; }")
        self.assertTrue(invalid_cpp.has_syntax_errors)
        self.assertGreaterEqual(invalid_cpp.error_count, 1)

    def test_python_fallback_structure_preserves_function_bodies_after_syntax_error(self) -> None:
        text = (
            "def first():\n"
            "    value = missing_name\n"
            "    helper(value)\n"
            "    return value\n"
            "\n"
            "def broken(value):\n"
            "    if value > 0\n"
            "        return value\n"
            "    return 0\n"
            "\n"
            "def multiline(\n"
            "    value: int,\n"
            ") -> int:\n"
            "    return helper(value)\n"
        )
        adapter = get_adapter("python")
        structure = adapter.extract_structure(adapter.parse_text(text), text)
        definitions = {item.qualified_name: item for item in structure.definitions}

        self.assertEqual(definitions["first"].span.start_line, 1)
        self.assertEqual(definitions["first"].span.end_line, 5)
        self.assertIn(
            "return value",
            text.encode("utf-8")[
                definitions["first"].span.start_byte:definitions["first"].span.end_byte
            ].decode("utf-8"),
        )
        self.assertEqual(definitions["broken"].span.start_line, 6)
        self.assertEqual(definitions["broken"].span.end_line, 10)
        self.assertIn(
            "if value > 0",
            text.encode("utf-8")[
                definitions["broken"].span.start_byte:definitions["broken"].span.end_byte
            ].decode("utf-8"),
        )
        self.assertEqual(definitions["multiline"].span.start_line, 11)
        self.assertEqual(definitions["multiline"].span.end_line, 14)
        self.assertIn(
            ") -> int:",
            text.encode("utf-8")[
                definitions["multiline"].span.start_byte:definitions["multiline"].span.end_byte
            ].decode("utf-8"),
        )
        calls = [(item.callee, item.caller_index, item.span.start_line) for item in structure.calls]
        self.assertEqual(calls, [("helper", 0, 3), ("helper", 2, 14)])

    def test_python_structure_preserves_nesting_dependencies_and_call_owners(self) -> None:
        text = (
            "import os, sys as system\n"
            "from .helpers import convert as cv\n"
            "class Box:\n"
            "    def method(self, value):\n"
            "        def nested():\n"
            "            return cv(value)\n"
            "        return nested()\n"
        )
        adapter = get_adapter("python")
        structure = adapter.extract_structure(adapter.parse_text(text), text)

        self.assertEqual(
            [(item.kind, item.qualified_name, item.parent_index) for item in structure.definitions],
            [
                ("class", "Box", None),
                ("method", "Box.method", 0),
                ("function", "Box.method.nested", 1),
            ],
        )
        self.assertEqual(structure.definitions[0].span.start_line, 3)
        self.assertEqual(structure.definitions[2].span.end_line, 6)
        self.assertEqual(
            [(item.kind, item.module_name, item.imported_names) for item in structure.dependencies],
            [
                ("import", "os", ()),
                ("import", "sys", ("system",)),
                ("import_from", ".helpers", ("cv",)),
            ],
        )
        self.assertEqual(
            [(item.callee, item.caller_index) for item in structure.calls],
            [("cv", 2), ("nested", 1)],
        )

    def test_cpp_structure_handles_includes_methods_and_pointer_declarators(self) -> None:
        text = (
            '#include <vector>\n#include "local.hpp"\n'
            "class Box { public: int method(int x) { return helper(x); } };\n"
            "int Box::outside(int y) { return method(y); }\n"
            "static int *pointer_fn() { return 0; }\n"
        )
        adapter = get_adapter("cpp")
        structure = adapter.extract_structure(adapter.parse_text(text), text)

        self.assertEqual(
            [(item.kind, item.qualified_name, item.parent_index) for item in structure.definitions],
            [
                ("class", "Box", None),
                ("method", "Box::method", 0),
                ("method", "Box::outside", None),
                ("function", "pointer_fn", None),
            ],
        )
        self.assertEqual(
            [(item.module_name, item.is_relative) for item in structure.dependencies],
            [("vector", False), ("local.hpp", True)],
        )
        self.assertEqual(
            [(item.callee, item.caller_index) for item in structure.calls],
            [("helper", 1), ("method", 2)],
        )

    def test_call_arguments_and_result_context_are_extracted_deterministically(self) -> None:
        python_text = (
            "def run():\n"
            "    value: int = target(1, name='x')\n"
            "    return value\n"
        )
        python_adapter = get_adapter("python")
        python_call = python_adapter.extract_structure(
            python_adapter.parse_text(python_text), python_text
        ).calls[0]
        self.assertEqual(python_call.usage_kind, "assignment")
        self.assertEqual(python_call.expected_return_types, ("int",))
        self.assertEqual(
            [
                (argument.keyword_name, argument.expression_kind, argument.inferred_types)
                for argument in python_call.arguments
            ],
            [(None, "integer", ("int",)), ("name", "keyword_argument", ("str",))],
        )

        cpp_text = 'int run() { int value = target(1, "x"); return value; }\n'
        cpp_adapter = get_adapter("cpp")
        cpp_call = cpp_adapter.extract_structure(
            cpp_adapter.parse_text(cpp_text), cpp_text
        ).calls[0]
        self.assertEqual(cpp_call.usage_kind, "assignment")
        self.assertEqual(cpp_call.expected_return_types, ("int",))
        self.assertEqual(
            [argument.inferred_types for argument in cpp_call.arguments],
            [("int",), ("string",)],
        )

    def test_python_await_usage_preserves_the_enclosing_value_context(self) -> None:
        text = (
            "async def no_value():\n"
            "    pass\n\n"
            "async def run():\n"
            "    await no_value()\n"
            "    task = create_task(no_value())\n"
            "    result = await no_value()\n"
        )
        adapter = get_adapter("python")
        calls = adapter.extract_structure(adapter.parse_text(text), text).calls

        no_value_usages = [
            call.usage_kind for call in calls if call.callee == "no_value"
        ]
        self.assertEqual(
            no_value_usages,
            ["statement", "argument", "assignment"],
        )

    def test_python_call_arguments_use_conservative_local_type_flow(self) -> None:
        source = (
            "def produce(raw: str) -> str:\n"
            "    return raw.strip()\n\n"
            "def target(text: str, count: int, names: list[str], flag: bool):\n"
            "    pass\n\n"
            "def run(raw: str, values: list[str], count: int):\n"
            "    normalized = produce(raw)\n"
            "    names = [item.strip() for item in values]\n"
            "    target(f'{normalized}', count + 1, names, count > 0)\n"
            "    target(later, count, names, True)\n"
            "    later = 'assigned too late'\n"
            "    if count:\n"
            "        conditional = 'not definite'\n"
            "    target(conditional, count, names, False)\n"
            "    maybe = produce(raw) if count else unknown_factory()\n"
            "    target(maybe, count, names, False)\n"
        )
        adapter = get_adapter("python")
        calls = [
            call
            for call in adapter.extract_structure(
                adapter.parse_text(source),
                source,
            ).calls
            if call.callee == "target"
        ]

        self.assertEqual(
            [argument.inferred_types for argument in calls[0].arguments],
            [("str",), ("int",), ("list[str]",), ("bool",)],
        )
        self.assertEqual(calls[1].arguments[0].inferred_types, ())
        self.assertEqual(calls[2].arguments[0].inferred_types, ())
        self.assertEqual(calls[3].arguments[0].inferred_types, ())


class ProjectParsingPersistenceTests(DatabaseTestCase):
    def test_supported_files_are_parsed_and_other_languages_remain_visible(self) -> None:
        user_id = self.create_user("parser")
        files = {
            "demo/main.py": b"def broken(:\n    pass\n",
            "demo/web.js": b"export function ready() { return true; }\n",
        }
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('parser-chat', ?, 'Parser')",
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO projects(
                    id, user_id, chat_id, name, source_kind, file_count, total_bytes
                ) VALUES ('parser-project', ?, 'parser-chat', 'demo', 'folder', 2, ?)
                """,
                (user_id, sum(map(len, files.values()))),
            )
            for path, content in files.items():
                db.execute(
                    """
                    INSERT INTO project_files(
                        project_id, path, content, size_bytes, sha256, is_binary
                    ) VALUES ('parser-project', ?, ?, ?, ?, 0)
                    """,
                    (path, content, len(content), hashlib.sha256(content).hexdigest()),
                )
            inventory_project_database(db, "parser-project")
            summary = parse_project_database(db, "parser-project")
            project = db.execute(
                """
                SELECT parser_status, parser_supported_file_count,
                       parser_parsed_file_count, parser_syntax_error_file_count,
                       structure_status, indexed_file_count, definition_count
                FROM projects WHERE id = 'parser-project'
                """
            ).fetchone()
            rows = db.execute(
                """
                SELECT path, parser_status, parser_adapter, parser_missing_count,
                       parser_source_sha256, parser_diagnostics_json
                FROM project_files WHERE project_id = 'parser-project' ORDER BY path
                """
            ).fetchall()

        self.assertEqual(summary.status, "completed")
        self.assertEqual(tuple(project), ("completed", 2, 2, 1, "completed", 2, 2))
        self.assertEqual(rows[0]["parser_status"], "syntax_error")
        self.assertEqual(rows[0]["parser_adapter"], "python")
        self.assertEqual(rows[0]["parser_missing_count"], 0)
        self.assertEqual(len(rows[0]["parser_source_sha256"]), 64)
        self.assertIn('"start_line":1', rows[0]["parser_diagnostics_json"])
        self.assertEqual(rows[1]["parser_status"], "parsed")
        self.assertEqual(rows[1]["parser_adapter"], "javascript")

    def test_persisted_fixture_matrix_covers_every_supported_language(self) -> None:
        user_id = self.create_user("language-fixtures")
        files = {
            "fixture.py": b"def run(value):\n    return helper(value)\n",
            "fixture.cpp": b'#include "helper.hpp"\nint run(int value) { return helper(value); }\n',
            "fixture.cs": b"using Demo.Tools; class Box { int Run(int value) { return Helper(value); } }\n",
            "fixture.js": b"import helper from './helper.js'; export function run(value) { return helper(value); }\n",
            "fixture.ts": b"import { helper } from './helper'; export function run(value: number): number { return helper(value); }\n",
            "fixture.rs": b"use crate::helper::run_helper; fn run(value: i32) -> i32 { run_helper(value) }\n",
            "fixture.sh": b"source ./helpers.sh\nrun() { helper \"$1\"; }\n",
            "fixture.ps1": b"Import-Module ./Helpers.psm1\nfunction Invoke-Thing($Value) { Get-Result $Value }\n",
            "fixture.html": b"<html>\n<script>\nfunction run(value) { return helper(value); }\n</script>\n</html>\n",
            "fixture.sql": b"CREATE FUNCTION add_one(value integer) RETURNS integer AS $$ SELECT value + 1 $$ LANGUAGE SQL; SELECT add_one(1);\n",
        }
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES "
                "('language-fixtures-chat', ?, 'Language fixtures')",
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO projects(id, user_id, chat_id, name, source_kind, file_count, total_bytes)
                VALUES ('language-fixtures-project', ?, 'language-fixtures-chat', 'fixtures', 'folder', ?, ?)
                """,
                (user_id, len(files), sum(len(content) for content in files.values())),
            )
            for path, content in files.items():
                db.execute(
                    """
                    INSERT INTO project_files(project_id, path, content, size_bytes, sha256, is_binary)
                    VALUES ('language-fixtures-project', ?, ?, ?, ?, 0)
                    """,
                    (path, content, len(content), hashlib.sha256(content).hexdigest()),
                )
            inventory_project_database(db, "language-fixtures-project")
            summary = parse_project_database(db, "language-fixtures-project")
            rows = db.execute(
                """
                SELECT path, language, parser_status, structure_status,
                       definition_count, dependency_count, call_count
                FROM project_files
                WHERE project_id = 'language-fixtures-project'
                ORDER BY path
                """
            ).fetchall()
            symbols = db.execute(
                """
                SELECT symbol.start_line, symbol.end_line, symbol.start_byte,
                       symbol.end_byte, file.path
                FROM project_symbols AS symbol
                JOIN project_files AS file ON file.id = symbol.file_id
                WHERE symbol.project_id = 'language-fixtures-project'
                  AND symbol.symbol_kind IN ('function', 'method')
                """
            ).fetchall()

        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.structure_status, "completed")
        self.assertEqual(len(rows), len(files))
        self.assertEqual(
            {str(row["language"]) for row in rows},
            {"python", "cpp", "csharp", "javascript", "typescript", "rust", "shell", "powershell", "html", "sql"},
        )
        self.assertTrue(all(row["parser_status"] == "parsed" for row in rows))
        self.assertTrue(all(row["structure_status"] == "indexed" for row in rows))
        self.assertTrue(all(int(row["definition_count"]) >= 1 for row in rows))
        self.assertTrue(all(int(row["call_count"]) >= 1 for row in rows))
        self.assertGreaterEqual(len(symbols), len(files))
        self.assertTrue(
            all(
                int(symbol["start_line"]) <= int(symbol["end_line"])
                and int(symbol["start_byte"]) < int(symbol["end_byte"])
                for symbol in symbols
            )
        )

    def test_reindex_replaces_derived_rows_and_resolves_parent_and_caller_ids(self) -> None:
        user_id = self.create_user("reindex")
        first = b"class Box:\n    def run(self):\n        return helper()\n"
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('reindex-chat', ?, 'Reindex')",
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO projects(id, user_id, chat_id, name, source_kind, file_count, total_bytes)
                VALUES ('reindex-project', ?, 'reindex-chat', 'demo', 'folder', 1, ?)
                """,
                (user_id, len(first)),
            )
            db.execute(
                """
                INSERT INTO project_files(project_id, path, content, size_bytes, sha256, is_binary)
                VALUES ('reindex-project', 'demo/main.py', ?, ?, ?, 0)
                """,
                (first, len(first), hashlib.sha256(first).hexdigest()),
            )
            inventory_project_database(db, "reindex-project")
            parse_project_database(db, "reindex-project")
            symbols = db.execute(
                """
                SELECT id, parent_symbol_id, qualified_name, start_line, end_line
                FROM project_symbols ORDER BY start_byte, end_byte DESC
                """
            ).fetchall()
            call = db.execute(
                "SELECT caller_symbol_id, callee FROM project_calls"
            ).fetchone()
            self.assertEqual(len(symbols), 2)
            self.assertEqual(symbols[1]["parent_symbol_id"], symbols[0]["id"])
            self.assertEqual(symbols[1]["qualified_name"], "Box.run")
            self.assertEqual(call["caller_symbol_id"], symbols[1]["id"])

            second = b"def replacement():\n    return 1\n"
            db.execute(
                """
                UPDATE project_files
                SET content = ?, size_bytes = ?, sha256 = ?
                WHERE project_id = 'reindex-project'
                """,
                (second, len(second), hashlib.sha256(second).hexdigest()),
            )
            inventory_project_database(db, "reindex-project")
            summary = parse_project_database(db, "reindex-project")
            names = [
                row[0]
                for row in db.execute(
                    "SELECT qualified_name FROM project_symbols ORDER BY start_byte"
                ).fetchall()
            ]
            call_count = db.execute("SELECT COUNT(*) FROM project_calls").fetchone()[0]
        self.assertEqual(names, ["replacement"])
        self.assertEqual(call_count, 0)
        self.assertEqual(summary.definition_count, 1)

    def test_reindex_reuses_unchanged_contracts_and_rebuilds_downstream_calls(self) -> None:
        user_id = self.create_user("incremental-reindex")
        first = (
            b"def stable(value):\n    return value\n\n"
            b"def changed(value):\n    return value + 1\n\n"
            b"def caller():\n    return stable(1)\n"
        )
        second = first.replace(b"return value + 1", b"return value + 2")

        def contract() -> FunctionAnalysisResult:
            return FunctionAnalysisResult(
                contract_version="1.0",
                summary="Returns an integer.",
                syntax_valid=True,
                parameters=[
                    FunctionParameterContract(
                        name="value",
                        kind="positional_or_keyword",
                        required=True,
                        accepted_types=["int"],
                        description="An integer value.",
                    )
                ],
                returns=FunctionReturnContract(
                    may_return_value=True,
                    possible_types=[
                        FunctionReturnType(type="int", description="Integer result.")
                    ],
                    nullable=False,
                    description="Returns an integer.",
                ),
                confidence=0.9,
            )

        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES "
                "('incremental-chat', ?, 'Incremental')",
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO projects(id, user_id, chat_id, name, source_kind, file_count, total_bytes)
                VALUES ('incremental-project', ?, 'incremental-chat', 'demo', 'folder', 1, ?)
                """,
                (user_id, len(first)),
            )
            db.execute(
                """
                INSERT INTO project_files(project_id, path, content, size_bytes, sha256, is_binary)
                VALUES ('incremental-project', 'demo/main.py', ?, ?, ?, 0)
                """,
                (first, len(first), hashlib.sha256(first).hexdigest()),
            )
            inventory_project_database(db, "incremental-project")
            parse_project_database(db, "incremental-project")

        first_calls: list[str] = []
        analyze_project_functions(
            main.connect_db,
            "incremental-project",
            analysis_request=lambda **kwargs: (
                first_calls.append(kwargs["qualified_name"]) or contract()
            ),
        )
        with main.connect_db() as db:
            initial_compatibility = check_project_call_compatibility(
                db, "incremental-project"
            )
            initial_call_rows = db.execute(
                "SELECT COUNT(*) FROM project_call_compatibility "
                "WHERE project_id = 'incremental-project'"
            ).fetchone()[0]
            db.execute(
                """
                UPDATE project_files
                SET content = ?, size_bytes = ?, sha256 = ?
                WHERE project_id = 'incremental-project'
                """,
                (second, len(second), hashlib.sha256(second).hexdigest()),
            )
            inventory_project_database(db, "incremental-project")
            parse_project_database(db, "incremental-project")
            cleared_call_rows = db.execute(
                "SELECT COUNT(*) FROM project_call_compatibility "
                "WHERE project_id = 'incremental-project'"
            ).fetchone()[0]

        second_calls: list[str] = []
        second_summary = analyze_project_functions(
            main.connect_db,
            "incremental-project",
            analysis_request=lambda **kwargs: (
                second_calls.append(kwargs["qualified_name"]) or contract()
            ),
        )
        with main.connect_db() as db:
            rebuilt_compatibility = check_project_call_compatibility(
                db, "incremental-project"
            )
            final_call_rows = db.execute(
                "SELECT COUNT(*) FROM project_call_compatibility "
                "WHERE project_id = 'incremental-project'"
            ).fetchone()[0]

        self.assertEqual(set(first_calls), {"stable", "changed", "caller"})
        self.assertEqual(second_calls, ["changed"])
        self.assertEqual(second_summary.cache_hit_count, 2)
        self.assertEqual(initial_compatibility.checked_count, 1)
        self.assertEqual(initial_call_rows, 1)
        self.assertEqual(cleared_call_rows, 0)
        self.assertEqual(rebuilt_compatibility.checked_count, 1)
        self.assertEqual(final_call_rows, 1)

    def test_internal_python_dependency_resolves_to_uploaded_file(self) -> None:
        user_id = self.create_user("resolve")
        files = {
            "demo/pkg/main.py": b"from .helper import run\nimport os\nrun()\n",
            "demo/pkg/helper.py": b"def run():\n    return 1\n",
        }
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('resolve-chat', ?, 'Resolve')",
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO projects(id, user_id, chat_id, name, source_kind, file_count, total_bytes)
                VALUES ('resolve-project', ?, 'resolve-chat', 'demo', 'folder', 2, ?)
                """,
                (user_id, sum(map(len, files.values()))),
            )
            file_ids: dict[str, int] = {}
            for path, content in files.items():
                cursor = db.execute(
                    """
                    INSERT INTO project_files(project_id, path, content, size_bytes, sha256, is_binary)
                    VALUES ('resolve-project', ?, ?, ?, ?, 0)
                    """,
                    (path, content, len(content), hashlib.sha256(content).hexdigest()),
                )
                file_ids[path] = int(cursor.lastrowid)
            inventory_project_database(db, "resolve-project")
            parse_project_database(db, "resolve-project")
            dependencies = db.execute(
                """
                SELECT module_name, resolution_status, resolved_file_id
                FROM project_dependencies ORDER BY start_byte, module_name
                """
            ).fetchall()
            project = db.execute(
                """
                SELECT resolved_dependency_count, ambiguous_dependency_count
                FROM projects WHERE id = 'resolve-project'
                """
            ).fetchone()
        self.assertEqual(
            [tuple(row) for row in dependencies],
            [
                (".helper", "internal", file_ids["demo/pkg/helper.py"]),
                ("os", "external", None),
            ],
        )
        self.assertEqual(tuple(project), (1, 0))

    def test_javascript_and_rust_imports_disambiguate_same_named_functions(self) -> None:
        user_id = self.create_user("multi-resolve")
        files = {
            "demo/web/main.js": b"import { run } from './helper.js';\nfunction start(){ return run(1); }\n",
            "demo/web/helper.js": b"export function run(value){ return value; }\n",
            "demo/src/main.rs": b"use crate::helper::run;\nfn start(){ run(1); }\n",
            "demo/src/helper.rs": b"fn run(value:i32)->i32 { value }\n",
        }
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('multi-chat', ?, 'Multi')",
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO projects(id, user_id, chat_id, name, source_kind, file_count, total_bytes)
                VALUES ('multi-project', ?, 'multi-chat', 'demo', 'folder', 4, ?)
                """,
                (user_id, sum(map(len, files.values()))),
            )
            file_ids = {}
            for path, content in files.items():
                cursor = db.execute(
                    """
                    INSERT INTO project_files(project_id, path, content, size_bytes, sha256, is_binary)
                    VALUES ('multi-project', ?, ?, ?, ?, 0)
                    """,
                    (path, content, len(content), hashlib.sha256(content).hexdigest()),
                )
                file_ids[path] = int(cursor.lastrowid)
            inventory_project_database(db, "multi-project")
            parse_project_database(db, "multi-project")
            dependencies = db.execute(
                """
                SELECT file.path, dependency.resolution_status, resolved.path AS resolved_path
                FROM project_dependencies AS dependency
                JOIN project_files AS file ON file.id = dependency.file_id
                LEFT JOIN project_files AS resolved ON resolved.id = dependency.resolved_file_id
                ORDER BY file.path
                """
            ).fetchall()
            calls = db.execute(
                """
                SELECT file.path, call.resolution_status, target_file.path AS target_path
                FROM project_calls AS call
                JOIN project_files AS file ON file.id = call.file_id
                LEFT JOIN project_symbols AS target ON target.id = call.resolved_symbol_id
                LEFT JOIN project_files AS target_file ON target_file.id = target.file_id
                WHERE call.callee = 'run' ORDER BY file.path
                """
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in dependencies],
            [
                ("demo/src/main.rs", "internal", "demo/src/helper.rs"),
                ("demo/web/main.js", "internal", "demo/web/helper.js"),
            ],
        )
        self.assertEqual(
            [tuple(row) for row in calls],
            [
                ("demo/src/main.rs", "internal", "demo/src/helper.rs"),
                ("demo/web/main.js", "internal", "demo/web/helper.js"),
            ],
        )

    def test_powershell_and_sql_calls_resolve_case_insensitively(self) -> None:
        user_id = self.create_user("case-resolve")
        files = {
            "tools/demo.ps1": (
                b"function Invoke-Thing($Value) { return $Value }\ninvoke-thing 1\n"
            ),
            "db/demo.sql": (
                b"CREATE FUNCTION MixedCase(value integer) RETURNS integer AS $$ "
                b"SELECT value $$ LANGUAGE SQL; SELECT mixedcase(1);"
            ),
        }
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('case-chat', ?, 'Case')",
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO projects(id, user_id, chat_id, name, source_kind, file_count, total_bytes)
                VALUES ('case-project', ?, 'case-chat', 'case', 'folder', 2, ?)
                """,
                (user_id, sum(map(len, files.values()))),
            )
            for path, content in files.items():
                db.execute(
                    """
                    INSERT INTO project_files(project_id, path, content, size_bytes, sha256, is_binary)
                    VALUES ('case-project', ?, ?, ?, ?, 0)
                    """,
                    (path, content, len(content), hashlib.sha256(content).hexdigest()),
                )
            inventory_project_database(db, "case-project")
            parse_project_database(db, "case-project")
            rows = db.execute(
                """
                SELECT call.callee, call.resolution_status, target.name
                FROM project_calls AS call
                LEFT JOIN project_symbols AS target ON target.id = call.resolved_symbol_id
                WHERE lower(call.callee) IN ('invoke-thing', 'mixedcase')
                ORDER BY call.callee
                """
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("invoke-thing", "internal", "Invoke-Thing"),
                ("mixedcase", "internal", "MixedCase"),
            ],
        )

    def test_python_component_receiver_resolves_to_the_assigned_class(self) -> None:
        user_id = self.create_user("component-receiver")
        content = (
            b"class Memory:\n"
            b"    def resetMemory(self, length):\n        return length\n\n"
            b"class Brain:\n"
            b"    def __init__(self):\n        self.memory = Memory()\n"
            b"    def resetMemory(self):\n        return None\n"
            b"    def run(self):\n        return self.memory.resetMemory(3)\n"
        )
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('receiver-chat', ?, 'Receiver')",
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO projects(id, user_id, chat_id, name, source_kind, file_count, total_bytes)
                VALUES ('receiver-project', ?, 'receiver-chat', 'receiver', 'folder', 1, ?)
                """,
                (user_id, len(content)),
            )
            db.execute(
                """
                INSERT INTO project_files(project_id, path, content, size_bytes, sha256, is_binary)
                VALUES ('receiver-project', 'brain.py', ?, ?, ?, 0)
                """,
                (content, len(content), hashlib.sha256(content).hexdigest()),
            )
            inventory_project_database(db, "receiver-project")
            parse_project_database(db, "receiver-project")
            row = db.execute(
                """
                SELECT call.resolution_status, target.qualified_name
                FROM project_calls AS call
                LEFT JOIN project_symbols AS target ON target.id = call.resolved_symbol_id
                WHERE call.callee = 'self.memory.resetMemory'
                """
            ).fetchone()

        self.assertEqual(tuple(row), ("internal", "Memory.resetMemory"))


if __name__ == "__main__":
    import unittest

    unittest.main()
