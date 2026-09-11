from __future__ import annotations

import unittest

import main
from tests.helpers import DatabaseTestCase


class SecurityRecordCleanupTests(DatabaseTestCase):
    def test_cleanup_removes_only_expired_and_stale_records(self) -> None:
        now = 1_000_000
        user_id = self.create_user("cleanup")
        with main.connect_db() as db:
            db.executemany(
                "INSERT INTO login_sessions(user_id, token_hash, expires_at) VALUES (?, ?, ?)",
                [
                    (user_id, "expired-session", now),
                    (user_id, "active-session", now + 1),
                ],
            )
            db.executemany(
                """
                INSERT INTO registration_tokens(
                    email, token_hash, expires_at, requested_at
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    ("expired@example.test", "expired-registration", now - 1, now - 10),
                    ("active@example.test", "active-registration", now + 1, now),
                ],
            )
            db.executemany(
                """
                INSERT INTO password_reset_tokens(
                    user_id, token_hash, expires_at, requested_at
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (user_id, "expired-reset", now, now - 10),
                    (user_id, "active-reset", now + 1, now),
                ],
            )
            db.executemany(
                """
                INSERT INTO login_throttles(
                    scope_hash, failed_attempts, last_attempt_at, locked_until, updated_at
                ) VALUES (?, 1, ?, ?, ?)
                """,
                [
                    ("stale-login", now - 90_000, 0, now - 90_000),
                    ("active-login", now, now + 60, now),
                ],
            )
            db.executemany(
                """
                INSERT INTO auth_request_rate_limits(
                    scope_hash, action, window_started_at, attempt_count, updated_at
                ) VALUES (?, 'registration', ?, 1, ?)
                """,
                [
                    (
                        "stale-registration-rate",
                        now - 3 * main.REGISTRATION_IP_WINDOW_SECONDS,
                        now - 3 * main.REGISTRATION_IP_WINDOW_SECONDS,
                    ),
                    ("active-registration-rate", now, now),
                ],
            )
            db.executemany(
                """
                INSERT INTO admin_audit_events(
                    actor_user_id, actor_username, target_user_id, target_username,
                    action, request_id, created_at
                ) VALUES (?, 'cleanup', ?, 'target', 'ban', ?, ?)
                """,
                [
                    (
                        user_id,
                        user_id,
                        "expired-audit",
                        now - (main.ADMIN_AUDIT_RETENTION_DAYS * 24 * 60 * 60) - 1,
                    ),
                    (user_id, user_id, "active-audit", now),
                ],
            )

            deleted = main.prune_expired_security_records(db, now=now)

            self.assertEqual(
                deleted,
                {
                    "login_sessions": 1,
                    "registration_tokens": 1,
                    "password_reset_tokens": 1,
                    "login_throttles": 1,
                    "auth_request_rate_limits": 1,
                    "admin_audit_events": 1,
                },
            )
            for table in deleted:
                remaining = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                self.assertEqual(remaining, 1, table)

    def test_cleanup_indexes_are_installed(self) -> None:
        expected = {
            "registration_tokens_expires",
            "login_sessions_expires",
            "password_reset_tokens_expires",
            "login_throttles_updated",
            "auth_request_rate_limits_updated",
            "admin_audit_events_newest",
        }
        with main.connect_db() as db:
            installed = {
                row[0]
                for table in (
                    "registration_tokens",
                    "login_sessions",
                    "password_reset_tokens",
                    "login_throttles",
                    "auth_request_rate_limits",
                    "admin_audit_events",
                )
                for row in db.execute(f"SELECT name FROM pragma_index_list('{table}')")
            }

        self.assertTrue(expected <= installed)


if __name__ == "__main__":
    unittest.main()
