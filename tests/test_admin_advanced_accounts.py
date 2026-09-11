from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException, Response

import main
from api_models import AdminAccountDisposition, AdminUserAction
from tests.helpers import DatabaseTestCase


class AdminAdvancedAccountTests(DatabaseTestCase):
    def privileged_user(self, suffix: str, *, owner: bool = False) -> int:
        user_id = self.create_user(suffix)
        with main.connect_db() as db:
            db.execute(
                "UPDATE users SET is_admin = 1, is_owner = ? WHERE id = ?",
                (int(owner), user_id),
            )
        return user_id

    def test_successful_login_is_visible_with_account_creation_time(self) -> None:
        admin_id = self.privileged_user("viewer")
        member_id = self.create_user("login-metadata")
        request = self.authenticated_request(member_id, method="POST", path="/api/login")

        result = main.login(
            main.LoginRequest(username=self.username(member_id), password="Valid-pass1!"),
            request,
            Response(),
        )
        users = main.list_users(self.authenticated_request(admin_id))["users"]
        member = next(user for user in users if user["id"] == member_id)

        self.assertEqual(result["message"], "Logged in")
        self.assertIsInstance(member["created_at"], str)
        self.assertGreater(member["last_login_at"], 0)

    def test_temporary_ban_records_reason_expiry_and_revokes_sessions(self) -> None:
        admin_id = self.privileged_user("admin")
        member_id = self.create_user("temporary-ban")
        self.authenticated_request(member_id)
        request = self.authenticated_request(admin_id, method="PATCH")
        request.state.request_id = "temporary-ban-request"

        result = main.change_user(
            member_id,
            AdminUserAction(
                action="ban", reason="Repeated abuse", expires_in_hours=2
            ),
            request,
        )

        with main.connect_db() as db:
            member = db.execute("SELECT * FROM users WHERE id = ?", (member_id,)).fetchone()
            sessions = db.execute(
                "SELECT COUNT(*) FROM login_sessions WHERE user_id = ?", (member_id,)
            ).fetchone()[0]
            audit = db.execute("SELECT details FROM admin_audit_events").fetchone()
        self.assertIn("Banned", result["message"])
        self.assertEqual(member["ban_reason"], "Repeated abuse")
        self.assertGreater(member["banned_until"], int(time.time()))
        self.assertEqual(sessions, 0)
        self.assertIn("Repeated abuse", audit["details"])

    def test_expired_temporary_ban_self_clears(self) -> None:
        member_id = self.create_user("expired-ban")
        with main.connect_db() as db:
            db.execute(
                """
                UPDATE users SET is_banned = 1, ban_reason = 'expired', banned_until = ?
                WHERE id = ?
                """,
                (int(time.time()) - 1, member_id),
            )
        request = self.authenticated_request(member_id)

        user = main.require_user(request)

        self.assertEqual(user["id"], member_id)
        self.assertEqual(user["is_banned"], 0)

    def test_admin_can_queue_member_password_reset_without_receiving_token(self) -> None:
        admin_id = self.privileged_user("admin")
        member_id = self.create_user("reset-target")
        tasks = BackgroundTasks()
        request = self.authenticated_request(admin_id, method="POST")
        request.state.request_id = "admin-reset-request"

        with patch.object(main, "SMTP_HOST", "smtp.example.test"), patch.object(
            main, "SMTP_FROM", "bot@example.test"
        ), patch.object(main, "PUBLIC_BASE_URL", "https://chat.example.test"):
            response = main.send_admin_password_reset(member_id, request, tasks)

        with main.connect_db() as db:
            reset = db.execute(
                "SELECT token_hash FROM password_reset_tokens WHERE user_id = ?",
                (member_id,),
            ).fetchone()
            audit = db.execute("SELECT action FROM admin_audit_events").fetchone()
        self.assertIn("queued", response["message"])
        self.assertNotIn("token", response)
        self.assertIsNotNone(reset)
        self.assertEqual(audit["action"], "send_password_reset")
        self.assertEqual(len(tasks.tasks), 1)

    def test_ordinary_admin_cannot_reset_peer_admin(self) -> None:
        admin_id = self.privileged_user("admin")
        peer_id = self.privileged_user("peer")
        with patch.object(main, "SMTP_HOST", "smtp.example.test"), patch.object(
            main, "SMTP_FROM", "bot@example.test"
        ), patch.object(main, "PUBLIC_BASE_URL", "https://chat.example.test"):
            with self.assertRaises(HTTPException) as raised:
                main.send_admin_password_reset(
                    peer_id,
                    self.authenticated_request(admin_id, method="POST"),
                    BackgroundTasks(),
                )
        self.assertEqual(raised.exception.status_code, 403)

    def test_owner_can_anonymize_account_and_all_stored_chat_data(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        member_id = self.create_user("erase-me")
        username = self.username(member_id)
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('private', ?, 'Private')",
                (member_id,),
            )
            db.execute(
                "INSERT INTO messages(session_id, role, content) VALUES (?, 'user', 'secret')",
                (f"user-{member_id}:private",),
            )

        result = main.dispose_admin_user_account(
            member_id,
            AdminAccountDisposition(mode="anonymize", confirmation=username),
            self.authenticated_request(owner_id, method="POST"),
        )

        with main.connect_db() as db:
            member = db.execute("SELECT * FROM users WHERE id = ?", (member_id,)).fetchone()
            messages = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            audit = db.execute("SELECT * FROM admin_audit_events").fetchone()
        self.assertIn("anonymized", result["message"])
        self.assertEqual(member["is_anonymized"], 1)
        self.assertNotEqual(member["username"], username)
        self.assertNotIn("erase-me", member["email"])
        self.assertEqual(messages, 0)
        self.assertEqual(audit["action"], "anonymize_account")
        self.assertNotIn(username, audit["target_username"])

    def test_owner_can_delete_account_but_not_owner(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        member_id = self.create_user("delete-me")
        username = self.username(member_id)
        request = self.authenticated_request(owner_id, method="POST")

        result = main.dispose_admin_user_account(
            member_id,
            AdminAccountDisposition(mode="delete", confirmation=username),
            request,
        )
        with main.connect_db() as db:
            member = db.execute("SELECT 1 FROM users WHERE id = ?", (member_id,)).fetchone()
            audit = db.execute("SELECT * FROM admin_audit_events").fetchone()
        self.assertIn("permanently deleted", result["message"])
        self.assertIsNone(member)
        self.assertEqual(audit["action"], "delete_account")
        self.assertIsNone(audit["target_user_id"])

        with self.assertRaises(HTTPException) as raised:
            main.dispose_admin_user_account(
                owner_id,
                AdminAccountDisposition(
                    mode="delete", confirmation=self.username(owner_id)
                ),
                self.authenticated_request(owner_id, method="POST"),
            )
        self.assertEqual(raised.exception.status_code, 403)

    def username(self, user_id: int) -> str:
        with main.connect_db() as db:
            return str(
                db.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()[0]
            )


if __name__ == "__main__":
    unittest.main()
