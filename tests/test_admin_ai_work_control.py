from __future__ import annotations

import json
import unittest

from fastapi import HTTPException

import main
import web_assets
from tests.helpers import DatabaseTestCase


class AdminAiWorkControlTests(DatabaseTestCase):
    def privileged_user(self, suffix: str, *, owner: bool = False) -> int:
        user_id = self.create_user(suffix)
        with main.connect_db() as db:
            db.execute(
                "UPDATE users SET is_admin = 1, is_owner = ? WHERE id = ?",
                (int(owner), user_id),
            )
        return user_id

    def create_chat(self, user_id: int, chat_id: str = "paused-chat") -> None:
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES (?, ?, 'Paused chat')",
                (chat_id, user_id),
            )

    def test_owner_pauses_and_resumes_ai_work_with_audit_events(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        request = self.authenticated_request(
            owner_id, method="PATCH", path="/api/admin/settings/ai-work"
        )
        request.state.request_id = "pause-ai-work"

        paused = main.set_admin_ai_work(main.AdminAiWorkSetting(enabled=False), request)

        self.assertEqual(
            paused,
            {"message": "AI work is paused", "enabled": False, "changed": True},
        )
        self.assertFalse(main.ai_work_is_enabled())
        unchanged = main.set_admin_ai_work(
            main.AdminAiWorkSetting(enabled=False), request
        )
        self.assertFalse(unchanged["changed"])

        request.state.request_id = "resume-ai-work"
        resumed = main.set_admin_ai_work(
            main.AdminAiWorkSetting(enabled=True), request
        )
        self.assertTrue(resumed["enabled"])
        self.assertTrue(main.ai_work_is_enabled())
        with main.connect_db() as db:
            setting = db.execute(
                """
                SELECT value, updated_by_user_id FROM application_settings
                WHERE key = 'ai_work_enabled'
                """
            ).fetchone()
            events = db.execute(
                "SELECT request_id, details FROM admin_audit_events ORDER BY id"
            ).fetchall()
        self.assertEqual(tuple(setting), ("1", owner_id))
        self.assertEqual(len(events), 2)
        self.assertEqual(
            [event["request_id"] for event in events],
            ["pause-ai-work", "resume-ai-work"],
        )
        self.assertEqual(json.loads(events[0]["details"]), {"enabled": False})
        self.assertEqual(json.loads(events[1]["details"]), {"enabled": True})

    def test_ordinary_administrator_cannot_change_ai_work_state(self) -> None:
        admin_id = self.privileged_user("admin")
        with self.assertRaises(HTTPException) as error:
            main.set_admin_ai_work(
                main.AdminAiWorkSetting(enabled=False),
                self.authenticated_request(admin_id, method="PATCH"),
            )
        self.assertEqual(error.exception.status_code, 403)
        self.assertTrue(main.ai_work_is_enabled())

    def test_paused_state_blocks_new_chat_without_storing_input(self) -> None:
        user_id = self.create_user("member")
        self.create_chat(user_id)
        with main.connect_db() as db:
            db.execute(
                "UPDATE application_settings SET value = '0' WHERE key = 'ai_work_enabled'"
            )

        with self.assertRaises(HTTPException) as error:
            main.chat(
                main.ChatRequest(chat_id="paused-chat", message="do not store this"),
                self.authenticated_request(user_id, method="POST", path="/api/chat"),
            )

        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(error.exception.headers["Retry-After"], "60")
        with main.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM chat_jobs").fetchone()[0], 0)

    def test_paused_state_blocks_verification_retries(self) -> None:
        user_id = self.create_user("member")
        self.create_chat(user_id)
        with main.connect_db() as db:
            db.execute(
                "UPDATE application_settings SET value = '0' WHERE key = 'ai_work_enabled'"
            )

        with self.assertRaises(HTTPException) as error:
            main.retry_chat_verification(
                "paused-chat",
                self.authenticated_request(user_id, method="POST"),
            )
        self.assertEqual(error.exception.status_code, 503)

    def test_pausing_does_not_cancel_existing_jobs(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        member_id = self.create_user("member")
        self.create_chat(member_id, "existing-job-chat")
        with main.connect_db() as db:
            db.execute(
                """
                INSERT INTO chat_jobs(id, user_id, chat_id, message, status)
                VALUES ('existing-job', ?, 'existing-job-chat', 'already queued', 'queued')
                """,
                (member_id,),
            )

        main.set_admin_ai_work(
            main.AdminAiWorkSetting(enabled=False),
            self.authenticated_request(owner_id, method="PATCH"),
        )

        with main.connect_db() as db:
            job = db.execute(
                "SELECT status, cancel_requested FROM chat_jobs WHERE id = 'existing-job'"
            ).fetchone()
        self.assertEqual(tuple(job), ("queued", 0))

    def test_admin_page_exposes_owner_only_ai_work_control(self) -> None:
        for marker in (
            "Pause AI work",
            "/api/admin/settings/ai-work",
            "aiWorkButton.hidden=false",
            "set_ai_work:'Changed AI work'",
        ):
            self.assertIn(marker, web_assets.ADMIN_HTML)


if __name__ == "__main__":
    unittest.main()
