from __future__ import annotations

import json
import sqlite3
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import main
import web_assets
from tests.helpers import DatabaseTestCase


class AdminMaintenanceTests(DatabaseTestCase):
    def privileged_user(self, suffix: str, *, owner: bool = False) -> int:
        user_id = self.create_user(suffix)
        with main.connect_db() as db:
            db.execute(
                "UPDATE users SET is_admin = 1, is_owner = ? WHERE id = ?",
                (int(owner), user_id),
            )
        return user_id

    def test_owner_runs_retention_maintenance_and_records_audit_event(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        request = self.authenticated_request(
            owner_id, method="POST", path="/api/admin/maintenance"
        )
        request.state.request_id = "maintenance-request"
        with main.connect_db() as db:
            db.execute(
                """
                INSERT INTO registration_tokens(
                    email, token_hash, expires_at, requested_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    "expired-maintenance@example.test",
                    "expired-maintenance-registration",
                    int(time.time()) - 1,
                    int(time.time()) - 60,
                ),
            )

        response = main.run_admin_database_maintenance(request)

        self.assertEqual(response["message"], "Database maintenance completed")
        self.assertEqual(response["expired_records_removed"], 1)
        self.assertEqual(response["terminal_jobs_compacted"], 0)
        with main.connect_db() as db:
            expired = db.execute(
                "SELECT COUNT(*) FROM registration_tokens WHERE token_hash = ?",
                ("expired-maintenance-registration",),
            ).fetchone()[0]
            active = db.execute(
                "SELECT COUNT(*) FROM login_sessions WHERE user_id = ?", (owner_id,)
            ).fetchone()[0]
            audit = db.execute("SELECT * FROM admin_audit_events").fetchone()
        self.assertEqual(expired, 0)
        self.assertEqual(active, 1)
        self.assertEqual(audit["action"], "run_maintenance")
        self.assertEqual(audit["request_id"], "maintenance-request")
        self.assertEqual(
            json.loads(audit["details"]),
            {"expired_records": 1, "compacted_jobs": 0},
        )

    def test_ordinary_administrator_cannot_run_maintenance(self) -> None:
        admin_id = self.privileged_user("admin")
        with self.assertRaises(HTTPException) as error:
            main.run_admin_database_maintenance(
                self.authenticated_request(admin_id, method="POST")
            )
        self.assertEqual(error.exception.status_code, 403)
        with main.connect_db() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM admin_audit_events").fetchone()[0],
                0,
            )

    def test_maintenance_failure_returns_safe_error_and_is_not_audited(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        with patch.object(
            main,
            "run_database_maintenance",
            side_effect=sqlite3.OperationalError("private table"),
        ), patch.object(main.LOGGER, "error"):
            with self.assertRaises(HTTPException) as error:
                main.run_admin_database_maintenance(
                    self.authenticated_request(owner_id, method="POST")
                )
        self.assertEqual(error.exception.status_code, 500)
        self.assertEqual(error.exception.detail, "Database maintenance could not be completed")
        self.assertNotIn("private table", error.exception.detail)
        with main.connect_db() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM admin_audit_events").fetchone()[0],
                0,
            )

    def test_admin_page_exposes_owner_only_maintenance_control(self) -> None:
        for marker in (
            "Run maintenance",
            "/api/admin/maintenance",
            "maintenanceButton.hidden=false",
            "run_maintenance:'Ran maintenance'",
        ):
            self.assertIn(marker, web_assets.ADMIN_HTML)

if __name__ == "__main__":
    unittest.main()
