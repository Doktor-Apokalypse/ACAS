from __future__ import annotations

import hashlib
import json
import threading
from unittest.mock import patch

import analysis_engine
import main
from analysis_engine import (
    FunctionAnalysisResult,
    FunctionParameterContract,
    FunctionReturnContract,
    FunctionReturnType,
)
from api_models import ProjectAnalysisRequest
from project_inventory import inventory_project_database
from project_parsing import parse_project_database
from tests.helpers import DatabaseTestCase


def model_result() -> FunctionAnalysisResult:
    return FunctionAnalysisResult(
        contract_version="1.0",
        summary="Returns its input value.",
        syntax_valid=True,
        parameters=[
            FunctionParameterContract(
                name="value",
                kind="positional_or_keyword",
                required=True,
                accepted_types=["int"],
                description="Input integer.",
            )
        ],
        returns=FunctionReturnContract(
            may_return_value=True,
            possible_types=[
                FunctionReturnType(type="int", description="Returned integer.")
            ],
            nullable=False,
            description="Returns an integer.",
        ),
        confidence=0.95,
    )


class ProjectAnalysisJobTests(DatabaseTestCase):
    def create_project(self, suffix: str = "job") -> tuple[int, str, str]:
        user_id = self.create_user(suffix)
        chat_id = f"{suffix}-chat"
        project_id = f"{suffix}-project"
        content = (
            b"def first(value):\n    return value\n\n"
            b"def second(value):\n    return value + 1\n"
        )
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES (?, ?, 'Project job')",
                (chat_id, user_id),
            )
            db.execute(
                """
                INSERT INTO projects(id, user_id, chat_id, name, source_kind, file_count, total_bytes)
                VALUES (?, ?, ?, 'demo', 'folder', 1, ?)
                """,
                (project_id, user_id, chat_id, len(content)),
            )
            db.execute(
                """
                INSERT INTO project_files(project_id, path, content, size_bytes, sha256, is_binary)
                VALUES (?, 'demo/main.py', ?, ?, ?, 0)
                """,
                (project_id, content, len(content), hashlib.sha256(content).hexdigest()),
            )
            inventory_project_database(db, project_id)
            parse_project_database(db, project_id)
        return user_id, chat_id, project_id

    def start_job(self, user_id: int, project_id: str, *, retry_failed: bool = False) -> dict:
        response = main.start_project_analysis_job(
            project_id,
            ProjectAnalysisRequest(retry_failed=retry_failed),
            self.authenticated_request(
                user_id,
                method="POST",
                path=f"/api/projects/{project_id}/analysis-jobs",
            ),
        )
        return json.loads(bytes(response.body))

    def test_start_and_queued_cancel_are_owned_durable_and_restore_project_state(self) -> None:
        user_id, chat_id, project_id = self.create_project("queued")
        data = self.start_job(user_id, project_id)
        self.assertEqual(data["job_kind"], "project_analysis")
        self.assertEqual(data["progress_total"], 2)
        self.assertEqual(data["progress_file_total"], 1)
        with main.connect_db() as db:
            job = db.execute(
                """
                SELECT chat_id, project_id, job_kind, status, input_char_count
                FROM chat_jobs WHERE id = ?
                """,
                (data["job_id"],),
            ).fetchone()
            project_status = db.execute(
                "SELECT function_analysis_status FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()[0]
        self.assertEqual(tuple(job)[:4], (chat_id, project_id, "project_analysis", "queued"))
        self.assertGreater(job["input_char_count"], 0)
        self.assertEqual(project_status, "running")
        with self.assertRaises(main.HTTPException) as conflict:
            self.start_job(user_id, project_id)
        self.assertEqual(conflict.exception.status_code, 409)

        cancelled = main.cancel_chat_job(
            data["job_id"],
            self.authenticated_request(
                user_id,
                method="POST",
                path=f"/api/chat-jobs/{data['job_id']}/cancel",
            ),
        )
        self.assertEqual(cancelled, {"status": "cancelled"})
        with main.connect_db() as db:
            project_status = db.execute(
                "SELECT function_analysis_status FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()[0]
        self.assertEqual(project_status, "pending")

    def test_active_project_job_status_includes_live_result_counts(self) -> None:
        user_id, chat_id, project_id = self.create_project("live-counts")
        data = self.start_job(user_id, project_id)
        self.assertEqual(data["project"]["function_analysis_completed_count"], 0)
        with main.connect_db() as db:
            symbol_id = int(
                db.execute(
                    "SELECT id FROM project_symbols WHERE project_id = ? ORDER BY id LIMIT 1",
                    (project_id,),
                ).fetchone()[0]
            )
            db.execute(
                "UPDATE project_symbols SET analysis_status = 'completed' WHERE id = ?",
                (symbol_id,),
            )
            main.refresh_project_function_analysis(
                db, project_id, forced_status="running"
            )
            db.execute(
                "UPDATE chat_jobs SET status = 'processing' WHERE id = ?",
                (data["job_id"],),
            )

        status = main.chat_job(
            data["job_id"],
            self.authenticated_request(
                user_id, path=f"/api/chat-jobs/{data['job_id']}"
            ),
        )
        self.assertEqual(status["status"], "processing")
        self.assertEqual(status["project"]["function_analysis_completed_count"], 1)

        loaded = main.load_chat(
            chat_id,
            self.authenticated_request(user_id, path=f"/api/chats/{chat_id}"),
        )
        self.assertEqual(
            loaded["active_job"]["project"]["function_analysis_completed_count"],
            1,
        )

    def test_worker_records_file_and_function_progress_and_status_api_needs_no_reply(self) -> None:
        user_id, chat_id, project_id = self.create_project("worker")
        data = self.start_job(user_id, project_id)
        cancel_event = threading.Event()
        with patch.object(analysis_engine, "request_function_analysis", side_effect=lambda **_kwargs: model_result()):
            main.process_chat_job(data["job_id"], cancel_event)

        status = main.chat_job(
            data["job_id"],
            self.authenticated_request(user_id, path=f"/api/chat-jobs/{data['job_id']}"),
        )
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["job_kind"], "project_analysis")
        self.assertNotIn("reply", status)
        self.assertEqual(status["project"]["function_analysis_completed_count"], 2)
        self.assertEqual(status["progress_file_path"], "demo/main.py")
        self.assertEqual(status["progress_file_current"], 1)
        self.assertEqual(status["progress_file_total"], 1)
        self.assertEqual(status["progress_function_current"], 2)
        self.assertEqual(status["progress_function_total"], 2)
        messages = [event["message"] for event in status["progress_log"]]
        self.assertIn("demo/main.py/second", messages)
        self.assertFalse(any(message.startswith("File ") for message in messages))
        self.assertTrue(any("Checking call contracts" in message for message in messages))
        self.assertEqual(status["project"]["call_compatibility_status"], "unavailable")

        loaded = main.load_chat(
            chat_id,
            self.authenticated_request(user_id, path=f"/api/chats/{chat_id}"),
        )
        self.assertIsNone(loaded["active_job"])
        self.assertEqual(loaded["projects"][0]["function_analysis_status"], "completed")

    def test_finished_pass_with_invalid_reviews_reports_outstanding_work(self) -> None:
        user_id, _chat_id, project_id = self.create_project("incomplete-pass")
        data = self.start_job(user_id, project_id)
        with patch.object(analysis_engine, "request_function_analysis", side_effect=ValueError("Invalid response")):
            main.process_chat_job(data["job_id"], threading.Event())
        status = main.chat_job(data["job_id"], self.authenticated_request(user_id, path=f"/api/chat-jobs/{data['job_id']}"))
        self.assertEqual(status["status"], "completed")
        self.assertNotEqual(status["project"]["function_analysis_status"], "completed")
        self.assertIn("resume to retry incomplete reviews", status["progress_log"][-1]["message"])
        self.assertNotEqual(status["progress_log"][-1]["message"], "Project function analysis completed")

    def test_identical_consecutive_function_messages_are_logged_once(self) -> None:
        user_id, _chat_id, project_id = self.create_project("deduplicated-progress")
        data = self.start_job(user_id, project_id)
        progress = (
            1,
            2,
            "demo/main.py",
            "first",
            1,
            1,
            1,
            2,
        )
        with main.connect_db() as db:
            main.record_project_job_progress(
                db,
                data["job_id"],
                "analyzing_function",
                *progress,
            )
            main.record_project_job_progress(
                db,
                data["job_id"],
                "using_deterministic_function",
                *progress,
            )
            job = db.execute(
                """
                SELECT progress_stage, progress_log
                FROM chat_jobs WHERE id = ?
                """,
                (data["job_id"],),
            ).fetchone()

        events = main.decode_progress_log(job["progress_log"])
        messages = [event.get("message") for event in events]
        self.assertEqual(messages.count("demo/main.py/first"), 1)
        self.assertEqual(job["progress_stage"], "using_deterministic_function")

    def test_processing_cancellation_updates_both_job_and_project(self) -> None:
        user_id, _chat_id, project_id = self.create_project("processing")
        data = self.start_job(user_id, project_id)
        cancel_event = threading.Event()

        def cancel_during_request(**_kwargs):
            cancel_event.set()
            raise analysis_engine.AnalysisCancelled("cancelled")

        with patch.object(analysis_engine, "request_function_analysis", side_effect=cancel_during_request):
            main.process_chat_job(data["job_id"], cancel_event)
        with main.connect_db() as db:
            job = db.execute(
                "SELECT status, error FROM chat_jobs WHERE id = ?",
                (data["job_id"],),
            ).fetchone()
            project = db.execute(
                "SELECT function_analysis_status FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
        self.assertEqual(tuple(job), ("failed", "Cancelled by user."))
        self.assertEqual(project[0], "cancelled")

    def test_project_analysis_job_can_be_paused_and_resumed(self) -> None:
        user_id, _chat_id, project_id = self.create_project("pause")
        data = self.start_job(user_id, project_id)
        request = self.authenticated_request(
            user_id,
            method="POST",
            path=f"/api/chat-jobs/{data['job_id']}/pause",
        )

        paused = main.pause_chat_job(data["job_id"], request)

        self.assertEqual(paused["status"], "paused")
        self.assertIsInstance(paused["elapsed_seconds"], int)
        with main.JOB_CANCEL_LOCK:
            self.assertTrue(main.JOB_PAUSE_EVENTS[data["job_id"]].is_set())
        status = main.chat_job(
            data["job_id"],
            self.authenticated_request(user_id, path=f"/api/chat-jobs/{data['job_id']}"),
        )
        self.assertEqual(status["progress_stage"], "paused")
        self.assertTrue(
            any(event["message"] == "Paused" for event in status["progress_log"])
        )

        resumed = main.resume_chat_job(
            data["job_id"],
            self.authenticated_request(
                user_id,
                method="POST",
                path=f"/api/chat-jobs/{data['job_id']}/resume",
            ),
        )

        self.assertEqual(resumed["status"], "resumed")
        self.assertIsInstance(resumed["elapsed_seconds"], int)
        with main.JOB_CANCEL_LOCK:
            self.assertFalse(main.JOB_PAUSE_EVENTS[data["job_id"]].is_set())
        status = main.chat_job(
            data["job_id"],
            self.authenticated_request(user_id, path=f"/api/chat-jobs/{data['job_id']}"),
        )
        self.assertTrue(
            any(event["message"] == "Resumed" for event in status["progress_log"])
        )
        self.assertEqual(status["progress_stage"], "resumed")

    def test_paused_time_is_excluded_from_elapsed_time_and_log_timestamps(self) -> None:
        user_id, _chat_id, project_id = self.create_project("pause-timer")
        data = self.start_job(user_id, project_id)
        with main.connect_db() as db:
            db.execute(
                """
                UPDATE chat_jobs
                SET started_at = 100, progress_log = '[]', paused_at = NULL,
                    paused_seconds = 0
                WHERE id = ?
                """,
                (data["job_id"],),
            )

        with patch.object(main.time, "time", return_value=110):
            paused = main.pause_chat_job(
                data["job_id"],
                self.authenticated_request(
                    user_id,
                    method="POST",
                    path=f"/api/chat-jobs/{data['job_id']}/pause",
                ),
            )
        self.assertEqual(paused["elapsed_seconds"], 10)

        with patch.object(main.time, "time", return_value=150):
            paused_status = main.chat_job(
                data["job_id"],
                self.authenticated_request(
                    user_id, path=f"/api/chat-jobs/{data['job_id']}"
                ),
            )
            resumed = main.resume_chat_job(
                data["job_id"],
                self.authenticated_request(
                    user_id,
                    method="POST",
                    path=f"/api/chat-jobs/{data['job_id']}/resume",
                ),
            )

        self.assertEqual(paused_status["elapsed_seconds"], 10)
        self.assertEqual(resumed["elapsed_seconds"], 10)

        with patch.object(main.time, "time", return_value=155):
            resumed_status = main.chat_job(
                data["job_id"],
                self.authenticated_request(
                    user_id, path=f"/api/chat-jobs/{data['job_id']}"
                ),
            )
        self.assertEqual(resumed_status["elapsed_seconds"], 15)
        event_times = {
            event["message"]: event["elapsed"]
            for event in resumed_status["progress_log"]
        }
        self.assertEqual(event_times["Paused"], 10)
        self.assertEqual(event_times["Resumed"], 10)
        with main.connect_db() as db:
            timing = db.execute(
                "SELECT paused_at, paused_seconds FROM chat_jobs WHERE id = ?",
                (data["job_id"],),
            ).fetchone()
        self.assertIsNone(timing["paused_at"])
        self.assertEqual(timing["paused_seconds"], 40)

    def test_project_cannot_be_deleted_while_its_job_is_active(self) -> None:
        user_id, _chat_id, project_id = self.create_project("delete-active")
        self.start_job(user_id, project_id)
        with self.assertRaises(main.HTTPException) as raised:
            main.delete_project(
                project_id,
                self.authenticated_request(
                    user_id,
                    method="DELETE",
                    path=f"/api/projects/{project_id}",
                ),
            )
        self.assertEqual(raised.exception.status_code, 409)

    def test_another_user_cannot_start_or_read_a_project_job(self) -> None:
        owner_id, _chat_id, project_id = self.create_project("ownership")
        stranger_id = self.create_user("stranger")
        with self.assertRaises(main.HTTPException) as missing:
            self.start_job(stranger_id, project_id)
        self.assertEqual(missing.exception.status_code, 404)
        data = self.start_job(owner_id, project_id)
        with self.assertRaises(main.HTTPException) as hidden:
            main.chat_job(
                data["job_id"],
                self.authenticated_request(
                    stranger_id,
                    path=f"/api/chat-jobs/{data['job_id']}",
                ),
            )
        self.assertEqual(hidden.exception.status_code, 404)


if __name__ == "__main__":
    import unittest

    unittest.main()
