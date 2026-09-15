from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import main
import web_assets
from tests.helpers import DatabaseTestCase


class _AliveWorker:
    @staticmethod
    def is_alive() -> bool:
        return True


class AdminSystemHealthTests(DatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.original_job_worker = main.OLLAMA_WORKER_THREAD
        self.original_maintenance_worker = main.MAINTENANCE_THREAD
        self.original_readiness_cache = main.READINESS_CACHE
        main.OLLAMA_WORKER_THREAD = _AliveWorker()
        main.MAINTENANCE_THREAD = _AliveWorker()
        main.OLLAMA_WORKER_STOP.clear()
        main.MAINTENANCE_STOP.clear()
        main.READINESS_CACHE = None

    def tearDown(self) -> None:
        main.OLLAMA_WORKER_THREAD = self.original_job_worker
        main.MAINTENANCE_THREAD = self.original_maintenance_worker
        main.READINESS_CACHE = self.original_readiness_cache
        super().tearDown()

    def privileged_user(self, suffix: str = "admin") -> int:
        user_id = self.create_user(suffix)
        with main.connect_db() as db:
            db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user_id,))
        return user_id

    def test_admin_health_report_contains_operational_data_without_secrets(self) -> None:
        admin_id = self.privileged_user()
        with patch.object(
            main,
            "installed_ollama_models",
            return_value={main.OLLAMA_MODEL.casefold()},
        ):
            report = main.admin_system_health(
                self.authenticated_request(admin_id, path="/api/admin/system-health")
            )

        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["checks"]["database"], "ok")
        self.assertTrue(report["jobs"]["worker_alive"])
        self.assertEqual(report["jobs"]["queued"], 0)
        self.assertEqual(report["database"]["schema_version"], 35)
        self.assertTrue(report["registration"]["enabled"])
        self.assertTrue(report["ai_work"]["enabled"])
        self.assertIsNone(report["announcement"])
        self.assertEqual(report["database"]["users"], 1)
        self.assertGreater(report["database"]["size_bytes"], 0)
        self.assertTrue(report["backups"]["worker_alive"])
        self.assertGreaterEqual(report["uptime_seconds"], 0)

        encoded = json.dumps(report)
        self.assertNotIn(str(main.DB_PATH), encoded)
        self.assertNotIn(main.OLLAMA_URL, encoded)
        self.assertNotIn("password", encoded.casefold())
        self.assertNotIn("token", encoded.casefold())

    def test_member_cannot_read_system_health(self) -> None:
        member_id = self.create_user("member")
        with self.assertRaises(HTTPException) as error:
            main.admin_system_health(self.authenticated_request(member_id))
        self.assertEqual(error.exception.status_code, 403)

    def test_admin_health_poll_is_excluded_from_routine_access_logs(self) -> None:
        self.assertTrue(
            main.is_routine_successful_job_poll(
                "GET", "/api/admin/system-health", 200
            )
        )
        self.assertFalse(
            main.is_routine_successful_job_poll(
                "GET", "/api/admin/system-health", 503
            )
        )

    def test_admin_page_exposes_system_health_dashboard(self) -> None:
        for marker in (
            "System health",
            "/api/admin/system-health",
            "Periodic backups",
            "renderHealth",
        ):
            self.assertIn(marker, web_assets.ADMIN_HTML)


if __name__ == "__main__":
    unittest.main()
