from __future__ import annotations

import re
import unittest

from tests.helpers import DatabaseTestCase, run_asgi_response


class BrowserSecurityHeaderTests(DatabaseTestCase):
    def test_all_responses_receive_browser_security_headers(self) -> None:
        for request in (
            {"method": "GET", "path": "/health"},
            {"method": "GET", "path": "/health", "host": "attacker.example"},
            {"method": "POST", "path": "/api/login", "body": b"{}"},
        ):
            with self.subTest(request=request):
                _status, headers, _body = run_asgi_response(**request)
                self.assertEqual(headers["cache-control"], "no-store")
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertEqual(headers["x-frame-options"], "DENY")
                self.assertEqual(headers["referrer-policy"], "no-referrer")
                self.assertEqual(headers["cross-origin-opener-policy"], "same-origin")
                self.assertEqual(headers["cross-origin-resource-policy"], "same-origin")
                self.assertIn("camera=()", headers["permissions-policy"])
                self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
                self.assertIn("object-src 'none'", headers["content-security-policy"])

    def test_html_uses_matching_per_response_script_and_style_nonces(self) -> None:
        status, headers, body = run_asgi_response(method="GET", path="/login")
        document = body.decode()
        policy = headers["content-security-policy"]
        script_nonce = re.search(r"script-src 'nonce-([^']+)'", policy)
        style_nonce = re.search(r"style-src [^;]*'nonce-([^']+)'", policy)

        self.assertEqual(status, 200)
        self.assertIsNotNone(script_nonce)
        self.assertIsNotNone(style_nonce)
        self.assertEqual(script_nonce.group(1), style_nonce.group(1))
        nonce = script_nonce.group(1)
        self.assertIn(f'<script nonce="{nonce}">', document)
        self.assertIn(f'<style nonce="{nonce}">', document)
        self.assertNotIn("<script>", document)
        self.assertNotIn("<style>", document)
        self.assertNotIn("'unsafe-inline'", policy.split("script-src ", 1)[1].split(";", 1)[0])
        self.assertIn("script-src-attr 'none'", policy)

    def test_nonce_changes_between_responses(self) -> None:
        policies = [
            run_asgi_response(method="GET", path="/login")[1][
                "content-security-policy"
            ]
            for _ in range(2)
        ]
        nonces = [re.search(r"script-src 'nonce-([^']+)'", policy).group(1) for policy in policies]

        self.assertNotEqual(nonces[0], nonces[1])

    def test_hsts_is_emitted_only_for_https(self) -> None:
        _status, http_headers, _body = run_asgi_response(
            method="GET", path="/health"
        )
        _status, https_headers, _body = run_asgi_response(
            method="GET", path="/health", scheme="https"
        )
        _status, forwarded_headers, _body = run_asgi_response(
            method="GET", path="/health", forwarded_proto="https"
        )

        self.assertNotIn("strict-transport-security", http_headers)
        self.assertEqual(
            https_headers["strict-transport-security"], "max-age=31536000"
        )
        self.assertEqual(
            forwarded_headers["strict-transport-security"], "max-age=31536000"
        )

if __name__ == "__main__":
    unittest.main()
