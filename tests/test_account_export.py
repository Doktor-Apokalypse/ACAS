from __future__ import annotations

import asyncio
import json

from fastapi import HTTPException

import main
import web_assets
from tests.helpers import DatabaseTestCase


async def read_stream(response: main.StreamingResponse) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.encode() if isinstance(chunk, str) else bytes(chunk))
    return b"".join(chunks)


class AccountExportTests(DatabaseTestCase):
    def test_export_streams_only_the_authenticated_users_portable_data(self) -> None:
        user_id = self.create_user("exporter")
        other_id = self.create_user("export_other")
        with main.connect_db() as db:
            account = db.execute(
                "SELECT username, email, password_hash FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            db.execute(
                """
                INSERT INTO chat_histories(
                    id, user_id, title, title_is_custom
                ) VALUES ('export-chat', ?, 'Exported chat', 1)
                """,
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO messages(session_id, role, content, context_content)
                VALUES (?, 'user', 'visible prompt', 'compact private context')
                """,
                (f"user-{user_id}:export-chat",),
            )
            db.execute(
                """
                INSERT INTO messages(session_id, role, content)
                VALUES (?, 'assistant', 'visible reply')
                """,
                (f"user-{user_id}:export-chat",),
            )
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('private-other', ?, 'Other')",
                (other_id,),
            )
            db.execute(
                """
                INSERT INTO projects(
                    id, user_id, chat_id, name, source_kind, file_count, total_bytes
                ) VALUES ('export-project', ?, 'export-chat', 'demo', 'folder', 1, 4)
                """,
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO project_files(
                    project_id, path, content, size_bytes, sha256, is_binary
                ) VALUES ('export-project', 'main.py', ?, 4, ?, 0)
                """,
                (b"pass", main.hashlib.sha256(b"pass").hexdigest()),
            )

        request = self.authenticated_request(user_id, path="/api/account/export")
        response = main.export_account_data(request)
        raw_export = asyncio.run(read_stream(response))
        exported = json.loads(raw_export)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("attachment;", response.headers["content-disposition"])
        self.assertEqual(exported["format_version"], 1)
        self.assertEqual(exported["account"]["username"], account["username"])
        self.assertEqual(exported["account"]["email"], account["email"])
        self.assertEqual([chat["id"] for chat in exported["chats"]], ["export-chat"])
        self.assertEqual(
            [message["content"] for message in exported["chats"][0]["messages"]],
            ["visible prompt", "visible reply"],
        )
        self.assertEqual(
            exported["chats"][0]["messages"][0]["context_content"],
            "compact private context",
        )
        self.assertEqual(exported["chats"][0]["projects"][0]["name"], "demo")
        exported_file = exported["chats"][0]["projects"][0]["files"][0]
        self.assertEqual(exported_file["path"], "main.py")
        self.assertEqual(exported_file["content_base64"], "cGFzcw==")
        self.assertNotIn(account["password_hash"].encode(), raw_export)
        self.assertNotIn(b"private-other", raw_export)
        self.assertNotIn(b"token_hash", raw_export)

    def test_export_requires_authentication(self) -> None:
        request = main.Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/account/export",
                "headers": [],
                "scheme": "http",
                "server": ("testserver", 80),
            }
        )
        with self.assertRaises(HTTPException) as raised:
            main.export_account_data(request)
        self.assertEqual(raised.exception.status_code, 401)

    def test_export_crosses_internal_chat_and_message_page_boundaries(self) -> None:
        user_id = self.create_user("large_export")
        session_id = f"user-{user_id}:chat-000"
        with main.connect_db() as db:
            db.executemany(
                """
                INSERT INTO chat_histories(id, user_id, title)
                VALUES (?, ?, ?)
                """,
                [
                    (f"chat-{index:03d}", user_id, f"Chat {index}")
                    for index in range(101)
                ],
            )
            db.executemany(
                "INSERT INTO messages(session_id, role, content) VALUES (?, 'user', ?)",
                [(session_id, f"Message {index}") for index in range(105)],
            )

        request = self.authenticated_request(user_id, path="/api/account/export")
        exported = json.loads(asyncio.run(read_stream(main.export_account_data(request))))

        self.assertEqual(len(exported["chats"]), 101)
        first_chat = next(chat for chat in exported["chats"] if chat["id"] == "chat-000")
        self.assertEqual(len(first_chat["messages"]), 105)
        self.assertEqual(first_chat["messages"][-1]["content"], "Message 104")

    def test_browser_header_links_to_account_export(self) -> None:
        self.assertIn('href="/api/account/export"', web_assets.HTML)


if __name__ == "__main__":
    import unittest

    unittest.main()
