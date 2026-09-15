from __future__ import annotations

import sqlite3
import time

from fastapi import HTTPException

import main
import web_assets
from api_models import AdminUserAction
from migrations import apply_migrations
from tests.helpers import DatabaseTestCase


class AdminAccountRecoveryTests(DatabaseTestCase):
    def privileged_user(self, suffix: str, *, owner: bool = False) -> int:
        user_id = self.create_user(suffix)
        with main.connect_db() as db:
            db.execute(
                "UPDATE users SET is_admin = 1, is_owner = ? WHERE id = ?",
                (int(owner), user_id),
            )
        return user_id

    def test_admin_can_inspect_and_clear_member_sessions_and_login_lock(self) -> None:
        admin_id = self.privileged_user("recovery_admin")
        target_id = self.create_user("recovery_target")
        now = int(time.time())
        account_scope = main.login_account_scope(target_id, "")
        with main.connect_db() as db:
            db.executemany(
                "INSERT INTO login_sessions(user_id, token_hash, expires_at) VALUES (?, ?, ?)",
                [
                    (target_id, "target-session-1", now + 600),
                    (target_id, "target-session-2", now + 1_200),
                ],
            )
            db.execute(
                """
                INSERT INTO login_throttles(
                    scope_hash, failed_attempts, last_attempt_at, locked_until, updated_at
                ) VALUES (?, 3, ?, ?, ?)
                """,
                (account_scope, now, now + 300, now),
            )

        list_request = self.authenticated_request(admin_id, path="/api/admin/users")
        listed = main.list_users(list_request, q="recovery_target")
        self.assertEqual(listed["users"][0]["active_session_count"], 2)
        self.assertEqual(listed["users"][0]["failed_login_attempts"], 3)
        self.assertEqual(listed["users"][0]["locked_until"], now + 300)

        action_request = self.authenticated_request(admin_id, method="PATCH")
        action_request.state.request_id = "account-recovery"
        revoked = main.change_user(
            target_id, AdminUserAction(action="revoke_sessions"), action_request
        )
        unlocked = main.change_user(
            target_id, AdminUserAction(action="unlock"), action_request
        )

        self.assertIn("Revoked 2 active sessions", revoked["message"])
        self.assertIn("Cleared login restrictions", unlocked["message"])
        with main.connect_db() as db:
            target_sessions = db.execute(
                "SELECT COUNT(*) FROM login_sessions WHERE user_id = ?", (target_id,)
            ).fetchone()[0]
            throttle = db.execute(
                "SELECT 1 FROM login_throttles WHERE scope_hash = ?", (account_scope,)
            ).fetchone()
            actions = [
                row["action"]
                for row in db.execute(
                    "SELECT action FROM admin_audit_events ORDER BY id"
                )
            ]
        self.assertEqual(target_sessions, 0)
        self.assertIsNone(throttle)
        self.assertEqual(actions, ["revoke_sessions", "unlock"])

    def test_admin_cannot_recover_peer_admin_and_owner_remains_protected(self) -> None:
        admin_id = self.privileged_user("ordinary_admin")
        peer_id = self.privileged_user("peer_admin")
        owner_id = self.privileged_user("protected_owner", owner=True)
        request = self.authenticated_request(admin_id, method="PATCH")

        for target_id, action in (
            (peer_id, "revoke_sessions"),
            (peer_id, "unlock"),
            (owner_id, "revoke_sessions"),
            (owner_id, "unlock"),
        ):
            with self.subTest(target_id=target_id, action=action), self.assertRaises(
                HTTPException
            ) as raised:
                main.change_user(target_id, AdminUserAction(action=action), request)
            self.assertEqual(raised.exception.status_code, 403)

        with main.connect_db() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM admin_audit_events").fetchone()[0], 0
            )

    def test_owner_can_revoke_administrator_sessions(self) -> None:
        owner_id = self.privileged_user("recovery_owner", owner=True)
        admin_id = self.privileged_user("target_admin")
        now = int(time.time())
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO login_sessions(user_id, token_hash, expires_at) VALUES (?, ?, ?)",
                (admin_id, "admin-target-session", now + 600),
            )
        request = self.authenticated_request(owner_id, method="PATCH")

        result = main.change_user(
            admin_id, AdminUserAction(action="revoke_sessions"), request
        )

        self.assertIn("Revoked 1 active session", result["message"])

    def test_audit_constraint_migration_preserves_existing_events(self) -> None:
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            apply_migrations(db, target_version=8)
            db.execute(
                """
                INSERT INTO users(email, username, password_hash)
                VALUES ('audit@example.test', 'audit-user', 'hash')
                """
            )
            user_id = int(db.execute("SELECT id FROM users").fetchone()[0])
            db.execute(
                """
                INSERT INTO admin_audit_events(
                    actor_user_id, actor_username, target_user_id, target_username,
                    action, request_id, created_at
                ) VALUES (?, 'audit-user', ?, 'audit-user', 'ban', 'before-v9', 1)
                """,
                (user_id, user_id),
            )

            self.assertEqual(
                apply_migrations(db),
                [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35],
            )
            preserved = db.execute(
                "SELECT action, request_id FROM admin_audit_events WHERE id = 1"
            ).fetchone()
            db.execute(
                """
                INSERT INTO admin_audit_events(
                    actor_username, target_username, action, request_id, created_at
                ) VALUES ('audit-user', 'audit-user', 'unlock', 'after-v10', 2)
                """
            )
            db.execute(
                """
                INSERT INTO admin_audit_events(
                    actor_username, target_username, action, request_id, details,
                    created_at
                ) VALUES (
                    'audit-user', 'audit-user', 'publish_announcement',
                    'announcement-publish', '{"level":"warning"}', 8
                )
                """
            )
            db.execute(
                """
                INSERT INTO admin_audit_events(
                    actor_username, target_username, action, request_id,
                    created_at
                ) VALUES (
                    'audit-user', 'audit-user', 'clear_announcement',
                    'announcement-clear', 9
                )
                """
            )
            db.execute(
                """
                INSERT INTO admin_audit_events(
                    actor_username, target_username, action, request_id, details,
                    created_at
                ) VALUES (
                    'audit-user', 'audit-user', 'set_ai_work',
                    'ai-work-action', '{"enabled":false}', 7
                )
                """
            )
            db.execute(
                """
                INSERT INTO admin_audit_events(
                    actor_username, target_username, action, request_id, details,
                    created_at
                ) VALUES (
                    'audit-user', 'audit-user', 'set_registration',
                    'registration-action', '{"enabled":false}', 6
                )
                """
            )
            db.execute(
                """
                INSERT INTO admin_audit_events(
                    actor_username, target_username, action, request_id,
                    created_at
                ) VALUES (
                    'audit-user', 'audit-user', 'run_maintenance',
                    'maintenance-action', 5
                )
                """
            )
            db.execute(
                """
                INSERT INTO admin_audit_events(
                    actor_username, target_username, action, request_id,
                    created_at
                ) VALUES (
                    'audit-user', 'audit-user', 'create_backup', 'backup-action', 4
                )
                """
            )
            db.execute(
                """
                INSERT INTO admin_audit_events(
                    actor_username, target_username, action, request_id, details,
                    created_at
                ) VALUES (
                    'audit-user', 'audit-user', 'cancel_job', 'job-control',
                    '{"job_id":"job-1"}', 3
                )
                """
            )
        finally:
            db.close()

        self.assertEqual(tuple(preserved), ("ban", "before-v9"))

    def test_admin_page_exposes_account_recovery_actions(self) -> None:
        self.assertIn("Revoke all sessions", web_assets.ADMIN_HTML)
        self.assertIn("Clear login lock/failures", web_assets.ADMIN_HTML)
        self.assertIn("active_session_count", web_assets.ADMIN_HTML)


if __name__ == "__main__":
    import unittest

    unittest.main()
