from __future__ import annotations

import time
import unittest

from fastapi import HTTPException

import main
from tests.helpers import DatabaseTestCase, run_asgi_status


class AuthenticationLifecycleTests(DatabaseTestCase):
    def test_registration_token_is_single_use(self) -> None:
        token = "registration-token-value-123456789"
        with main.connect_db() as db:
            db.execute(
                """
                INSERT INTO registration_tokens(
                    email, token_hash, expires_at, requested_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    "new@example.test",
                    main.token_digest(token),
                    int(time.time()) + 300,
                    int(time.time()),
                ),
            )

        result = main.complete_registration(
            main.CompleteRegistrationRequest(
                token=token,
                username="new_user",
                password="river stones drift brightly",
            )
        )
        self.assertIn("Account created", result["message"])
        with main.connect_db() as db:
            user = db.execute(
                "SELECT password_hash FROM users WHERE email = ?", ("new@example.test",)
            ).fetchone()
            used_at = db.execute(
                "SELECT used_at FROM registration_tokens WHERE token_hash = ?",
                (main.token_digest(token),),
            ).fetchone()["used_at"]
        self.assertTrue(
            main.password_matches("river stones drift brightly", user["password_hash"])
        )
        self.assertIsNotNone(used_at)

        with self.assertRaises(HTTPException) as raised:
            main.complete_registration(
                main.CompleteRegistrationRequest(
                    token=token,
                    username="second_user",
                    password="another river stone phrase",
                )
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_password_reset_expires_token_and_sessions(self) -> None:
        user_id = self.create_user("reset")
        reset_token = "password-reset-token-value-12345"
        with main.connect_db() as db:
            db.execute(
                """
                INSERT INTO password_reset_tokens(
                    user_id, token_hash, expires_at, requested_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    user_id,
                    main.token_digest(reset_token),
                    int(time.time()) + 300,
                    int(time.time()),
                ),
            )
            db.execute(
                "INSERT INTO login_sessions(user_id, token_hash, expires_at) VALUES (?, ?, ?)",
                (user_id, main.token_digest("old-session"), int(time.time()) + 300),
            )

        main.complete_password_reset(
            main.CompletePasswordResetRequest(
                token=reset_token,
                password="Replacement-pass2!",
            )
        )
        with main.connect_db() as db:
            user = db.execute(
                "SELECT password_hash FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            sessions = db.execute(
                "SELECT COUNT(*) FROM login_sessions WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
        self.assertTrue(main.password_matches("Replacement-pass2!", user["password_hash"]))
        self.assertEqual(sessions, 0)
        with self.assertRaises(HTTPException):
            main.complete_password_reset(
                main.CompletePasswordResetRequest(
                    token=reset_token,
                    password="Another-valid-pass3!",
                )
            )


class OriginProtectionTests(unittest.TestCase):
    def test_trusted_and_untrusted_hosts(self) -> None:
        self.assertEqual(run_asgi_status(method="GET", path="/health"), 200)
        self.assertEqual(
            run_asgi_status(method="GET", path="/health", host="attacker.example"),
            400,
        )

    def test_unsafe_requests_require_same_origin(self) -> None:
        request = {
            "method": "POST",
            "path": "/api/login",
            "body": b"{}",
            "content_type": "application/json",
        }
        self.assertEqual(run_asgi_status(**request), 403)
        self.assertEqual(
            run_asgi_status(**request, origin="http://attacker.example"), 403
        )
        self.assertEqual(
            run_asgi_status(**request, origin="http://127.0.0.1:8000"), 422
        )


if __name__ == "__main__":
    unittest.main()
