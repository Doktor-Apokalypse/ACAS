from __future__ import annotations

import base64
from unittest.mock import patch

from fastapi import HTTPException

import main
import web_assets
from tests.helpers import DatabaseTestCase


class ChatListPaginationTests(DatabaseTestCase):
    def test_composite_cursor_returns_every_chat_once_in_stable_order(self) -> None:
        user_id = self.create_user("chat_list")
        other_id = self.create_user("chat_list_other")
        records = [
            ("tie-c", "Tie C", "2026-01-03 10:00:00", "2026-01-01 10:00:00"),
            ("tie-a", "Tie A", "2026-01-03 10:00:00", "2026-01-01 10:00:00"),
            ("newer-created", "Newer created", "2026-01-03 10:00:00", "2026-01-02 10:00:00"),
            ("tie-b", "Tie B", "2026-01-03 10:00:00", "2026-01-01 10:00:00"),
            ("older", "Older", "2026-01-02 10:00:00", "2026-01-02 10:00:00"),
            ("oldest", "Oldest", "2026-01-01 10:00:00", "2026-01-01 10:00:00"),
        ]
        with main.connect_db() as db:
            db.executemany(
                """
                INSERT INTO chat_histories(id, user_id, title, updated_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [(chat_id, user_id, title, updated, created) for chat_id, title, updated, created in records],
            )
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('other-private', ?, 'Private')",
                (other_id,),
            )

        request = self.authenticated_request(user_id, path="/api/chats")
        cursor = None
        received: list[str] = []
        with patch.object(main, "CHAT_LIST_PAGE_SIZE", 2):
            while True:
                page = main.list_chats(request, cursor=cursor)
                self.assertLessEqual(len(page["chats"]), 2)
                received.extend(chat["id"] for chat in page["chats"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break

        expected = [
            item[0]
            for item in sorted(records, key=lambda item: (item[2], item[3], item[0]), reverse=True)
        ]
        self.assertEqual(received, expected)
        self.assertEqual(len(received), len(set(received)))
        self.assertNotIn("other-private", received)

    def test_malformed_or_structurally_invalid_cursor_is_rejected(self) -> None:
        user_id = self.create_user("bad_cursor")
        request = self.authenticated_request(user_id, path="/api/chats")
        wrong_shape = base64.urlsafe_b64encode(b'["only-one"]').decode().rstrip("=")

        for cursor in ("not-base64!", wrong_shape):
            with self.subTest(cursor=cursor), self.assertRaises(HTTPException) as raised:
                main.list_chats(request, cursor=cursor)
            self.assertEqual(raised.exception.status_code, 422)

    def test_browser_exposes_incremental_chat_list_control(self) -> None:
        self.assertIn("Load more chats", web_assets.HTML)
        self.assertIn("?cursor=", web_assets.HTML)

    def test_composite_chat_order_index_is_installed(self) -> None:
        with main.connect_db() as db:
            indexes = {
                row["name"] for row in db.execute("PRAGMA index_list('chat_histories')")
            }
        self.assertIn("chat_histories_user_order", indexes)


if __name__ == "__main__":
    import unittest

    unittest.main()
