from __future__ import annotations

from http.cookies import SimpleCookie
import time
import unittest
from unittest.mock import patch

from fastapi import Request, Response

import main
from tests.helpers import DatabaseTestCase


def session_request(
    token: str | None = None,
    *,
    forwarded_proto: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if token:
        headers.append((b"cookie", f"{main.SESSION_COOKIE}={token}".encode()))
    if forwarded_proto:
        headers.append((b"x-forwarded-proto", forwarded_proto.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/login",
            "headers": headers,
            "scheme": "http",
            "server": ("127.0.0.1", 8000),
            "client": ("127.0.0.1", 50000),
        }
    )


def response_session_token(response: Response) -> str:
    cookies = SimpleCookie()
    cookies.load(response.headers["set-cookie"])
    return cookies[main.SESSION_COOKIE].value


class SessionLifecycleTests(DatabaseTestCase):
    def test_login_rotates_the_session_presented_by_the_browser(self) -> None:
        user_id = self.create_user("rotation")
        old_token = "existing-browser-session"
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO login_sessions(user_id, token_hash, expires_at) VALUES (?, ?, ?)",
                (user_id, main.token_digest(old_token), int(time.time()) + 600),
            )
        response = Response()

        main.set_login_cookie(response, session_request(old_token), user_id)
        new_token = response_session_token(response)

        self.assertNotEqual(new_token, old_token)
        with main.connect_db() as db:
            old_count = db.execute(
                "SELECT COUNT(*) FROM login_sessions WHERE token_hash = ?",
                (main.token_digest(old_token),),
            ).fetchone()[0]
            new_count = db.execute(
                "SELECT COUNT(*) FROM login_sessions WHERE token_hash = ?",
                (main.token_digest(new_token),),
            ).fetchone()[0]
        self.assertEqual(old_count, 0)
        self.assertEqual(new_count, 1)

    def test_oldest_sessions_are_removed_above_per_user_limit(self) -> None:
        user_id = self.create_user("session_cap")
        issued_tokens: list[str] = []
        with patch.object(main, "MAX_SESSIONS_PER_USER", 2):
            for _ in range(3):
                response = Response()
                main.set_login_cookie(response, session_request(), user_id)
                issued_tokens.append(response_session_token(response))

        with main.connect_db() as db:
            remaining = {
                row[0]
                for row in db.execute(
                    "SELECT token_hash FROM login_sessions WHERE user_id = ?", (user_id,)
                )
            }
        self.assertEqual(len(remaining), 2)
        self.assertNotIn(main.token_digest(issued_tokens[0]), remaining)
        self.assertIn(main.token_digest(issued_tokens[1]), remaining)
        self.assertIn(main.token_digest(issued_tokens[2]), remaining)

    def test_secure_cookie_attributes_match_forwarded_https(self) -> None:
        user_id = self.create_user("secure_cookie")
        response = Response()

        main.set_login_cookie(
            response,
            session_request(forwarded_proto="https"),
            user_id,
        )

        set_cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", set_cookie)
        self.assertIn("secure", set_cookie)
        self.assertIn("samesite=lax", set_cookie)

    def test_https_logout_deletes_server_session_and_secure_cookie(self) -> None:
        user_id = self.create_user("secure_logout")
        token = "session-to-log-out"
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO login_sessions(user_id, token_hash, expires_at) VALUES (?, ?, ?)",
                (user_id, main.token_digest(token), int(time.time()) + 600),
            )
        response = Response()

        result = main.logout(
            session_request(token, forwarded_proto="https"),
            response,
        )

        self.assertEqual(result, {"message": "Logged out"})
        self.assertIn("secure", response.headers["set-cookie"].lower())
        with main.connect_db() as db:
            remaining = db.execute(
                "SELECT COUNT(*) FROM login_sessions WHERE token_hash = ?",
                (main.token_digest(token),),
            ).fetchone()[0]
        self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()
