from __future__ import annotations

import asyncio
import time
import unittest

from fastapi import HTTPException

import main
from tests.helpers import DatabaseTestCase


async def response_body(response) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
    return b"".join(chunks)


class AdminAuditExportTests(DatabaseTestCase):
    def privileged_user(self, suffix: str, *, owner: bool = False) -> int:
        user_id = self.create_user(suffix)
        with main.connect_db() as db:
            db.execute(
                "UPDATE users SET is_admin = 1, is_owner = ? WHERE id = ?",
                (int(owner), user_id),
            )
        return user_id

    def seed_events(self, actor_id: int, target_id: int) -> None:
        with main.connect_db() as db:
            actor = db.execute("SELECT * FROM users WHERE id = ?", (actor_id,)).fetchone()
            target = db.execute("SELECT * FROM users WHERE id = ?", (target_id,)).fetchone()
            main.record_admin_audit_event(
                db, actor, target, "ban", "request-ban", details={"reason": "test"}
            )
            main.record_admin_audit_event(
                db, actor, target, "unban", "request-unban"
            )

    def test_owner_filters_and_pages_audit_records(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        target_id = self.create_user("target")
        self.seed_events(owner_id, target_id)
        request = self.authenticated_request(owner_id, path="/api/admin/audit")

        result = main.list_admin_audit_events(
            request, q="request-ban", action="ban", page=1, page_size=1
        )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["total_pages"], 1)
        self.assertEqual(result["events"][0]["action"], "ban")
        self.assertIn("set_user_limits", result["actions"])

    def test_csv_export_streams_filtered_records_and_blocks_formula_cells(self) -> None:
        owner_id = self.privileged_user("owner", owner=True)
        target_id = self.create_user("target")
        with main.connect_db() as db:
            db.execute(
                """
                INSERT INTO admin_audit_events(
                    actor_user_id, actor_username, target_user_id, target_username,
                    action, request_id, details, created_at
                ) VALUES (?, 'owner', ?, '=spreadsheet-command', 'ban', 'csv-request',
                          '{"reason":"test"}', ?)
                """,
                (owner_id, target_id, int(time.time())),
            )
        response = main.export_admin_audit_events(
            self.authenticated_request(owner_id), action="ban"
        )

        body = asyncio.run(response_body(response)).decode("utf-8-sig")

        self.assertIn("time_utc,actor,action,target,details,request_id", body)
        self.assertIn("'=spreadsheet-command", body)
        self.assertIn("csv-request", body)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("attachment", response.headers["content-disposition"])

    def test_ordinary_admin_cannot_filter_or_export_audit(self) -> None:
        admin_id = self.privileged_user("admin")
        request = self.authenticated_request(admin_id)
        with self.assertRaises(HTTPException) as listing:
            main.list_admin_audit_events(request)
        with self.assertRaises(HTTPException) as exporting:
            main.export_admin_audit_events(request)
        self.assertEqual(listing.exception.status_code, 403)
        self.assertEqual(exporting.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
