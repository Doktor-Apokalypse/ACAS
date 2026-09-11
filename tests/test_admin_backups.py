from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from fastapi import HTTPException

import main
import web_assets
from tests.helpers import DatabaseTestCase


class AdminBackupTests(DatabaseTestCase):
    def privileged_user(self, suffix: str, *, owner: bool = False) -> int:
        user_id = self.create_user(suffix)
        with main.connect_db() as db:
            db.execute(
                "UPDATE users SET is_admin = 1, is_owner = ? WHERE id = ?",
                (int(owner), user_id),
            )
        return user_id

    def test_owner_creates_verified_backup_and_audit_event(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        request = self.authenticated_request(
            owner_id, method="POST", path="/api/admin/backups"
        )
        request.state.request_id = "manual-backup-request"

        response = main.create_admin_database_backup(request)

        backups = main.periodic_backup_paths()
        self.assertEqual(len(backups), 1)
        self.assertEqual(response["message"], "Verified database backup created")
        self.assertEqual(response["backup_count"], 1)
        self.assertEqual(response["size_bytes"], backups[0].stat().st_size)
        self.assertNotIn("path", response)
        with closing(sqlite3.connect(backups[0])) as backup:
            self.assertEqual(backup.execute("PRAGMA quick_check").fetchone()[0], "ok")
        with main.connect_db() as db:
            audit = db.execute("SELECT * FROM admin_audit_events").fetchone()
        self.assertEqual(audit["actor_user_id"], owner_id)
        self.assertEqual(audit["action"], "create_backup")
        self.assertEqual(audit["request_id"], "manual-backup-request")
        details = json.loads(audit["details"])
        self.assertEqual(details["size_bytes"], response["size_bytes"])
        self.assertEqual(details["backup_count"], 1)

    def test_ordinary_administrator_cannot_create_backup(self) -> None:
        admin_id = self.privileged_user("admin")
        with self.assertRaises(HTTPException) as error:
            main.create_admin_database_backup(
                self.authenticated_request(admin_id, method="POST")
            )
        self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(main.periodic_backup_paths(), [])
        with main.connect_db() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM admin_audit_events").fetchone()[0],
                0,
            )

    def test_backup_failure_returns_safe_error_and_is_not_audited(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        with patch.object(
            main, "create_periodic_database_backup", side_effect=OSError("private path")
        ), patch.object(main.LOGGER, "error"):
            with self.assertRaises(HTTPException) as error:
                main.create_admin_database_backup(
                    self.authenticated_request(owner_id, method="POST")
                )
        self.assertEqual(error.exception.status_code, 500)
        self.assertEqual(error.exception.detail, "The database backup could not be created")
        self.assertNotIn("private path", error.exception.detail)
        with main.connect_db() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM admin_audit_events").fetchone()[0],
                0,
            )

    def test_admin_page_exposes_owner_only_backup_control(self) -> None:
        for marker in (
            "Create backup",
            "/api/admin/backups",
            "createBackupButton.hidden=false",
            "create_backup:'Created backup'",
        ):
            self.assertIn(marker, web_assets.ADMIN_HTML)


if __name__ == "__main__":
    unittest.main()
