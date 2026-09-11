from __future__ import annotations

import unittest

from fastapi import HTTPException

import main
from api_models import AdminUserLimits
from tests.helpers import DatabaseTestCase


class AdminUserLimitTests(DatabaseTestCase):
    def privileged_user(self, suffix: str, *, owner: bool = False) -> int:
        user_id = self.create_user(suffix)
        with main.connect_db() as db:
            db.execute(
                "UPDATE users SET is_admin = 1, is_owner = ? WHERE id = ?",
                (int(owner), user_id),
            )
        return user_id

    def test_owner_sets_tighter_limits_and_change_is_audited(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        member_id = self.create_user("limited")
        request = self.authenticated_request(owner_id, method="PUT")
        request.state.request_id = "limit-request"
        payload = AdminUserLimits(
            storage_limit_bytes=2_000_000,
            active_job_limit=1,
            pending_input_char_limit=10_000,
        )

        response = main.set_admin_user_limits(member_id, payload, request)

        with main.connect_db() as db:
            member = db.execute("SELECT * FROM users WHERE id = ?", (member_id,)).fetchone()
            audit = db.execute("SELECT action, details FROM admin_audit_events").fetchone()
        self.assertIn("Updated limits", response["message"])
        self.assertEqual(member["active_job_limit"], 1)
        self.assertEqual(member["pending_input_char_limit"], 10_000)
        self.assertEqual(audit["action"], "set_user_limits")
        self.assertIn("storage_limit_bytes", audit["details"])

    def test_limits_cannot_exceed_global_safeguards(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        member_id = self.create_user("limited")
        with self.assertRaises(HTTPException) as raised:
            main.set_admin_user_limits(
                member_id,
                AdminUserLimits(active_job_limit=main.MAX_ACTIVE_JOBS_PER_USER + 1),
                self.authenticated_request(owner_id, method="PUT"),
            )
        self.assertEqual(raised.exception.status_code, 422)

    def test_active_job_pending_input_and_storage_limits_are_enforced(self) -> None:
        member_id = self.create_user("limited")
        with main.connect_db() as db:
            db.execute(
                """
                UPDATE users SET active_job_limit = 1, pending_input_char_limit = 10,
                                 storage_limit_bytes = 20
                WHERE id = ?
                """,
                (member_id,),
            )
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('limit-chat', ?, 'Chat')",
                (member_id,),
            )
            db.execute(
                "INSERT INTO messages(session_id, role, content) VALUES (?, 'user', '123456789012345678')",
                (f"user-{member_id}:limit-chat",),
            )
            db.execute(
                """
                INSERT INTO chat_jobs(id, user_id, chat_id, message, status)
                VALUES ('active-limit', ?, 'limit-chat', '12345', 'queued')
                """,
                (member_id,),
            )
            with self.assertRaises(HTTPException) as active:
                main.enforce_job_admission(db, member_id, 1)
            self.assertEqual(active.exception.status_code, 429)
            db.execute("UPDATE chat_jobs SET status = 'completed' WHERE id = 'active-limit'")
            with self.assertRaises(HTTPException) as storage:
                main.enforce_job_admission(
                    db, member_id, 1, storage_bytes_to_add=3
                )
            self.assertEqual(storage.exception.status_code, 413)

    def test_ordinary_administrator_cannot_set_limits(self) -> None:
        admin_id = self.privileged_user("admin")
        member_id = self.create_user("member")
        with self.assertRaises(HTTPException) as raised:
            main.set_admin_user_limits(
                member_id,
                AdminUserLimits(active_job_limit=1),
                self.authenticated_request(admin_id, method="PUT"),
            )
        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
