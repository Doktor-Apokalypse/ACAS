from __future__ import annotations

import json
from unittest.mock import patch

import main
import web_assets
from tests.helpers import DatabaseTestCase


class ChatHistoryPaginationTests(DatabaseTestCase):
    def test_chat_messages_use_stable_cursor_pages_with_scoped_progress(self) -> None:
        user_id = self.create_user("pagination")
        chat_id = "long-chat"
        session_id = f"user-{user_id}:{chat_id}"
        message_ids: list[int] = []
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES (?, ?, 'Long chat')",
                (chat_id, user_id),
            )
            for index in range(1, 9):
                role = "user" if index % 2 else "assistant"
                message_ids.append(
                    int(
                        db.execute(
                            "INSERT INTO messages(session_id, role, content) VALUES (?, ?, ?)",
                            (session_id, role, f"message-{index}"),
                        ).lastrowid
                    )
                )
            for label, reply_index in (("old-progress", 1), ("new-progress", 7)):
                db.execute(
                    """
                    INSERT INTO chat_jobs(
                        id, user_id, chat_id, status, reply_message_id, progress_log
                    ) VALUES (?, ?, ?, 'completed', ?, ?)
                    """,
                    (
                        label,
                        user_id,
                        chat_id,
                        message_ids[reply_index],
                        json.dumps([{"message": label, "elapsed": 1}]),
                    ),
                )

        request = self.authenticated_request(user_id, path=f"/api/chats/{chat_id}")
        with patch.object(main, "CHAT_HISTORY_PAGE_SIZE", 3):
            newest = main.load_chat(chat_id, request)
            middle = main.load_chat(chat_id, request, before=newest["next_before"])
            oldest = main.load_chat(chat_id, request, before=middle["next_before"])

        def contents(page: dict[str, object]) -> list[str]:
            return [
                entry["content"]
                for entry in page["messages"]
                if entry["role"] != "progress"
            ]

        self.assertEqual(contents(newest), ["message-6", "message-7", "message-8"])
        self.assertEqual(contents(middle), ["message-3", "message-4", "message-5"])
        self.assertEqual(contents(oldest), ["message-1", "message-2"])
        self.assertEqual(newest["next_before"], message_ids[5])
        self.assertEqual(middle["next_before"], message_ids[2])
        self.assertIsNone(oldest["next_before"])

        newest_progress = [
            entry for entry in newest["messages"] if entry["role"] == "progress"
        ]
        oldest_progress = [
            entry for entry in oldest["messages"] if entry["role"] == "progress"
        ]
        self.assertEqual(newest_progress[0]["events"][0]["message"], "new-progress")
        self.assertEqual(oldest_progress[0]["events"][0]["message"], "old-progress")

    def test_chat_page_remains_private_to_its_owner(self) -> None:
        owner_id = self.create_user("chat_owner")
        other_id = self.create_user("chat_other")
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('private', ?, 'Private')",
                (owner_id,),
            )
        request = self.authenticated_request(other_id, path="/api/chats/private")

        with self.assertRaises(main.HTTPException) as raised:
            main.load_chat("private", request)

        self.assertEqual(raised.exception.status_code, 404)

    def test_browser_exposes_incremental_history_control(self) -> None:
        self.assertIn("Load earlier messages", web_assets.HTML)
        self.assertIn("?before=", web_assets.HTML)


if __name__ == "__main__":
    import unittest

    unittest.main()
