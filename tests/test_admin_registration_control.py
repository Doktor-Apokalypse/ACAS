from __future__ import annotations

import json
import time
import unittest

from fastapi import BackgroundTasks, HTTPException, Request

import main
import web_assets
from tests.helpers import DatabaseTestCase


class AdminRegistrationControlTests(DatabaseTestCase):
    def privileged_user(self, suffix: str, *, owner: bool = False) -> int:
        user_id = self.create_user(suffix)
        with main.connect_db() as db:
            db.execute(
                "UPDATE users SET is_admin = 1, is_owner = ? WHERE id = ?",
                (int(owner), user_id),
            )
        return user_id

    @staticmethod
    def anonymous_request(path: str, *, method: str = "GET") -> Request:
        request = Request(
            {
                "type": "http",
                "method": method,
                "path": path,
                "headers": [(b"host", b"testserver")],
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("127.0.0.1", 12345),
            }
        )
        request.state.csp_nonce = "test-nonce"
        return request

    def test_owner_closes_and_reopens_registration_with_audit_events(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        request = self.authenticated_request(
            owner_id, method="PATCH", path="/api/admin/settings/registration"
        )
        request.state.request_id = "close-registration"

        closed = main.set_admin_registration(
            main.AdminRegistrationSetting(enabled=False), request
        )

        self.assertEqual(
            closed,
            {"message": "Registration is closed", "enabled": False, "changed": True},
        )
        self.assertFalse(main.registration_is_enabled())
        unchanged = main.set_admin_registration(
            main.AdminRegistrationSetting(enabled=False), request
        )
        self.assertFalse(unchanged["changed"])

        request.state.request_id = "open-registration"
        opened = main.set_admin_registration(
            main.AdminRegistrationSetting(enabled=True), request
        )
        self.assertTrue(opened["enabled"])
        self.assertTrue(main.registration_is_enabled())
        with main.connect_db() as db:
            setting = db.execute(
                "SELECT value, updated_by_user_id FROM application_settings"
            ).fetchone()
            events = db.execute(
                "SELECT action, request_id, details FROM admin_audit_events ORDER BY id"
            ).fetchall()
        self.assertEqual(tuple(setting), ("1", owner_id))
        self.assertEqual(len(events), 2)
        self.assertEqual(
            [event["request_id"] for event in events],
            ["close-registration", "open-registration"],
        )
        self.assertEqual(json.loads(events[0]["details"]), {"enabled": False})
        self.assertEqual(json.loads(events[1]["details"]), {"enabled": True})

    def test_ordinary_administrator_cannot_change_registration(self) -> None:
        admin_id = self.privileged_user("admin")
        with self.assertRaises(HTTPException) as error:
            main.set_admin_registration(
                main.AdminRegistrationSetting(enabled=False),
                self.authenticated_request(admin_id, method="PATCH"),
            )
        self.assertEqual(error.exception.status_code, 403)
        self.assertTrue(main.registration_is_enabled())

    def test_closed_registration_blocks_requests_and_unfinished_links(self) -> None:
        token = "unfinished-registration-token-123456789"
        with main.connect_db() as db:
            db.execute(
                "UPDATE application_settings SET value = '0' WHERE key = 'registration_enabled'"
            )
            db.execute(
                """
                INSERT INTO registration_tokens(email, token_hash, expires_at, requested_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    "pending@example.test",
                    main.token_digest(token),
                    int(time.time()) + 300,
                    int(time.time()),
                ),
            )

        with self.assertRaises(HTTPException) as request_error:
            main.request_registration(
                main.RegistrationRequest(email="another@example.test"),
                self.anonymous_request("/api/register", method="POST"),
                BackgroundTasks(),
            )
        self.assertEqual(request_error.exception.status_code, 403)

        with self.assertRaises(HTTPException) as completion_error:
            main.complete_registration(
                main.CompleteRegistrationRequest(
                    token=token,
                    username="pending_user",
                    password="river stones drift brightly",
                )
            )
        self.assertEqual(completion_error.exception.status_code, 403)
        with main.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
            pending = db.execute(
                "SELECT used_at FROM registration_tokens WHERE token_hash = ?",
                (main.token_digest(token),),
            ).fetchone()
        self.assertIsNone(pending["used_at"])

    def test_registration_page_reports_closed_state(self) -> None:
        with main.connect_db() as db:
            db.execute(
                "UPDATE application_settings SET value = '0' WHERE key = 'registration_enabled'"
            )

        response = main.registration_page(self.anonymous_request("/register"))

        self.assertEqual(response.status_code, 403)
        self.assertIn(b"Registration is closed", response.body)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_admin_page_exposes_owner_only_registration_control(self) -> None:
        for marker in (
            "Close registration",
            "/api/admin/settings/registration",
            "registrationButton.hidden=false",
            "set_registration:'Changed registration'",
        ):
            self.assertIn(marker, web_assets.ADMIN_HTML)


if __name__ == "__main__":
    unittest.main()
