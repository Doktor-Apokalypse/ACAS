from __future__ import annotations

import json
import time
import unittest

from fastapi import HTTPException, Response

import main
import web_assets
from tests.helpers import DatabaseTestCase


class AdminAnnouncementTests(DatabaseTestCase):
    def privileged_user(self, suffix: str, *, owner: bool = False) -> int:
        user_id = self.create_user(suffix)
        with main.connect_db() as db:
            db.execute(
                "UPDATE users SET is_admin = 1, is_owner = ? WHERE id = ?",
                (int(owner), user_id),
            )
        return user_id

    def test_owner_publishes_and_clears_audited_announcement(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        request = self.authenticated_request(
            owner_id, method="PUT", path="/api/admin/announcement"
        )
        request.state.request_id = "publish-announcement"

        published = main.publish_admin_announcement(
            main.AdminAnnouncementSetting(
                message="  Planned maintenance tonight.  ",
                level="warning",
                expires_in_hours=6,
            ),
            request,
        )

        announcement = published["announcement"]
        self.assertEqual(published["message"], "Site announcement published")
        self.assertEqual(announcement["message"], "Planned maintenance tonight.")
        self.assertEqual(announcement["level"], "warning")
        self.assertGreater(announcement["expires_at"], int(time.time()))
        self.assertEqual(main.active_announcement()["id"], announcement["id"])

        request.state.request_id = "clear-announcement"
        cleared = main.clear_admin_announcement(request)

        self.assertEqual(cleared, {"message": "Site announcement cleared", "changed": True})
        self.assertIsNone(main.active_announcement())
        with main.connect_db() as db:
            events = db.execute(
                "SELECT action, request_id, details FROM admin_audit_events ORDER BY id"
            ).fetchall()
        self.assertEqual(
            [event["action"] for event in events],
            ["publish_announcement", "clear_announcement"],
        )
        self.assertEqual(
            [event["request_id"] for event in events],
            ["publish-announcement", "clear-announcement"],
        )
        publish_details = json.loads(events[0]["details"])
        self.assertEqual(publish_details["level"], "warning")
        self.assertEqual(publish_details["announcement_id"], announcement["id"])

    def test_signed_in_user_can_read_active_announcement_without_path_or_markup(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        member_id = self.create_user("member")
        main.publish_admin_announcement(
            main.AdminAnnouncementSetting(
                message="<img src=x onerror=alert(1)>", level="critical"
            ),
            self.authenticated_request(owner_id, method="PUT"),
        )
        response = Response()

        result = main.get_current_announcement(
            self.authenticated_request(member_id, path="/api/announcement"), response
        )

        self.assertEqual(
            result["announcement"]["message"], "<img src=x onerror=alert(1)>"
        )
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("path", result["announcement"])
        self.assertIn("announcementMessage.textContent", web_assets.HTML)
        self.assertNotIn("announcementMessage.innerHTML", web_assets.HTML)

    def test_expired_announcement_is_not_returned(self) -> None:
        value = {
            "id": "expired-announcement",
            "message": "Old notice",
            "level": "info",
            "expires_at": 100,
        }
        with main.connect_db() as db:
            db.execute(
                """
                INSERT INTO application_settings(key, value, updated_at)
                VALUES ('announcement', ?, 50)
                """,
                (json.dumps(value),),
            )

        self.assertIsNone(main.active_announcement(now=101))

    def test_ordinary_administrator_cannot_manage_announcements(self) -> None:
        admin_id = self.privileged_user("admin")
        request = self.authenticated_request(admin_id, method="PUT")
        with self.assertRaises(HTTPException) as publish_error:
            main.publish_admin_announcement(
                main.AdminAnnouncementSetting(message="Not allowed"), request
            )
        self.assertEqual(publish_error.exception.status_code, 403)
        with self.assertRaises(HTTPException) as clear_error:
            main.clear_admin_announcement(request)
        self.assertEqual(clear_error.exception.status_code, 403)

    def test_whitespace_only_announcement_is_rejected(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        with self.assertRaises(HTTPException) as error:
            main.publish_admin_announcement(
                main.AdminAnnouncementSetting(message="   "),
                self.authenticated_request(owner_id, method="PUT"),
            )
        self.assertEqual(error.exception.status_code, 422)
        self.assertIsNone(main.active_announcement())

    def test_announcement_poll_is_excluded_from_routine_access_logs(self) -> None:
        self.assertTrue(
            main.is_routine_successful_job_poll("GET", "/api/announcement", 200)
        )
        self.assertFalse(
            main.is_routine_successful_job_poll("GET", "/api/announcement", 401)
        )

    def test_admin_and_chat_pages_expose_announcement_controls(self) -> None:
        for marker in (
            "Site announcement",
            "/api/admin/announcement",
            "announcementForm.hidden=false",
            "publish_announcement:'Published announcement'",
        ):
            self.assertIn(marker, web_assets.ADMIN_HTML)
        for marker in (
            "site-announcement",
            "/api/announcement",
            "Dismiss announcement",
            "renderSiteAnnouncement",
        ):
            self.assertIn(marker, web_assets.HTML)


if __name__ == "__main__":
    unittest.main()
