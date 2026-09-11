from __future__ import annotations

import unittest

from fastapi import HTTPException
from pydantic import ValidationError

import authentication
from api_models import CompletePasswordResetRequest, CompleteRegistrationRequest
from web_assets import RESET_PASSWORD_HTML, VERIFY_HTML


class PasswordPolicyTests(unittest.TestCase):
    def test_long_lowercase_passphrases_and_unicode_are_allowed(self) -> None:
        authentication.validate_password("orchids drift beyond amber skies")
        authentication.validate_password("静かな森を歩く長い合言葉で安全です")
        authentication.validate_password("spaces are welcome here")

    def test_short_password_is_rejected_without_composition_requirements(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            authentication.validate_password("Valid-pass1!")

        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("15-128", raised.exception.detail)

    def test_common_or_expected_password_is_rejected(self) -> None:
        for password in (
            "passwordpassword",
            " Password1234567 ",
            "apokalypsecoderbot",
            "correcthorsebatterystaple",
        ):
            with self.subTest(password=password):
                with self.assertRaises(HTTPException) as raised:
                    authentication.validate_password(password)
                self.assertIn("common or predictable", raised.exception.detail)

    def test_api_contract_enforces_new_length_for_creation_and_reset(self) -> None:
        with self.assertRaises(ValidationError):
            CompleteRegistrationRequest(
                token="registration-token-value-12345",
                username="new_user",
                password="Valid-pass1!",
            )
        with self.assertRaises(ValidationError):
            CompletePasswordResetRequest(
                token="password-reset-token-value-12345",
                password="Valid-pass1!",
            )

    def test_browser_forms_explain_the_same_policy(self) -> None:
        for document in (VERIFY_HTML, RESET_PASSWORD_HTML):
            self.assertIn('minlength="15"', document)
            self.assertIn("Long passphrases, spaces, and Unicode are allowed.", document)


if __name__ == "__main__":
    unittest.main()
