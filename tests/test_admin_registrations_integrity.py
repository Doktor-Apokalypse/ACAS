from __future__ import annotations

import time
import unittest

from fastapi import HTTPException

import main
from tests.helpers import DatabaseTestCase


class AdminRegistrationAndIntegrityTests(DatabaseTestCase):
    def privileged_user(self, suffix: str, *, owner: bool = False) -> int:
        user_id = self.create_user(suffix)
        with main.connect_db() as db:
            db.execute(
                "UPDATE users SET is_admin = 1, is_owner = ? WHERE id = ?",
                (int(owner), user_id),
            )
        return user_id

    def test_owner_reviews_searches_and_revokes_pending_registration(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        now = int(time.time())
        with main.connect_db() as db:
            registration_id = int(
                db.execute(
                    """
                    INSERT INTO registration_tokens(
                        email, token_hash, expires_at, requested_at
                    ) VALUES ('pending@example.test', 'secret-digest', ?, ?)
                    """,
                    (now + 300, now),
                ).lastrowid
            )
        request = self.authenticated_request(owner_id)

        listed = main.list_pending_admin_registrations(request, q="PENDING@")
        revoked = main.revoke_admin_registration(registration_id, request)

        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["registrations"][0]["email"], "pending@example.test")
        self.assertNotIn("token_hash", listed["registrations"][0])
        self.assertIn("revoked", revoked["message"])
        with main.connect_db() as db:
            remaining = db.execute(
                "SELECT COUNT(*) FROM registration_tokens WHERE id = ?",
                (registration_id,),
            ).fetchone()[0]
            audit = db.execute("SELECT action FROM admin_audit_events").fetchone()
        self.assertEqual(remaining, 0)
        self.assertEqual(audit["action"], "revoke_registration")

    def test_registration_review_is_owner_only(self) -> None:
        admin_id = self.privileged_user("admin")
        with self.assertRaises(HTTPException) as raised:
            main.list_pending_admin_registrations(
                self.authenticated_request(admin_id)
            )
        self.assertEqual(raised.exception.status_code, 403)

    def test_owner_integrity_check_is_persisted_audited_and_owner_visible(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        request = self.authenticated_request(owner_id, method="POST")
        request.state.request_id = "integrity-request"

        result = main.run_admin_integrity_check(request)
        health = main.admin_system_health(self.authenticated_request(owner_id))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(health["database"]["integrity"]["status"], "ok")
        self.assertGreater(health["database"]["integrity"]["checked_at"], 0)
        with main.connect_db() as db:
            audit = db.execute("SELECT action, request_id FROM admin_audit_events").fetchone()
        self.assertEqual(tuple(audit), ("run_integrity_check", "integrity-request"))

    def test_integrity_history_is_not_exposed_to_ordinary_administrator(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        admin_id = self.privileged_user("admin")
        main.run_admin_integrity_check(self.authenticated_request(owner_id, method="POST"))

        health = main.admin_system_health(self.authenticated_request(admin_id))

        self.assertNotIn("integrity", health["database"])
        with self.assertRaises(HTTPException) as raised:
            main.run_admin_integrity_check(
                self.authenticated_request(admin_id, method="POST")
            )
        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
