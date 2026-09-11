from __future__ import annotations

import hashlib
import unittest

from fastapi import Request, Response

import authentication
import main
from tests.helpers import DatabaseTestCase


def legacy_password_hash(password: str) -> str:
    salt = bytes.fromhex("00112233445566778899aabbccddeeff")
    derived = hashlib.scrypt(
        password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=64
    )
    return f"scrypt$16384$8$1${salt.hex()}${derived.hex()}"


class PasswordHashPrimitiveTests(unittest.TestCase):
    def test_new_hash_uses_current_bounded_profile(self) -> None:
        encoded = authentication.hash_password("Valid-pass1!")

        self.assertTrue(authentication.password_matches("Valid-pass1!", encoded))
        self.assertFalse(authentication.password_matches("Wrong-pass1!", encoded))
        self.assertFalse(authentication.password_needs_rehash(encoded))
        self.assertTrue(
            encoded.startswith(
                f"scrypt${authentication.SCRYPT_N}$"
                f"{authentication.SCRYPT_R}${authentication.SCRYPT_P}$"
            )
        )

    def test_legacy_hash_remains_valid_but_requires_upgrade(self) -> None:
        encoded = legacy_password_hash("Valid-pass1!")

        self.assertTrue(authentication.password_matches("Valid-pass1!", encoded))
        self.assertTrue(authentication.password_needs_rehash(encoded))

    def test_untrusted_parameters_are_rejected_before_allocation(self) -> None:
        salt = "00" * 16
        expected = "00" * 64
        malicious = f"scrypt${2**30}$8$1${salt}${expected}"

        self.assertIsNone(authentication.parse_scrypt_hash(malicious))
        self.assertFalse(authentication.password_matches("anything", malicious))
        self.assertFalse(
            authentication.password_matches("anything", "scrypt$broken")
        )
        oversized = f"scrypt$16384$8$1${'00' * 10_000}${expected}"
        self.assertIsNone(authentication.parse_scrypt_hash(oversized))


class PasswordHashUpgradeTests(DatabaseTestCase):
    def test_successful_login_upgrades_legacy_hash(self) -> None:
        password = "Valid-pass1!"
        old_hash = legacy_password_hash(password)
        with main.connect_db() as db:
            user_id = int(
                db.execute(
                    "INSERT INTO users(email, username, password_hash) VALUES (?, ?, ?)",
                    ("legacy@example.test", "legacy_user", old_hash),
                ).lastrowid
            )
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/login",
                "headers": [],
                "scheme": "http",
                "server": ("127.0.0.1", 8000),
                "client": ("127.0.0.1", 50000),
            }
        )
        response = Response()

        result = main.login(
            main.LoginRequest(username="legacy_user", password=password),
            request,
            response,
        )

        self.assertEqual(result["message"], "Logged in")
        with main.connect_db() as db:
            new_hash = db.execute(
                "SELECT password_hash FROM users WHERE id = ?", (user_id,)
            ).fetchone()[0]
        self.assertNotEqual(new_hash, old_hash)
        self.assertTrue(main.password_matches(password, new_hash))
        self.assertFalse(main.password_needs_rehash(new_hash))


if __name__ == "__main__":
    unittest.main()
