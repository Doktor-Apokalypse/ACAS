from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import threading
from contextlib import closing
from unittest.mock import patch

from fastapi import HTTPException
from starlette.datastructures import UploadFile

import main
from api_models import ProjectEntryDelete, ProjectMainFile
from project_function_analysis import (
    ProjectFunctionAnalysisSummary,
    analyze_project_functions,
    load_function_analysis_task,
)
from project_uploads import UploadLimits
from migrations import migration_032_editable_project_tree
from tests.helpers import DatabaseTestCase, run_asgi_response
from tests.test_project_uploads import zip_bytes
from tests.test_function_analysis import valid_result


class ProjectTreeTests(DatabaseTestCase):
    def test_tree_tracks_function_processing_completion_and_pause(self):
        project = self.upload("files", {
            "main.py": b"def first(): return 1\ndef second(): return 2\n",
            "helper.py": b"def helper(): return 3\n",
            "notes.txt": b"No functions here",
        })
        def states():
            return {file["path"]: file for file in self.tree(project["id"])["files"]}
        initial = states()
        self.assertEqual(initial["main.py"]["function_count"], 2)
        self.assertEqual(initial["main.py"]["analysis_state"], "pending")
        self.assertEqual(initial["notes.txt"]["analysis_state"], "no_functions")
        with main.connect_db() as db:
            db.execute("UPDATE project_symbols SET analysis_status='processing' WHERE project_id=? AND name IN ('first','helper')", (project["id"],))
            db.execute("INSERT INTO chat_jobs(id,user_id,chat_id,project_id,job_kind,status,progress_stage) VALUES ('tree-progress',?,'tree-chat',?,'project_analysis','processing','analyzing_function_batch')", (self.user_id,project["id"]))
        active = states()
        self.assertEqual(active["main.py"]["analysis_state"], "analysing")
        self.assertEqual(active["helper.py"]["analysis_state"], "analysing")
        with main.connect_db() as db:
            db.execute("UPDATE project_symbols SET analysis_status='completed' WHERE project_id=? AND name='first'", (project["id"],))
        self.assertEqual(states()["main.py"]["analysis_state"], "pending", "One finished function does not finish a file")
        with main.connect_db() as db:
            db.execute(
                """UPDATE project_symbols
                   SET analysis_status='failed',
                       analysis_error='Incomplete model review: unresolved parameter type'
                   WHERE project_id=? AND name='second'""",
                (project["id"],),
            )
            db.execute(
                """INSERT INTO project_symbol_analyses(
                       symbol_id, project_id, file_id, contract_version, model_name,
                       source_sha256, response_json, summary, syntax_valid,
                       may_return_value, return_nullable, return_description,
                       raised_errors_json, side_effects_json, confidence
                   ) SELECT id, project_id, file_id, '1.0', 'test',
                            '0000000000000000000000000000000000000000000000000000000000000000',
                            '{}', '', 1, 0, 0, '', '[]', '[]', 1.0
                     FROM project_symbols WHERE project_id=? AND name IN ('first','second')""",
                (project["id"],),
            )
            db.execute(
                """INSERT INTO project_symbol_issues(
                       symbol_id, ordinal, severity, category, title, description,
                       start_line, end_line, provenance
                   ) SELECT id, 0, 'warning', 'logic', 'Warning', 'Warning detail',
                            start_line, start_line, 'deterministic'
                     FROM project_symbols WHERE project_id=? AND name='first'""",
                (project["id"],),
            )
            db.execute(
                """INSERT INTO project_symbol_issues(
                       symbol_id, ordinal, severity, category, title, description,
                       start_line, end_line, provenance
                   ) SELECT id, 0, 'error', 'runtime', 'Error', 'Error detail',
                            start_line, start_line, 'deterministic'
                     FROM project_symbols WHERE project_id=? AND name='second'""",
                (project["id"],),
            )
            db.execute("UPDATE chat_jobs SET progress_stage='paused' WHERE id='tree-progress'")
        paused_tree = self.tree(project["id"])
        paused = {file["path"]: file for file in paused_tree["files"]}
        self.assertEqual(paused["main.py"]["analysis_state"], "processed")
        self.assertEqual(paused["main.py"]["processed_function_count"], 2)
        self.assertEqual(paused["main.py"]["failed_function_count"], 1)
        self.assertEqual(paused["main.py"]["error_function_count"], 1)
        self.assertEqual(paused["main.py"]["warning_function_count"], 1)
        function_findings = {
            item["name"]: (item["error_count"], item["warning_count"])
            for item in paused_tree["functions"]
        }
        self.assertEqual(function_findings["first"], (0, 1))
        self.assertEqual(function_findings["second"], (1, 0))
        failed_function = next(
            item for item in paused_tree["functions"] if item["name"] == "second"
        )
        self.assertEqual(
            failed_function["analysis_error"],
            "Incomplete model review: unresolved parameter type",
        )
        self.assertEqual(paused["helper.py"]["analysis_state"], "paused")
        with main.connect_db() as db:
            db.execute("UPDATE chat_jobs SET status='failed' WHERE id='tree-progress'")
        self.assertEqual(states()["helper.py"]["analysis_state"], "pending", "Cancelled work must not remain red or get a completion tick")
        with main.connect_db() as db:
            db.execute("UPDATE project_symbols SET analysis_status='pending' WHERE project_id=?", (project["id"],))
        self.assertEqual(states()["main.py"]["analysis_state"], "pending", "Reset removes completion state")

    def test_tree_lists_functions_with_model_descriptions_and_active_state(self):
        project = self.upload(
            "files",
            {
                "main.py": (
                    b"def first(value):\n    return value\n\n"
                    b"def second(value):\n    return value\n"
                )
            },
        )
        initial = self.tree(project["id"])
        symbols = {item["name"]: item for item in initial["functions"]}
        self.assertEqual(set(symbols), {"first", "second"})
        self.assertIsNone(symbols["first"]["description"])
        self.assertEqual(symbols["first"]["description_status"], "unknown")

        reviewed: list[str] = []
        analyze_project_functions(
            main.connect_db,
            project["id"],
            selected_symbol_ids={symbols["second"]["id"]},
            force_model=True,
            analysis_request=lambda **kwargs: (
                reviewed.append(kwargs["qualified_name"]) or valid_result()
            ),
        )
        updated = self.tree(project["id"])
        functions = {item["name"]: item for item in updated["functions"]}
        self.assertEqual(reviewed, ["second"])
        self.assertEqual(
            functions["second"]["description"],
            "Returns the supplied numeric value.",
        )
        self.assertEqual(functions["second"]["return_lines"][0]["return_type"], "int")
        long_summary = (
            "second returns the supplied value after validating its input and preserving the "
            "documented parameter contract. The engine return contract is an integer result, "
            "including the complete final sentence that previously disappeared from the hover "
            "description when the API stopped at 249 characters."
        )
        with main.connect_db() as db:
            db.execute(
                "UPDATE project_symbol_analyses SET summary=? WHERE symbol_id=?",
                (long_summary, functions["second"]["id"]),
            )
        functions = {
            item["name"]: item for item in self.tree(project["id"])["functions"]
        }
        self.assertGreater(len(long_summary), 250)
        self.assertEqual(
            functions["second"]["description"],
            "second returns the supplied value after validating its input and preserving the "
            "documented parameter contract.",
        )
        self.assertNotIn(
            "engine return contract",
            functions["second"]["description"].casefold(),
        )
        self.assertLessEqual(
            len(functions["second"]["description"]),
            main.FUNCTION_TREE_DESCRIPTION_MAX_CHARS,
        )
        self.assertIsNone(functions["first"]["description"])
        self.assertEqual(
            main.function_summary_description(
                "render_evidence_review returns at line 2627 using "
                "`return \"\\n\".join(lines).strip()`. The engine return contract is str.",
                "render_evidence_review",
            ),
            "Formats the evidence review.",
        )
        merged_returns = main.merge_analyzed_return_types(
            [
                {"code": "return value1", "return_type": "unknown"},
                {"code": "return value2.strip()", "return_type": "unknown"},
                {"code": "return None", "return_type": "null"},
            ],
            ["int", "str"],
        )
        self.assertEqual(
            [item["return_type"] for item in merged_returns],
            ["int", "str", "null"],
        )
        with self.assertRaises(HTTPException) as duplicate:
            main.start_project_function_description_job(
                project["id"], functions["second"]["id"], self.request
            )
        self.assertEqual(duplicate.exception.status_code, 409)

        with main.connect_db() as db:
            db.execute(
                "UPDATE project_symbols SET analysis_status='processing' WHERE id=?",
                (functions["first"]["id"],),
            )
            db.execute(
                """INSERT INTO chat_jobs(
                       id,user_id,chat_id,project_id,project_symbol_id,
                       job_kind,status,progress_stage
                   ) VALUES ('description-active',?,'tree-chat',?,?,'project_analysis',
                             'processing','analyzing_function')""",
                (self.user_id, project["id"], functions["first"]["id"]),
            )
        active = {
            item["name"]: item for item in self.tree(project["id"])["functions"]
        }
        self.assertEqual(active["first"]["analysis_state"], "analysing")
        self.assertEqual(active["second"]["analysis_state"], "processed")

    def test_tree_source_details_and_resolved_function_callers(self):
        project = self.upload(
            "files",
            {
                "main.py": (
                    b"def choose(value: bool) -> int:\n"
                    b"    def nested() -> int:\n"
                    b"        return 99\n"
                    b"    if value:\n"
                    b"        return 1\n"
                    b"    return 2\n\n"
                    b"def use_choose() -> int:\n"
                    b"    return choose(True)\n"
                )
            },
        )
        functions = {
            item["name"]: item for item in self.tree(project["id"])["functions"]
        }
        choose = functions["choose"]
        self.assertEqual(choose["header"], "def choose(value: bool) -> int:")
        self.assertEqual(
            [
                (item["line"], item["code"], item["return_type"])
                for item in choose["return_lines"]
            ],
            [(5, "return 1", "int"), (6, "return 2", "int")],
        )
        self.assertTrue(all(item["flow_dependent"] for item in choose["return_lines"]))
        self.assertNotIn("return 99", {item["code"] for item in choose["return_lines"]})
        self.assertNotIn("start_byte", choose)

        result = main.get_project_function_callers(
            project["id"], int(choose["id"]), self.request
        )
        self.assertEqual(result["caller_count"], 1)
        self.assertEqual(result["function"]["header"], choose["header"])
        self.assertEqual(result["callers"][0]["caller_name"], "use_choose")
        self.assertEqual(result["callers"][0]["path"], "main.py")
        self.assertEqual(result["callers"][0]["start_line"], 9)
        self.assertEqual(result["callers"][0]["usage_kind"], "return")
        self.assertEqual(result["callers"][0]["code"], "choose(True)")
        status, _, body = run_asgi_response(
            method="GET",
            path=f'/api/projects/{project["id"]}/functions/{choose["id"]}/callers',
            cookie=self.request.headers["cookie"],
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["callers"][0]["code"], "choose(True)")

        intruder = self.authenticated_request(self.create_user("caller-intruder"))
        with self.assertRaises(HTTPException) as rejected:
            main.get_project_function_callers(project["id"], int(choose["id"]), intruder)
        self.assertEqual(rejected.exception.status_code, 404)

    def test_get_description_queues_and_processes_only_selected_function(self):
        project = self.upload(
            "files", {"main.py": b"def selected(value):\n    return value\n"}
        )
        function = self.tree(project["id"])["functions"][0]
        with patch.object(main, "enqueue_ollama_job") as enqueue:
            response = main.start_project_function_description_job(
                project["id"], function["id"], self.request
            )
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 202)
        self.assertTrue(payload["description_only"])
        self.assertEqual(payload["project_symbol_id"], function["id"])
        enqueue.assert_called_once_with(payload["job_id"])
        with main.connect_db() as db:
            job = db.execute(
                "SELECT project_symbol_id,progress_total FROM chat_jobs WHERE id=?",
                (payload["job_id"],),
            ).fetchone()
        self.assertEqual(job["project_symbol_id"], function["id"])
        self.assertEqual(job["progress_total"], 1)

        summary = ProjectFunctionAnalysisSummary(
            status="completed",
            total_count=1,
            completed_count=1,
            failed_count=0,
            skipped_count=0,
        )
        with patch.object(main, "analyze_project_functions", return_value=summary) as analyse:
            main.process_project_analysis_job(payload["job_id"], threading.Event())
        self.assertEqual(
            analyse.call_args.kwargs["selected_symbol_ids"], {function["id"]}
        )
        self.assertTrue(analyse.call_args.kwargs["force_model"])

    def test_tree_exposes_deterministic_validator_description(self):
        project = self.upload(
            "files",
            {
                "main.py": (
                    b"class FunctionIssue:\n"
                    b"    def valid_line_order(self):\n"
                    b"        if self.end_line < self.start_line:\n"
                    b"            raise ValueError('bad order')\n"
                    b"        return self\n"
                )
            },
        )
        analyze_project_functions(
            main.connect_db,
            project["id"],
            analysis_request=lambda **_kwargs: self.fail(
                "the deterministic validator should not call the model"
            ),
        )

        function = self.tree(project["id"])["functions"][0]
        self.assertEqual(function["description_status"], "deterministic")
        self.assertIn("validates `self.end_line < self.start_line`", function["description"])
        self.assertIn("raises ValueError", function["description"])

        with patch.object(main, "enqueue_ollama_job") as enqueue:
            response = main.start_project_function_description_job(
                project["id"], function["id"], self.request
            )
        self.assertEqual(response.status_code, 202)
        enqueue.assert_called_once()

    def test_existing_uploads_are_preserved_by_tree_migration(self):
        with closing(sqlite3.connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT, source_kind TEXT)")
            db.execute("CREATE TABLE project_files(id INTEGER PRIMARY KEY, project_id TEXT REFERENCES projects(id), path TEXT, content BLOB)")
            db.execute("INSERT INTO projects VALUES ('old', 'Existing project', 'zip')")
            db.execute("INSERT INTO project_files VALUES (1, 'old', 'src/main.py', ?)", (b"original",))
            migration_032_editable_project_tree(db)
            self.assertEqual(db.execute("SELECT content FROM project_files").fetchone()[0], b"original")
            self.assertIsNotNone(db.execute("SELECT upload_batch_id FROM project_files").fetchone()[0])
            self.assertEqual(db.execute("SELECT source_kind FROM project_upload_batches").fetchone()[0], "zip")
            self.assertIsNone(db.execute("SELECT main_file_path FROM projects").fetchone()[0])
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def setUp(self):
        super().setUp()
        self.user_id = self.create_user("tree")
        with main.connect_db() as db:
            db.execute("INSERT INTO chat_histories(id, user_id, title) VALUES ('tree-chat', ?, 'Tree')", (self.user_id,))
        self.request = self.authenticated_request(self.user_id, method="POST")

    def upload(self, kind, entries, project_id=None):
        if kind == "zip":
            uploads = [UploadFile(file=io.BytesIO(zip_bytes(entries)), filename="library.zip")]
        else:
            uploads = [UploadFile(file=io.BytesIO(content), filename=path.split('/')[-1]) for path, content in entries.items()]
        return asyncio.run(main.upload_project(
            request=self.request, chat_id="tree-chat", source_kind=kind, files=uploads,
            relative_paths=json.dumps(list(entries)), project_id=project_id,
        ))

    def tree(self, project_id):
        return main.get_project_tree(project_id, self.request)

    def test_mixed_uploads_append_with_origins_and_combined_analysis(self):
        project = self.upload("folder", {"demo/main.py": b"def main():\n    return 1\n"})
        second = self.upload("zip", {"lib/helper.js": b"function helper() { return 2; }"}, project["id"])
        third = self.upload("files", {"extra.py": b"def extra():\n    return 3\n", "note.txt": b"notes"}, project["id"])
        self.assertEqual(project["id"], second["id"])
        self.assertEqual(second["id"], third["id"])
        self.assertEqual(third["name"], "demo")
        self.assertEqual(third["file_count"], 4)
        self.assertEqual(third["function_analysis_total_count"], 3)
        tree = self.tree(project["id"])
        self.assertEqual([item["source_kind"] for item in tree["uploads"]], ["folder", "zip", "files"])
        self.assertEqual(tree["uploads"][1]["name"], "library.zip")
        self.assertTrue(all("content" not in file for file in tree["files"]))
        javascript_file = next(
            item for item in tree["files"] if item["path"] == "lib/helper.js"
        )
        javascript_function = next(
            item for item in tree["functions"] if item["file_id"] == javascript_file["id"]
        )
        self.assertEqual(javascript_function["header"], "function helper() {")
        self.assertEqual(
            javascript_function["return_lines"],
            [
                {
                    "line": 1,
                    "code": "return 2;",
                    "return_type": "int",
                    "flow_dependent": False,
                }
            ],
        )
        with main.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_duplicate_and_combined_limits_are_atomic(self):
        project = self.upload("files", {"main.py": b"one"})
        for content in ({"MAIN.py": b"replacement"}, {"main.py/child.py": b"bad"},
                        {"main.py-other.py": b"pass", "main.py/child.py": b"bad"}):
            with self.assertRaises(HTTPException) as rejected:
                self.upload("folder", content, project["id"])
            self.assertEqual(rejected.exception.status_code, 409)
        with patch.object(main, "PROJECT_UPLOAD_LIMITS", UploadLimits(100, 5, 5, 2, 100)):
            with self.assertRaises(HTTPException) as rejected:
                self.upload("files", {"extra.py": b"two"}, project["id"])
            self.assertEqual(rejected.exception.status_code, 413)
        tree = self.tree(project["id"])
        self.assertEqual(len(tree["files"]), 1)
        self.assertEqual(len(tree["uploads"]), 1)

    def test_http_append_main_and_delete_routes(self):
        project = self.upload("files", {"original.py": b"def original(): return 1"})
        boundary = "tree-upload-boundary"
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
                 for key, value in {"chat_id": "tree-chat", "source_kind": "files", "project_id": project["id"]}.items()]
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="entry.py"\r\nContent-Type: application/octet-stream\r\n\r\ndef entry(): return 2\r\n--{boundary}--\r\n'.encode())
        arguments = {"cookie": self.request.headers["cookie"], "origin": "http://127.0.0.1:8000"}
        status, _, body = run_asgi_response(method="POST", path="/api/projects", body=b"".join(parts),
                                           content_type=f"multipart/form-data; boundary={boundary}", **arguments)
        self.assertEqual(status, 201, body)
        self.assertEqual(json.loads(body)["id"], project["id"])
        status, _, body = run_asgi_response(method="GET", path=f'/api/projects/{project["id"]}/tree', **arguments)
        self.assertEqual(status, 200, body)
        file = next(file for file in json.loads(body)["files"] if file["path"] == "entry.py")
        for method, endpoint, payload in (("PUT", "main-file", {"file_id": file["id"]}),
                                          ("DELETE", "entries", {"kind": "file", "file_id": file["id"]})):
            status, _, body = run_asgi_response(method=method, path=f'/api/projects/{project["id"]}/{endpoint}',
                                               body=json.dumps(payload).encode(), content_type="application/json", **arguments)
            self.assertEqual(status, 200, body)
        self.assertEqual([file["path"] for file in self.tree(project["id"])["files"]], ["original.py"])

    def test_non_source_main_file_is_rejected(self):
        project = self.upload("files", {"note.txt": b"notes", "image.png": b"\x00\xff\x00"})
        for file in self.tree(project["id"])["files"]:
            with self.assertRaises(HTTPException) as rejected:
                main.set_project_main_file(project["id"], ProjectMainFile(file_id=file["id"]), self.request)
            self.assertEqual(rejected.exception.status_code, 422)

    def test_delete_folder_only_removes_its_descendants_and_clears_main(self):
        project = self.upload("zip", {
            "root/sub/main.py": b"def main():\n    return 1\n",
            "root/sub/helper.py": b"def helper():\n    return 2\n",
            "root/submarine/keep.py": b"def keep():\n    return 3\n",
        })
        tree = self.tree(project["id"])
        file = next(file for file in tree["files"] if file["path"].endswith("main.py"))
        main.set_project_main_file(project["id"], ProjectMainFile(file_id=file["id"]), self.request)
        with main.connect_db() as db:
            symbol_id = db.execute("SELECT id FROM project_symbols WHERE name = 'main'").fetchone()[0]
            task = load_function_analysis_task(db, symbol_id)
        self.assertIn("root/sub/main.py", task.analysis_context)
        analyze_project_functions(main.connect_db, project["id"])
        deleted = main.delete_project_entry(project["id"], ProjectEntryDelete(kind="folder", batch_id=tree["uploads"][0]["id"], path="root/sub"), self.request)
        self.assertEqual(deleted["deleted_file_count"], 2)
        remaining = self.tree(project["id"])
        self.assertEqual([file["path"] for file in remaining["files"]], ["root/submarine/keep.py"])
        self.assertIsNone(remaining["project"]["main_file_path"])
        self.assertEqual(remaining["project"]["function_analysis_completed_count"], 0)
        with main.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_symbol_analyses").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM function_analysis_cache").fetchone()[0], 0)

    def test_delete_last_file_and_append_again(self):
        project = self.upload("files", {"main.py": b"def main():\n    return 1\n"})
        file = self.tree(project["id"])["files"][0]
        main.delete_project_entry(project["id"], ProjectEntryDelete(kind="file", file_id=file["id"]), self.request)
        empty = self.tree(project["id"])
        self.assertEqual(empty["files"], [])
        self.assertEqual(empty["uploads"], [])
        self.assertEqual(empty["project"]["total_bytes"], 0)
        self.assertEqual(empty["project"]["languages"], [])
        self.assertEqual(self.upload("files", {"new.py": b"def new():\n    return 1"}, project["id"])["file_count"], 1)

    def test_whole_upload_delete_and_main_file_clear(self):
        project = self.upload("files", {"main.py": b"def main():\n    return 1"})
        self.upload("zip", {"lib/helper.py": b"def helper():\n    return 2"}, project["id"])
        tree = self.tree(project["id"])
        file = next(file for file in tree["files"] if file["path"] == "main.py")
        main.set_project_main_file(project["id"], ProjectMainFile(file_id=file["id"]), self.request)
        self.assertEqual(self.tree(project["id"])["project"]["main_file_path"], "main.py")
        main.set_project_main_file(project["id"], ProjectMainFile(), self.request)
        self.assertIsNone(self.tree(project["id"])["project"]["main_file_path"])
        main.delete_project_entry(project["id"], ProjectEntryDelete(kind="upload", batch_id=tree["uploads"][1]["id"]), self.request)
        self.assertEqual([file["path"] for file in self.tree(project["id"])["files"]], ["main.py"])

    def test_entry_point_can_switch_directly_between_source_files(self):
        project = self.upload("files", {
            "first.py": b"def first():\n    return 1\n",
            "second.py": b"def second():\n    return 2\n",
        })
        files = {file["path"]: file for file in self.tree(project["id"])["files"]}

        for path in ("first.py", "second.py", "first.py"):
            response = main.set_project_main_file(
                project["id"], ProjectMainFile(file_id=files[path]["id"]), self.request
            )
            self.assertEqual(response["project"]["main_file_path"], path)
            self.assertEqual(self.tree(project["id"])["project"]["main_file_path"], path)

    def test_ownership_scope_and_active_job_guards(self):
        project = self.upload("files", {"main.py": b"def main():\n    return 1"})
        other = self.upload("files", {"other.py": b"def other():\n    return 2"})
        foreign_file = self.tree(other["id"])["files"][0]
        for action in (
            lambda: main.set_project_main_file(project["id"], ProjectMainFile(file_id=foreign_file["id"]), self.request),
            lambda: main.delete_project_entry(project["id"], ProjectEntryDelete(kind="file", file_id=foreign_file["id"]), self.request),
            lambda: main.get_project_tree(project["id"], self.authenticated_request(self.create_user("intruder"))),
        ):
            with self.assertRaises(HTTPException) as rejected:
                action()
            self.assertEqual(rejected.exception.status_code, 404)
        with main.connect_db() as db:
            db.execute("INSERT INTO chat_jobs(id, user_id, chat_id, project_id, job_kind, status) VALUES ('busy-tree', ?, 'tree-chat', ?, 'project_analysis', 'queued')", (self.user_id, project["id"]))
        file = self.tree(project["id"])["files"][0]
        for action in (
            lambda: self.upload("files", {"extra.py": b"pass"}, project["id"]),
            lambda: main.set_project_main_file(project["id"], ProjectMainFile(file_id=file["id"]), self.request),
            lambda: main.delete_project_entry(project["id"], ProjectEntryDelete(kind="file", file_id=file["id"]), self.request),
        ):
            with self.assertRaises(HTTPException) as rejected:
                action()
            self.assertEqual(rejected.exception.status_code, 409)
