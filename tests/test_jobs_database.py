from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import main
from tests.helpers import DatabaseTestCase


class JobAndDeletionTests(DatabaseTestCase):
    def test_analysis_mode_survives_submission_polling_and_chat_reload(self) -> None:
        user_id = self.create_user("analysis-layout")
        request = self.authenticated_request(user_id, method="POST", path="/api/chat")
        with main.connect_db() as db:
            db.execute("INSERT INTO chat_histories(id, user_id, title) VALUES ('layout-chat', ?, 'Layout')", (user_id,))
        with patch.object(main, "enqueue_ollama_job"):
            response = main.chat(main.ChatRequest(chat_id="layout-chat", message="def main(): return 1", mode="analyse"), request)
        job = json.loads(response.body)
        self.assertEqual(job["mode"], "analyse")
        self.assertEqual(main.chat_job(job["job_id"], request)["mode"], "analyse")
        self.assertEqual(main.load_chat("layout-chat", request)["active_job"]["mode"], "analyse")

    def test_simultaneous_submissions_allow_one_active_job(self) -> None:
        user_id = self.create_user("concurrent")
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES (?, ?, ?)",
                ("same-chat", user_id, "Concurrent"),
            )
        request = self.authenticated_request(user_id, method="POST", path="/api/chat")
        cookie = request.headers["cookie"]

        def new_request():
            from fastapi import Request

            return Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/chat",
                    "headers": [(b"cookie", cookie.encode())],
                    "scheme": "http",
                    "server": ("testserver", 80),
                    "client": ("127.0.0.1", 12345),
                }
            )

        barrier = threading.Barrier(3)
        results: list[int] = []
        lock = threading.Lock()

        def submit(label: str) -> None:
            barrier.wait()
            try:
                response = main.chat(
                    main.ChatRequest(chat_id="same-chat", message=f"message {label}"),
                    new_request(),
                )
                status = response.status_code
            except HTTPException as exc:
                status = exc.status_code
            with lock:
                results.append(status)

        threads = [threading.Thread(target=submit, args=(str(index),)) for index in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

        self.assertEqual(sorted(results), [202, 409])
        with main.connect_db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM chat_jobs WHERE status IN ('queued', 'processing')"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM messages WHERE role = 'user'").fetchone()[0],
                1,
            )

    def test_queued_cancellation_is_immediate(self) -> None:
        user_id = self.create_user("cancel")
        request = self.authenticated_request(
            user_id, method="POST", path="/api/chat-jobs/job/cancel"
        )
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES (?, ?, ?)",
                ("cancel-chat", user_id, "Cancellation"),
            )
            message_id = db.execute(
                "INSERT INTO messages(session_id, role, content) VALUES (?, 'user', ?)",
                (f"user-{user_id}:cancel-chat", "queued input"),
            ).lastrowid
            db.execute(
                """
                INSERT INTO chat_jobs(id, user_id, chat_id, message, status, user_message_id)
                VALUES (?, ?, ?, ?, 'queued', ?)
                """,
                ("cancel-job", user_id, "cancel-chat", "queued input", message_id),
            )

        result = main.cancel_chat_job("cancel-job", request)
        self.assertEqual(result, {"status": "cancelled"})
        with main.connect_db() as db:
            job = db.execute(
                "SELECT status, error, message FROM chat_jobs WHERE id = ?", ("cancel-job",)
            ).fetchone()
        self.assertEqual(tuple(job), ("failed", "Cancelled by user.", None))

    def test_deleting_chat_removes_messages_and_jobs(self) -> None:
        user_id = self.create_user("delete")
        request = self.authenticated_request(
            user_id, method="DELETE", path="/api/chats/delete-chat"
        )
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES (?, ?, ?)",
                ("delete-chat", user_id, "Delete me"),
            )
            db.execute(
                "INSERT INTO messages(session_id, role, content) VALUES (?, 'user', ?)",
                (f"user-{user_id}:delete-chat", "private input"),
            )
            db.execute(
                """
                INSERT INTO chat_jobs(id, user_id, chat_id, status, reply)
                VALUES (?, ?, ?, 'completed', ?)
                """,
                ("delete-job", user_id, "delete-chat", "private reply"),
            )

        self.assertEqual(
            main.delete_chat("delete-chat", request), {"message": "Deleted Delete me"}
        )
        with main.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM chat_histories").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM chat_jobs").fetchone()[0], 0)

    def test_migrations_are_recorded_and_idempotent(self) -> None:
        with main.connect_db() as db:
            first = [tuple(row) for row in db.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            )]
        main.initialise_db()
        with main.connect_db() as db:
            second = [tuple(row) for row in db.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            )]
            user_version = db.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(
            first,
            [
                (1, "core_schema"),
                (2, "one_active_job_per_chat"),
                (3, "security_cleanup_indexes"),
                (4, "general_auth_request_limits"),
                (5, "session_user_index"),
                (6, "admin_audit_events"),
                (7, "terminal_job_maintenance_index"),
                (8, "chat_list_order_index"),
                (9, "admin_session_audit_actions"),
                (10, "admin_job_controls"),
                (11, "admin_backup_audit_action"),
                (12, "admin_maintenance_audit_action"),
                (13, "registration_control"),
                (14, "ai_work_control"),
                (15, "announcement_control"),
                (16, "advanced_admin_controls"),
                (17, "project_uploads"),
                (18, "project_file_inventory"),
                (19, "tree_sitter_adapters"),
                (20, "project_structure_index"),
                (21, "function_analysis_contract"),
                (22, "project_analysis_jobs"),
                (23, "call_compatibility"),
                (24, "function_analysis_cache"),
                (25, "oversized_function_chunking"),
                (26, "issue_provenance"),
                (27, "issue_unsafe_severity"),
                (28, "issue_verification_details"),
                (29, "active_job_elapsed_time"),
                (30, "analysis_signal_metrics"),
                (31, "analysis_accuracy_and_coverage"),
                (32, "editable_project_tree"),
                (33, "function_output_budgets"),
                (34, "function_tree_descriptions"),
            ],
        )
        self.assertEqual(second, first)
        self.assertEqual(user_version, 34)


if __name__ == "__main__":
    unittest.main()
