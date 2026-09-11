from __future__ import annotations

import unittest

import web_assets


class AdminRemainingUiTests(unittest.TestCase):
    def test_admin_page_exposes_remaining_account_controls(self) -> None:
        for marker in (
            "Created",
            "Last login",
            "Temporary/permanent ban",
            "Send password-reset email",
            "/password-reset",
            "Configure user limits",
            "/limits",
            "Anonymize account and data",
            "Permanently delete account",
            "/account-data",
        ):
            self.assertIn(marker, web_assets.ADMIN_HTML)

    def test_owner_page_exposes_registration_audit_and_integrity_controls(self) -> None:
        for marker in (
            "Pending registrations",
            "/api/admin/registrations",
            "Check integrity",
            "/api/admin/database/integrity-check",
            "Export CSV",
            "/api/admin/audit/export",
            "audit-filters",
            "previous-audit",
            "next-audit",
        ):
            self.assertIn(marker, web_assets.ADMIN_HTML)

    def test_dynamic_admin_values_are_inserted_as_plain_text(self) -> None:
        self.assertIn("cell.textContent=value", web_assets.ADMIN_HTML)
        self.assertNotIn("innerHTML", web_assets.ADMIN_HTML)


if __name__ == "__main__":
    unittest.main()
