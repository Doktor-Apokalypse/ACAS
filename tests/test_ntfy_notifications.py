from __future__ import annotations

import logging
import queue
import unittest
from unittest.mock import patch

import main


class FakeHttpResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size: int = -1) -> bytes:
        return b"{"


class NtfyErrorHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.notifications: queue.Queue[main.NtfyNotification] = queue.Queue(maxsize=10)
        self.handler = main.NtfyErrorHandler(self.notifications, dedup_seconds=60)
        self.logger = logging.Logger("tests.ntfy", level=logging.DEBUG)
        self.logger.propagate = False
        self.logger.addHandler(self.handler)

    def test_only_errors_are_queued_and_duplicates_are_suppressed(self) -> None:
        self.logger.warning("ordinary warning")
        self.assertTrue(self.notifications.empty())

        self.logger.error("database unavailable")
        self.logger.error("database unavailable")
        title, message, priority = self.notifications.get_nowait()

        self.assertEqual(title, "Apokalypse Coder Bot ERROR")
        self.assertIn("database unavailable", message)
        self.assertEqual(priority, "high")
        self.assertTrue(self.notifications.empty())

    def test_critical_errors_are_urgent_and_long_unicode_messages_are_bounded(self) -> None:
        self.logger.critical("failure %s", "☃" * 4_000)
        title, message, priority = self.notifications.get_nowait()

        self.assertEqual(title, "Apokalypse Coder Bot CRITICAL")
        self.assertEqual(priority, "urgent")
        self.assertLessEqual(len(message.encode("utf-8")), main.NTFY_MESSAGE_MAX_BYTES)
        self.assertIn("notification shortened", message)

    def test_full_queue_drops_the_alert_without_raising(self) -> None:
        notifications: queue.Queue[main.NtfyNotification] = queue.Queue(maxsize=1)
        handler = main.NtfyErrorHandler(notifications, dedup_seconds=0)
        logger = logging.Logger("tests.ntfy.full", level=logging.ERROR)
        logger.addHandler(handler)

        logger.error("first")
        logger.error("second")

        self.assertEqual(notifications.qsize(), 1)
        self.assertEqual(handler.dropped_notifications, 1)


class NtfyDeliveryTests(unittest.TestCase):
    def tearDown(self) -> None:
        main.stop_ntfy_error_notifier()

    def test_delivery_posts_to_configured_topic_with_headers_and_token(self) -> None:
        response = FakeHttpResponse()
        with patch.object(main, "NTFY_SERVER_URL", "https://notify.example"), patch.object(
            main, "NTFY_TOPIC", "admin_alerts"
        ), patch.object(main, "NTFY_ACCESS_TOKEN", "secret-token"), patch.object(
            main.urllib.request, "urlopen", return_value=response
        ) as urlopen:
            main.deliver_ntfy_notification(("Bot ERROR", "Something broke", "high"))

        request = urlopen.call_args.args[0]
        headers = dict(request.header_items())
        self.assertEqual(request.full_url, "https://notify.example/admin_alerts")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.data, b"Something broke")
        self.assertEqual(headers["Title"], "Bot ERROR")
        self.assertEqual(headers["Priority"], "high")
        self.assertEqual(headers["Authorization"], "Bearer secret-token")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], main.NTFY_TIMEOUT_SECONDS)

    def test_start_is_idempotent_and_stop_removes_the_handler(self) -> None:
        with patch.object(main, "NTFY_TOPIC", "admin_alerts"):
            main.start_ntfy_error_notifier()
            handler = main.NTFY_NOTIFICATION_HANDLER
            main.start_ntfy_error_notifier()

            self.assertIsNotNone(handler)
            self.assertEqual(main.LOGGER.handlers.count(handler), 1)
            self.assertTrue(main.NTFY_NOTIFICATION_THREAD.is_alive())

            main.stop_ntfy_error_notifier()

        self.assertNotIn(handler, main.LOGGER.handlers)


if __name__ == "__main__":
    unittest.main()
