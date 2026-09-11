from __future__ import annotations

import json
import threading
import time
import unittest

from fastapi import HTTPException

import main
import web_assets
from tests.helpers import DatabaseTestCase


class AdminJobControlTests(DatabaseTestCase):
    def privileged_user(self, suffix: str, *, owner: bool = False) -> int:
        user_id = self.create_user(suffix)
        with main.connect_db() as db:
            db.execute(
                "UPDATE users SET is_admin = 1, is_owner = ? WHERE id = ?",
                (int(owner), user_id),
            )
        return user_id

    def create_job(
        self,
        user_id: int,
        job_id: str,
        *,
        status: str = "queued",
        with_message: bool = False,
    ) -> None:
        chat_id = f"chat-{job_id}"
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES (?, ?, ?)",
                (chat_id, user_id, f"Title {job_id}"),
            )
            message_id = None
            if with_message:
                message_id = db.execute(
                    "INSERT INTO messages(session_id, role, content) VALUES (?, 'user', ?)",
                    (f"user-{user_id}:{chat_id}", "pending request"),
                ).lastrowid
            db.execute(
                """
                INSERT INTO chat_jobs(
                    id, user_id, chat_id, message, status, user_message_id,
                    progress_stage, progress_current, progress_total, started_at,
                    progress_log
                ) VALUES (?, ?, ?, 'pending request', ?, ?, 'analysing', 2, 5, ?, ?)
                """,
                (
                    job_id,
                    user_id,
                    chat_id,
                    status,
                    message_id,
                    int(time.time()) - 7 if status == "processing" else 0,
                    '[{"message":"Checking files"}]',
                ),
            )

    def test_admin_can_monitor_all_active_jobs(self) -> None:
        admin_id = self.privileged_user("admin")
        member_id = self.create_user("member")
        self.create_job(member_id, "processing-job", status="processing")
        self.create_job(member_id, "queued-job")
        with main.connect_db() as db:
            db.execute(
                """
                INSERT INTO chat_jobs(id, user_id, chat_id, status)
                VALUES ('finished-job', ?, 'finished-chat', 'completed')
                """,
                (member_id,),
            )

        response = main.list_active_admin_jobs(
            self.authenticated_request(admin_id, path="/api/admin/jobs")
        )

        self.assertEqual([job["id"] for job in response["jobs"]], ["processing-job", "queued-job"])
        self.assertEqual(response["jobs"][0]["progress_message"], "Checking files")
        self.assertGreaterEqual(response["jobs"][0]["elapsed_seconds"], 7)
        self.assertIn("queue_depth", response)
        self.assertIn("worker_alive", response)

    def test_admin_immediately_cancels_queued_member_job_and_audits_it(self) -> None:
        admin_id = self.privileged_user("admin")
        member_id = self.create_user("member")
        self.create_job(member_id, "queued-job", with_message=True)
        event = threading.Event()
        with main.JOB_CANCEL_LOCK:
            main.JOB_CANCEL_EVENTS["queued-job"] = event
        request = self.authenticated_request(
            admin_id, method="POST", path="/api/admin/chat-jobs/queued-job/cancel"
        )
        request.state.request_id = "admin-cancel-request"

        result = main.cancel_admin_chat_job("queued-job", request)

        self.assertEqual(result, {"status": "cancelled"})
        self.assertTrue(event.is_set())
        with main.connect_db() as db:
            job = db.execute(
                "SELECT status, error, cancel_reason FROM chat_jobs WHERE id = 'queued-job'"
            ).fetchone()
            message = db.execute(
                "SELECT content FROM messages WHERE role = 'assistant'"
            ).fetchone()
            audit = db.execute("SELECT * FROM admin_audit_events").fetchone()
        self.assertEqual(
            tuple(job),
            ("failed", "Cancelled by an administrator.", "Cancelled by an administrator."),
        )
        self.assertEqual(message["content"], "Process cancelled by an administrator.")
        self.assertEqual(audit["action"], "cancel_job")
        self.assertEqual(audit["request_id"], "admin-cancel-request")
        self.assertEqual(json.loads(audit["details"])["job_id"], "queued-job")

    def test_processing_cancellation_is_cooperative_and_keeps_admin_reason(self) -> None:
        admin_id = self.privileged_user("admin")
        member_id = self.create_user("member")
        self.create_job(member_id, "processing-job", status="processing")
        event = threading.Event()
        with main.JOB_CANCEL_LOCK:
            main.JOB_CANCEL_EVENTS["processing-job"] = event

        result = main.cancel_admin_chat_job(
            "processing-job",
            self.authenticated_request(admin_id, method="POST"),
        )

        self.assertEqual(result, {"status": "cancellation_requested"})
        self.assertTrue(event.is_set())
        with main.connect_db() as db:
            job = db.execute(
                "SELECT status, cancel_requested, cancel_reason FROM chat_jobs WHERE id = ?",
                ("processing-job",),
            ).fetchone()
        self.assertEqual(
            tuple(job),
            ("processing", 1, "Cancelled by an administrator."),
        )

        with main.connect_db() as db:
            db.execute(
                "UPDATE chat_jobs SET status = 'queued' WHERE id = ?",
                ("processing-job",),
            )
        main.process_active_chat_job("processing-job", event)
        with main.connect_db() as db:
            terminal = db.execute(
                "SELECT status, error FROM chat_jobs WHERE id = ?", ("processing-job",)
            ).fetchone()
        self.assertEqual(tuple(terminal), ("failed", "Cancelled by an administrator."))

    def test_job_cancellation_enforces_administrator_hierarchy(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        admin_id = self.privileged_user("admin")
        peer_admin_id = self.privileged_user("peer")
        self.create_job(owner_id, "owner-job")
        self.create_job(peer_admin_id, "admin-job")

        with self.assertRaises(HTTPException) as peer_error:
            main.cancel_admin_chat_job(
                "admin-job", self.authenticated_request(admin_id, method="POST")
            )
        self.assertEqual(peer_error.exception.status_code, 403)

        with self.assertRaises(HTTPException) as owner_job_error:
            main.cancel_admin_chat_job(
                "owner-job", self.authenticated_request(owner_id, method="POST")
            )
        self.assertEqual(owner_job_error.exception.status_code, 403)

        self.assertEqual(
            main.cancel_admin_chat_job(
                "admin-job", self.authenticated_request(owner_id, method="POST")
            ),
            {"status": "cancelled"},
        )

    def test_member_cannot_access_admin_job_monitor(self) -> None:
        member_id = self.create_user("member")
        with self.assertRaises(HTTPException) as error:
            main.list_active_admin_jobs(self.authenticated_request(member_id))
        self.assertEqual(error.exception.status_code, 403)

    def test_admin_page_exposes_job_monitor_and_safe_controls(self) -> None:
        self.assertIn("Active jobs", web_assets.ADMIN_HTML)
        self.assertIn("/api/admin/jobs", web_assets.ADMIN_HTML)
        self.assertIn("mayCancelJob", web_assets.ADMIN_HTML)
        self.assertIn("cancel_job", web_assets.ADMIN_HTML)


if __name__ == "__main__":
    unittest.main()
