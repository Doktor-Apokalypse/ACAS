from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from fastapi import Request

import main
from tests.helpers import run_asgi_response


class RequestCorrelationTests(unittest.TestCase):
    def test_safe_upstream_request_id_is_preserved(self) -> None:
        status, headers, _body = run_asgi_response(
            method="GET",
            path="/health",
            request_id="edge-01:request_123",
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["x-request-id"], "edge-01:request_123")

    def test_malformed_request_id_is_replaced(self) -> None:
        _status, first_headers, _body = run_asgi_response(
            method="GET", path="/health", request_id="unsafe request id"
        )
        _status, second_headers, _body = run_asgi_response(
            method="GET", path="/health", request_id="unsafe request id"
        )

        first = first_headers["x-request-id"]
        second = second_headers["x-request-id"]
        self.assertRegex(first, r"^[0-9a-f]{32}$")
        self.assertRegex(second, r"^[0-9a-f]{32}$")
        self.assertNotEqual(first, second)

    def test_access_log_omits_sensitive_query_string(self) -> None:
        with self.assertLogs("uvicorn.error", level="INFO") as captured:
            run_asgi_response(
                method="GET",
                path="/health",
                query_string="token=do-not-log-this-secret",
                request_id="query-test",
            )

        log_output = "\n".join(captured.output)
        self.assertIn("request_id=query-test", log_output)
        self.assertIn("method=GET", log_output)
        self.assertIn("path=/health", log_output)
        self.assertIn("status=200", log_output)
        self.assertNotIn("do-not-log-this-secret", log_output)

    def test_successful_job_poll_is_not_logged(self) -> None:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/chat-jobs/job-123",
                "scheme": "http",
                "server": ("127.0.0.1", 8000),
                "headers": [],
            }
        )

        async def successful_poll(_request: Request):
            return main.Response(status_code=200)

        with patch.object(main.LOGGER, "info") as info_log:
            response = asyncio.run(
                main.correlate_and_log_request(request, successful_poll)
            )

        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.headers["x-request-id"], r"^[0-9a-f]{32}$")
        info_log.assert_not_called()

    def test_failed_job_poll_remains_logged(self) -> None:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/chat-jobs/missing-job",
                "scheme": "http",
                "server": ("127.0.0.1", 8000),
                "headers": [],
            }
        )

        async def failed_poll(_request: Request):
            return main.Response(status_code=404)

        with patch.object(main.LOGGER, "info") as info_log:
            response = asyncio.run(main.correlate_and_log_request(request, failed_poll))

        self.assertEqual(response.status_code, 404)
        info_log.assert_called_once()

    def test_unexpected_error_response_is_generic_and_correlated(self) -> None:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/failure",
                "scheme": "http",
                "server": ("127.0.0.1", 8000),
                "headers": [],
            }
        )
        request.state.request_id = "failure-test"

        with self.assertLogs("uvicorn.error", level="ERROR") as captured:
            response = asyncio.run(
                main.unexpected_server_error(request, ValueError("sensitive detail"))
            )
        payload = json.loads(response.body)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            payload,
            {
                "detail": "The server failed while processing the request.",
                "request_id": "failure-test",
            },
        )
        self.assertNotIn("sensitive detail", response.body.decode())
        self.assertIn("request_id=failure-test", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
