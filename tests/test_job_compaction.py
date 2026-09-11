from __future__ import annotations

import main
from tests.helpers import DatabaseTestCase


class TerminalJobCompactionTests(DatabaseTestCase):
    def test_old_terminal_payloads_are_compacted_without_breaking_polling(self) -> None:
        now = 2_000_000_000
        old_time = now - (main.TERMINAL_JOB_PAYLOAD_RETENTION_DAYS * 86_400) - 1
        user_id = self.create_user("job_compaction")
        chat_id = "compaction-chat"
        session_id = f"user-{user_id}:{chat_id}"
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES (?, ?, ?)",
                (chat_id, user_id, "Canonical title"),
            )
            user_message_id = int(
                db.execute(
                    "INSERT INTO messages(session_id, role, content) VALUES (?, 'user', ?)",
                    (session_id, "canonical prompt"),
                ).lastrowid
            )
            reply_message_id = int(
                db.execute(
                    "INSERT INTO messages(session_id, role, content) VALUES (?, 'assistant', ?)",
                    (session_id, "canonical reply"),
                ).lastrowid
            )
            db.execute(
                """
                INSERT INTO chat_jobs(
                    id, user_id, chat_id, message, status, reply, title,
                    user_message_id, reply_message_id, progress_log, updated_at
                ) VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, datetime(?, 'unixepoch'))
                """,
                (
                    "old-completed",
                    user_id,
                    chat_id,
                    "duplicate prompt",
                    "duplicate reply",
                    "Canonical title",
                    user_message_id,
                    reply_message_id,
                    '[{"message":"complete"}]',
                    old_time,
                ),
            )
            db.execute(
                """
                INSERT INTO chat_jobs(
                    id, user_id, chat_id, message, status, error, user_message_id,
                    progress_log, updated_at
                ) VALUES (?, ?, ?, ?, 'failed', ?, ?, ?, datetime(?, 'unixepoch'))
                """,
                (
                    "old-failed",
                    user_id,
                    chat_id,
                    "duplicate failed prompt",
                    "retained diagnostic",
                    user_message_id,
                    '[{"message":"failed"}]',
                    old_time,
                ),
            )
            db.execute(
                """
                INSERT INTO chat_jobs(
                    id, user_id, chat_id, message, status, reply, updated_at
                ) VALUES (?, ?, ?, ?, 'completed', ?, datetime(?, 'unixepoch'))
                """,
                (
                    "legacy-unlinked",
                    user_id,
                    chat_id,
                    "only legacy prompt",
                    "only legacy reply",
                    old_time,
                ),
            )
            db.execute(
                """
                INSERT INTO chat_jobs(id, user_id, chat_id, message, status, reply, updated_at)
                VALUES (?, ?, ?, ?, 'completed', ?, datetime(?, 'unixepoch'))
                """,
                ("recent", user_id, chat_id, "recent prompt", "recent reply", now),
            )
            db.execute(
                """
                INSERT INTO chat_jobs(id, user_id, chat_id, message, status, updated_at)
                VALUES (?, ?, ?, ?, 'queued', datetime(?, 'unixepoch'))
                """,
                ("active", user_id, chat_id, "active prompt", old_time),
            )

            compacted = main.compact_terminal_chat_job_payloads(db, now=now)

            jobs = {
                row["id"]: dict(row)
                for row in db.execute(
                    "SELECT id, status, message, reply, error, progress_log FROM chat_jobs"
                )
            }

        self.assertEqual(compacted, 2)
        self.assertIsNone(jobs["old-completed"]["message"])
        self.assertIsNone(jobs["old-completed"]["reply"])
        self.assertEqual(jobs["old-completed"]["progress_log"], "[]")
        self.assertEqual(jobs["old-failed"]["error"], "retained diagnostic")
        self.assertIsNone(jobs["old-failed"]["message"])
        self.assertEqual(jobs["legacy-unlinked"]["message"], "only legacy prompt")
        self.assertEqual(jobs["legacy-unlinked"]["reply"], "only legacy reply")
        self.assertEqual(jobs["recent"]["reply"], "recent reply")
        self.assertEqual(jobs["active"]["message"], "active prompt")

        request = self.authenticated_request(
            user_id, path="/api/chat-jobs/old-completed"
        )
        response = main.chat_job("old-completed", request)
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["reply"], "canonical reply")
        self.assertEqual(response["title"], "Canonical title")

    def test_terminal_job_maintenance_index_is_installed(self) -> None:
        with main.connect_db() as db:
            indexes = {
                row["name"] for row in db.execute("PRAGMA index_list('chat_jobs')")
            }
        self.assertIn("chat_jobs_terminal_updated", indexes)


if __name__ == "__main__":
    import unittest

    unittest.main()
