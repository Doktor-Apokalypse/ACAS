from __future__ import annotations

import unittest
from unittest.mock import patch

import app_config


class ConfigurationParsingTests(unittest.TestCase):
    def test_integer_errors_name_the_setting_and_expected_range(self) -> None:
        with self.assertRaisesRegex(
            app_config.ConfigurationError, "PORT must be an integer"
        ):
            app_config.parse_integer_setting("PORT", "eight thousand")
        with self.assertRaisesRegex(
            app_config.ConfigurationError, "PORT must be at most 65535"
        ):
            app_config.parse_integer_setting("PORT", "70000", maximum=65535)

    def test_float_rejects_non_finite_and_out_of_range_values(self) -> None:
        with self.assertRaisesRegex(
            app_config.ConfigurationError, "OLLAMA_TEMPERATURE must be finite"
        ):
            app_config.parse_float_setting("OLLAMA_TEMPERATURE", "nan")
        with self.assertRaisesRegex(
            app_config.ConfigurationError, "OLLAMA_TEMPERATURE must be at most 2"
        ):
            app_config.parse_float_setting(
                "OLLAMA_TEMPERATURE", "2.5", maximum=2
            )

    def test_boolean_accepts_explicit_values_and_rejects_typos(self) -> None:
        self.assertTrue(app_config.parse_boolean_setting("SMTP_USE_TLS", "yes"))
        self.assertFalse(app_config.parse_boolean_setting("SMTP_USE_TLS", "off"))
        with self.assertRaisesRegex(
            app_config.ConfigurationError, "SMTP_USE_TLS must be one of"
        ):
            app_config.parse_boolean_setting("SMTP_USE_TLS", "sometimes")


class ConfigurationRelationshipTests(unittest.TestCase):
    def test_adaptive_context_relationships_are_validated(self) -> None:
        with patch.object(app_config, "OLLAMA_ANALYSIS_CONTEXT_MIN", 65536), patch.object(app_config, "OLLAMA_ANALYSIS_CONTEXT_MAX", 32768):
            with self.assertRaisesRegex(app_config.ConfigurationError, "MIN cannot exceed"):
                app_config.validate_application_configuration()
        with patch.object(app_config, "OLLAMA_ADAPTIVE_ANALYSIS_CONTEXT", True), patch.object(app_config, "OLLAMA_ANALYSIS_CONTEXT_MAX", 8192):
            with self.assertRaisesRegex(app_config.ConfigurationError, "output allowance"):
                app_config.validate_application_configuration()

    def test_current_configuration_is_valid(self) -> None:
        app_config.validate_application_configuration()

    def test_related_limits_are_validated_together(self) -> None:
        with patch.object(app_config, "DIRECT_MESSAGE_CHARS", 101), patch.object(
            app_config, "MAX_MESSAGE_CHARS", 100
        ):
            with self.assertRaisesRegex(
                app_config.ConfigurationError,
                "DIRECT_MESSAGE_CHARS cannot exceed MAX_MESSAGE_CHARS",
            ):
                app_config.validate_application_configuration()

        with patch.object(
            app_config, "FUNCTION_ANALYSIS_CHUNK_CHARS", 201
        ), patch.object(app_config, "FUNCTION_ANALYSIS_MAX_SOURCE_CHARS", 200):
            with self.assertRaisesRegex(
                app_config.ConfigurationError,
                "FUNCTION_ANALYSIS_CHUNK_CHARS cannot exceed",
            ):
                app_config.validate_application_configuration()

    def test_multiple_configuration_errors_are_reported_together(self) -> None:
        with patch.object(app_config, "OLLAMA_URL", "file:///tmp/model"), patch.object(
            app_config, "SMTP_USE_TLS", True
        ), patch.object(app_config, "SMTP_USE_SSL", True), patch.object(
            app_config, "OLLAMA_GPT_OSS_REASONING", "extreme"
        ):
            with self.assertRaises(app_config.ConfigurationError) as raised:
                app_config.validate_application_configuration()

        message = str(raised.exception)
        self.assertIn("OLLAMA_URL", message)
        self.assertIn("SMTP_USE_TLS and SMTP_USE_SSL", message)
        self.assertIn("OLLAMA_GPT_OSS_REASONING", message)

    def test_ntfy_endpoint_and_topic_are_validated(self) -> None:
        with patch.object(app_config, "NTFY_SERVER_URL", "file:///tmp/alerts"), patch.object(
            app_config, "NTFY_TOPIC", "invalid/topic"
        ), patch.object(app_config, "NTFY_ACCESS_TOKEN", "token\r\ninjected"):
            with self.assertRaises(app_config.ConfigurationError) as raised:
                app_config.validate_application_configuration()

        message = str(raised.exception)
        self.assertIn("NTFY_SERVER_URL", message)
        self.assertIn("NTFY_TOPIC", message)
        self.assertIn("NTFY_ACCESS_TOKEN", message)


if __name__ == "__main__":
    unittest.main()
