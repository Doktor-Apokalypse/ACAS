from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks, Request

import main
from tests.helpers import DatabaseTestCase


def request_from(address: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/forgot-password",
            "headers": [],
            "scheme": "http",
            "server": ("127.0.0.1", 8000),
            "client": (address, 50000),
        }
    )


class EmailActionThrottleTests(DatabaseTestCase):
    def test_generic_email_response_waits_only_for_remaining_floor(self) -> None:
        with patch.object(main, "AUTH_EMAIL_RESPONSE_FLOOR_SECONDS", 0.25), patch.object(
            main.time, "monotonic", return_value=10.1
        ), patch.object(main.time, "sleep") as sleep:
            response = main.generic_auth_email_response(10.0, "Generic response")

        self.assertEqual(response, {"message": "Generic response"})
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.15)

    def test_generic_email_response_does_not_add_delay_after_floor(self) -> None:
        with patch.object(main, "AUTH_EMAIL_RESPONSE_FLOOR_SECONDS", 0.25), patch.object(
            main.time, "monotonic", return_value=10.3
        ), patch.object(main.time, "sleep") as sleep:
            main.generic_auth_email_response(10.0, "Generic response")

        sleep.assert_not_called()

    def test_password_reset_ip_allowance_is_persistent_and_bounded(self) -> None:
        request = request_from("192.0.2.10")
        with patch.object(main, "PASSWORD_RESET_MAX_REQUESTS_PER_IP", 2):
            self.assertTrue(main.reserve_password_reset_ip_attempt(request))
            self.assertTrue(main.reserve_password_reset_ip_attempt(request))
            self.assertFalse(main.reserve_password_reset_ip_attempt(request))

        with main.connect_db() as db:
            row = db.execute(
                """
                SELECT action, attempt_count FROM auth_request_rate_limits
                WHERE scope_hash = ?
                """,
                (main.token_digest("password_reset-ip:192.0.2.10"),),
            ).fetchone()
        self.assertEqual(tuple(row), ("password_reset", 3))

    def test_registration_and_reset_have_independent_allowances(self) -> None:
        request = request_from("192.0.2.20")
        with patch.object(main, "REGISTRATION_MAX_REQUESTS_PER_IP", 1), patch.object(
            main, "PASSWORD_RESET_MAX_REQUESTS_PER_IP", 1
        ):
            self.assertTrue(main.reserve_registration_ip_attempt(request))
            self.assertFalse(main.reserve_registration_ip_attempt(request))
            self.assertTrue(main.reserve_password_reset_ip_attempt(request))
            self.assertFalse(main.reserve_password_reset_ip_attempt(request))

    def test_limited_reset_request_returns_generic_response_without_new_token(self) -> None:
        user_id = self.create_user("reset_limit")
        request = request_from("192.0.2.30")
        payload = main.PasswordResetRequest(identity="reset_limit@example.test")
        with patch.object(
            main, "PASSWORD_RESET_MAX_REQUESTS_PER_IP", 1
        ), patch.object(main, "PUBLIC_BASE_URL", "https://chat.example.test"), patch.object(
            main, "AUTH_EMAIL_RESPONSE_FLOOR_SECONDS", 0.05
        ), patch.object(main.time, "sleep"):
            first = main.request_password_reset(payload, request, BackgroundTasks())
            second = main.request_password_reset(payload, request, BackgroundTasks())

        self.assertEqual(first, second)
        self.assertEqual(
            first["message"],
            "If that username or email address exists, a password reset link has been sent.",
        )
        with main.connect_db() as db:
            token_count = db.execute(
                "SELECT COUNT(*) FROM password_reset_tokens WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
        self.assertEqual(token_count, 1)


if __name__ == "__main__":
    unittest.main()
