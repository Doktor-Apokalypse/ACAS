from __future__ import annotations

from unittest.mock import patch

from fastapi import HTTPException

import main
import web_assets
from tests.helpers import DatabaseTestCase


class AdminUserSearchTests(DatabaseTestCase):
    def named_user(
        self,
        username: str,
        *,
        admin: bool = False,
        owner: bool = False,
        banned: bool = False,
    ) -> int:
        user_id = self.create_user(username.lower())
        with main.connect_db() as db:
            db.execute(
                """
                UPDATE users
                SET username = ?, email = ?, is_admin = ?, is_owner = ?, is_banned = ?
                WHERE id = ?
                """,
                (
                    username,
                    f"{username.lower()}@search.test",
                    int(admin or owner),
                    int(owner),
                    int(banned),
                    user_id,
                ),
            )
        return user_id

    def test_search_role_status_sorting_and_usage_are_server_side(self) -> None:
        admin_id = self.named_user("AdminViewer", admin=True)
        self.named_user("OwnerUser", owner=True)
        alpha_id = self.named_user("AlphaMember")
        beta_id = self.named_user("BetaMember", banned=True)
        with main.connect_db() as db:
            db.executemany(
                "INSERT INTO chat_histories(id, user_id, title) VALUES (?, ?, 'Chat')",
                [("alpha-1", alpha_id), ("alpha-2", alpha_id), ("beta-1", beta_id)],
            )
            db.execute(
                "INSERT INTO messages(session_id, role, content) VALUES (?, 'user', ?)",
                (f"user-{alpha_id}:alpha-1", "large alpha payload"),
            )

        request = self.authenticated_request(admin_id, path="/api/admin/users")
        searched = main.list_users(request, q="ALPHAMEMBER@SEARCH.TEST")
        banned_members = main.list_users(request, role="member", status="banned")
        by_chats = main.list_users(request, sort="chats", direction="desc")

        self.assertEqual([user["username"] for user in searched["users"]], ["AlphaMember"])
        self.assertEqual(
            [user["username"] for user in banned_members["users"]], ["BetaMember"]
        )
        self.assertEqual(by_chats["users"][0]["username"], "AlphaMember")
        self.assertEqual(by_chats["users"][0]["history_count"], 2)
        self.assertGreater(by_chats["users"][0]["history_size"], 0)

    def test_pages_are_bounded_stable_and_report_totals(self) -> None:
        admin_id = self.named_user("Viewer", admin=True)
        for username in ("Zulu", "Echo", "Bravo", "Charlie", "Delta"):
            self.named_user(username)
        request = self.authenticated_request(admin_id, path="/api/admin/users")

        received: list[str] = []
        with patch.object(main, "ADMIN_USER_PAGE_SIZE", 2):
            first = main.list_users(
                request, sort="username", direction="asc", page=1
            )
            for page_number in range(1, first["total_pages"] + 1):
                result = main.list_users(
                    request,
                    sort="username",
                    direction="asc",
                    page=page_number,
                )
                self.assertLessEqual(len(result["users"]), 2)
                received.extend(user["username"] for user in result["users"])

        self.assertEqual(received, sorted(received, key=str.casefold))
        self.assertEqual(len(received), 6)
        self.assertEqual(len(received), len(set(received)))
        self.assertEqual(first["page_size"], 2)
        self.assertEqual(first["total"], 6)
        self.assertEqual(first["total_pages"], 3)

    def test_non_administrator_cannot_search_users(self) -> None:
        member_id = self.named_user("OrdinaryMember")
        request = self.authenticated_request(member_id, path="/api/admin/users")

        with self.assertRaises(HTTPException) as raised:
            main.list_users(request)

        self.assertEqual(raised.exception.status_code, 403)

    def test_admin_page_contains_discovery_controls(self) -> None:
        for marker in (
            'id="user-search"',
            'id="role-filter"',
            'id="status-filter"',
            'id="user-sort"',
            'id="previous-users"',
            'id="next-users"',
        ):
            self.assertIn(marker, web_assets.ADMIN_HTML)


if __name__ == "__main__":
    import unittest

    unittest.main()
