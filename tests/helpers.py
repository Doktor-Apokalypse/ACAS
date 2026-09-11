from __future__ import annotations

import asyncio
import pathlib
import queue
import tempfile
import time
import unittest
import uuid

from fastapi import Request

import main


class DatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._original_db_path = main.DB_PATH
        self._original_migration_backup_dir = main.MIGRATION_BACKUP_DIR
        self._original_periodic_backup_dir = main.PERIODIC_BACKUP_DIR
        self._temporary_directory = tempfile.TemporaryDirectory()
        main.DB_PATH = pathlib.Path(self._temporary_directory.name) / "test.db"
        main.MIGRATION_BACKUP_DIR = (
            pathlib.Path(self._temporary_directory.name) / "migration_backups"
        )
        main.PERIODIC_BACKUP_DIR = (
            pathlib.Path(self._temporary_directory.name) / "periodic_backups"
        )
        main.initialise_db()

    def tearDown(self) -> None:
        main.stop_ollama_worker()
        main.stop_periodic_backup_worker()
        while True:
            try:
                main.OLLAMA_JOB_QUEUE.get_nowait()
            except queue.Empty:
                break
            else:
                main.OLLAMA_JOB_QUEUE.task_done()
        with main.JOB_CANCEL_LOCK:
            main.JOB_CANCEL_EVENTS.clear()
            main.JOB_PAUSE_EVENTS.clear()
        main.DB_PATH = self._original_db_path
        main.MIGRATION_BACKUP_DIR = self._original_migration_backup_dir
        main.PERIODIC_BACKUP_DIR = self._original_periodic_backup_dir
        self._temporary_directory.cleanup()

    def create_user(self, suffix: str = "user") -> int:
        with main.connect_db() as db:
            return int(
                db.execute(
                    "INSERT INTO users(email, username, password_hash) VALUES (?, ?, ?)",
                    (
                        f"{suffix}@example.test",
                        f"{suffix}_{uuid.uuid4().hex[:8]}",
                        main.hash_password("Valid-pass1!"),
                    ),
                ).lastrowid
            )

    def authenticated_request(
        self,
        user_id: int,
        *,
        method: str = "GET",
        path: str = "/",
    ) -> Request:
        token = uuid.uuid4().hex
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO login_sessions(user_id, token_hash, expires_at) VALUES (?, ?, ?)",
                (user_id, main.token_digest(token), int(time.time()) + 300),
            )
        return Request(
            {
                "type": "http",
                "method": method,
                "path": path,
                "headers": [(b"cookie", f"{main.SESSION_COOKIE}={token}".encode())],
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("127.0.0.1", 12345),
            }
        )


async def asgi_status(
    *,
    method: str,
    path: str,
    host: str = "127.0.0.1:8000",
    origin: str | None = None,
    body: bytes = b"",
    content_type: str | None = None,
    scheme: str = "http",
    forwarded_proto: str | None = None,
    request_id: str | None = None,
    query_string: str = "",
    cookie: str | None = None,
) -> int:
    status, _headers, _response_body = await asgi_response(
        method=method,
        path=path,
        host=host,
        origin=origin,
        body=body,
        content_type=content_type,
        scheme=scheme,
        forwarded_proto=forwarded_proto,
        request_id=request_id,
        query_string=query_string,
        cookie=cookie,
    )
    return status


async def asgi_response(
    *,
    method: str,
    path: str,
    host: str = "127.0.0.1:8000",
    origin: str | None = None,
    body: bytes = b"",
    content_type: str | None = None,
    scheme: str = "http",
    forwarded_proto: str | None = None,
    request_id: str | None = None,
    query_string: str = "",
    cookie: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    sent: list[dict[str, object]] = []
    delivered = False

    async def receive() -> dict[str, object]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    headers = [(b"host", host.encode())]
    if body:
        headers.append((b"content-length", str(len(body)).encode()))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if content_type is not None:
        headers.append((b"content-type", content_type.encode()))
    if forwarded_proto is not None:
        headers.append((b"x-forwarded-proto", forwarded_proto.encode()))
    if request_id is not None:
        headers.append((b"x-request-id", request_id.encode()))
    if cookie is not None:
        headers.append((b"cookie", cookie.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": scheme,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string.encode(),
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }
    await main.app(scope, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    response_headers = {
        bytes(name).decode("latin-1").lower(): bytes(value).decode("latin-1")
        for name, value in start.get("headers", [])
    }
    response_body = b"".join(
        bytes(message.get("body", b""))
        for message in sent
        if message["type"] == "http.response.body"
    )
    return int(start["status"]), response_headers, response_body


def run_asgi_status(**kwargs: object) -> int:
    return asyncio.run(asgi_status(**kwargs))


def run_asgi_response(**kwargs: object) -> tuple[int, dict[str, str], bytes]:
    return asyncio.run(asgi_response(**kwargs))
