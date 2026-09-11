from __future__ import annotations

import unittest

from fastapi import HTTPException

import main
from api_models import AdminUserAction
from tests.helpers import DatabaseTestCase


class AdminAuditTests(DatabaseTestCase):
    def privileged_user(self, suffix: str, *, owner: bool = False) -> int:
        user_id = self.create_user(suffix)
        with main.connect_db() as db:
            db.execute(
                "UPDATE users SET is_admin = 1, is_owner = ? WHERE id = ?",
                (int(owner), user_id),
            )
        return user_id

    def test_successful_admin_change_records_actor_target_action_and_request(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        target_id = self.create_user("target")
        request = self.authenticated_request(owner_id, method="PATCH")
        request.state.request_id = "audit-request-123"

        result = main.change_user(
            target_id, AdminUserAction(action="make_admin"), request
        )

        self.assertIn("was updated", result["message"])
        with main.connect_db() as db:
            target = db.execute(
                "SELECT username, is_admin FROM users WHERE id = ?", (target_id,)
            ).fetchone()
            event = db.execute("SELECT * FROM admin_audit_events").fetchone()
        self.assertEqual(target["is_admin"], 1)
        self.assertEqual(event["actor_user_id"], owner_id)
        self.assertEqual(event["target_user_id"], target_id)
        self.assertEqual(event["target_username"], target["username"])
        self.assertEqual(event["action"], "make_admin")
        self.assertEqual(event["request_id"], "audit-request-123")

    def test_rejected_change_does_not_create_an_audit_event(self) -> None:
        admin_id = self.privileged_user("admin")
        target_id = self.create_user("target")
        request = self.authenticated_request(admin_id, method="PATCH")

        with self.assertRaises(HTTPException) as raised:
            main.change_user(
                target_id, AdminUserAction(action="make_admin"), request
            )

        self.assertEqual(raised.exception.status_code, 403)
        with main.connect_db() as db:
            count = db.execute("SELECT COUNT(*) FROM admin_audit_events").fetchone()[0]
        self.assertEqual(count, 0)

    def test_only_owner_can_read_audit_events(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        admin_id = self.privileged_user("admin")
        target_id = self.create_user("target")
        change_request = self.authenticated_request(admin_id, method="PATCH")
        change_request.state.request_id = "ban-request"
        main.change_user(target_id, AdminUserAction(action="ban"), change_request)

        owner_request = self.authenticated_request(owner_id, path="/api/admin/audit")
        response = main.list_admin_audit_events(owner_request)
        self.assertEqual(len(response["events"]), 1)
        self.assertEqual(response["events"][0]["request_id"], "ban-request")

        admin_request = self.authenticated_request(admin_id, path="/api/admin/audit")
        with self.assertRaises(HTTPException) as raised:
            main.list_admin_audit_events(admin_request)
        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
