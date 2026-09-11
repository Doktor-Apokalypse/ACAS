from __future__ import annotations

import json
import unittest

import main
from tests.helpers import DatabaseTestCase


class _AliveWorker:
    @staticmethod
    def is_alive() -> bool:
        return True


class _ModelListResponse:
    def __init__(self, models: list[str]):
        self.payload = json.dumps(
            {"models": [{"name": name} for name in models]}
        ).encode()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def read(self, limit: int) -> bytes:
        return self.payload[:limit]


class HealthProbeTests(DatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.original_worker = main.OLLAMA_WORKER_THREAD
        self.original_urlopen = main.urllib.request.urlopen
        self.original_cache = main.READINESS_CACHE
        main.OLLAMA_WORKER_THREAD = _AliveWorker()
        main.OLLAMA_WORKER_STOP.clear()
        main.READINESS_CACHE = None

    def tearDown(self) -> None:
        main.OLLAMA_WORKER_THREAD = self.original_worker
        main.urllib.request.urlopen = self.original_urlopen
        main.READINESS_CACHE = self.original_cache
        super().tearDown()

    def test_liveness_is_minimal_and_dependency_free(self) -> None:
        self.assertEqual(main.health(), {"status": "ok"})

    def test_readiness_succeeds_when_all_dependencies_are_available(self) -> None:
        main.urllib.request.urlopen = lambda _request, timeout: _ModelListResponse(
            [main.OLLAMA_MODEL]
        )

        report = main.build_readiness_report()

        self.assertEqual(report["status"], "ready")
        self.assertEqual(
            report["checks"],
            {
                "database": "ok",
                "job_worker": "ok",
                "ollama": "ok",
                "configured_model": "ok",
            },
        )
        self.assertEqual(main.readiness().status_code, 200)

    def test_readiness_fails_when_configured_model_is_missing(self) -> None:
        main.urllib.request.urlopen = lambda _request, timeout: _ModelListResponse(
            ["some-other-model:latest"]
        )

        report = main.build_readiness_report()

        self.assertEqual(report["status"], "not_ready")
        self.assertEqual(report["checks"]["ollama"], "ok")
        self.assertEqual(report["checks"]["configured_model"], "unavailable")

    def test_untagged_model_accepts_installed_latest_alias(self) -> None:
        original_model = main.OLLAMA_MODEL
        main.OLLAMA_MODEL = "example-model"
        main.urllib.request.urlopen = lambda _request, timeout: _ModelListResponse(
            ["example-model:latest"]
        )
        try:
            report = main.build_readiness_report()
        finally:
            main.OLLAMA_MODEL = original_model

        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["checks"]["configured_model"], "ok")

    def test_readiness_fails_when_worker_or_ollama_is_unavailable(self) -> None:
        main.OLLAMA_WORKER_THREAD = None

        def unavailable(_request, timeout):
            raise OSError("connection refused")

        main.urllib.request.urlopen = unavailable

        report = main.build_readiness_report()

        self.assertEqual(report["status"], "not_ready")
        self.assertEqual(report["checks"]["job_worker"], "unavailable")
        self.assertEqual(report["checks"]["ollama"], "unavailable")
        self.assertEqual(report["checks"]["configured_model"], "unknown")


if __name__ == "__main__":
    unittest.main()
