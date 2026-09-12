"""Ollama chat server with persistent per-session memory and optional ngrok.

Run locally:
    pip install fastapi uvicorn pyngrok
    ollama pull deepseek-coder-v2:16B
    python main.py

To expose it through ngrok (PowerShell):
    $env:NGROK_AUTHTOKEN = "your-token"
    $env:SMTP_HOST = "smtp.example.com"
    $env:SMTP_USERNAME = "your-email@example.com"
    $env:SMTP_PASSWORD = "your-app-password"
    $env:SMTP_FROM = "your-email@example.com"
    $env:PUBLIC_BASE_URL = "https://chat.example.com"
    $env:TRUSTED_HOSTS = "chat.example.com"
    python main.py

PUBLIC_BASE_URL is set automatically when this script starts its own ngrok tunnel.
"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import html
import io
import json
import logging
import os
import queue
import re
import secrets
import smtplib
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Annotated, Callable, Literal

import uvicorn
from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from api_models import (
    AdminAccountDisposition,
    AdminAnnouncementSetting,
    AdminAiWorkSetting,
    AdminRegistrationSetting,
    AdminUserAction,
    AdminUserLimits,
    ChatRequest,
    ChatResponse,
    CompletePasswordResetRequest,
    CompleteRegistrationRequest,
    LoginRequest,
    PasswordResetRequest,
    ProjectAnalysisRequest,
    ProjectEntryDelete,
    ProjectMainFile,
    RegistrationRequest,
    RenameChatRequest,
    RenameProjectRequest,
)
from app_config import (
    HOST,
    PORT,
    OLLAMA_URL,
    OLLAMA_MODEL,
    MAX_HISTORY_MESSAGES,
    CHAT_HISTORY_PAGE_SIZE,
    CHAT_LIST_PAGE_SIZE,
    ADMIN_USER_PAGE_SIZE,
    MAX_MESSAGE_CHARS,
    OLLAMA_CONTEXT_SIZE,
    OLLAMA_ADAPTIVE_ANALYSIS_CONTEXT,
    OLLAMA_ANALYSIS_CONTEXT_MIN,
    OLLAMA_ANALYSIS_CONTEXT_MAX,
    DIRECT_MESSAGE_CHARS,
    LARGE_CHUNK_CHARS,
    LARGE_CHUNK_OVERLAP_CHARS,
    LARGE_CHUNK_SUMMARY_TOKENS,
    CONSOLIDATION_CHUNK_CHARS,
    CONSOLIDATION_MAX_CHARS,
    LARGE_REQUEST_CONTEXT_CHARS,
    LARGE_VERIFICATION_TOKENS,
    EVIDENCE_REPAIR_SOURCE_CHARS,
    SOURCE_INVENTORY_MAX_CHARS,
    FUNCTION_ANALYSIS_CACHE_RETENTION_DAYS,
    FUNCTION_ANALYSIS_CACHE_MAX_ROWS_PER_USER,
    HISTORY_CONTEXT_MESSAGE_CHARS,
    MODEL_INPUT_CHAR_BUDGET,
    OLLAMA_MAX_OUTPUT_TOKENS,
    OLLAMA_TEMPERATURE,
    OLLAMA_REPEAT_PENALTY,
    OLLAMA_SOCKET_TIMEOUT,
    READINESS_OLLAMA_TIMEOUT_SECONDS,
    READINESS_CACHE_SECONDS,
    JOB_QUEUE_CAPACITY,
    MAX_ACTIVE_JOBS_PER_USER,
    MAX_PENDING_INPUT_CHARS_PER_USER,
    PROJECT_UPLOAD_MAX_ARCHIVE_BYTES,
    PROJECT_UPLOAD_MAX_EXPANDED_BYTES,
    PROJECT_UPLOAD_MAX_FILE_BYTES,
    PROJECT_UPLOAD_MAX_FILES,
    PROJECT_UPLOAD_MAX_COMPRESSION_RATIO,
    OLLAMA_GPT_OSS_REASONING,
    DB_PATH,
    CREATE_MIGRATION_BACKUPS,
    MIGRATION_BACKUP_DIR,
    CREATE_PERIODIC_BACKUPS,
    PERIODIC_BACKUP_DIR,
    PERIODIC_BACKUP_INTERVAL_SECONDS,
    PERIODIC_BACKUP_RETENTION_COUNT,
    SQLITE_BUSY_TIMEOUT_MS,
    PUBLIC_BASE_URL,
    TRUSTED_HOSTS_CONFIG,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USERNAME,
    SMTP_PASSWORD,
    SMTP_FROM,
    SMTP_USE_TLS,
    SMTP_USE_SSL,
    NTFY_SERVER_URL,
    NTFY_TOPIC,
    NTFY_ACCESS_TOKEN,
    NTFY_TIMEOUT_SECONDS,
    NTFY_DEDUP_SECONDS,
    NTFY_QUEUE_CAPACITY,
    VERIFICATION_MINUTES,
    REGISTRATION_RESEND_SECONDS,
    REGISTRATION_IP_WINDOW_SECONDS,
    REGISTRATION_MAX_REQUESTS_PER_IP,
    AUTH_EMAIL_RESPONSE_FLOOR_SECONDS,
    PASSWORD_RESET_MINUTES,
    PASSWORD_RESET_RESEND_SECONDS,
    PASSWORD_RESET_IP_WINDOW_SECONDS,
    PASSWORD_RESET_MAX_REQUESTS_PER_IP,
    LOGIN_ATTEMPT_COOLDOWN_SECONDS,
    LOGIN_MAX_FAILED_ATTEMPTS,
    LOGIN_LOCKOUT_SECONDS,
    SESSION_DAYS,
    MAX_SESSIONS_PER_USER,
    ADMIN_AUDIT_RETENTION_DAYS,
    TERMINAL_JOB_PAYLOAD_RETENTION_DAYS,
    SECURITY_CLEANUP_INTERVAL_SECONDS,
    SESSION_COOKIE,
    OWNER_USERNAME,
    OWNER_EMAIL,
    SYSTEM_PROMPT,
    validate_application_configuration,
)
from authentication import (
    hash_password,
    password_matches,
    password_needs_rehash,
    token_digest,
    validate_email,
    validate_password,
    validate_username,
)
from project_workspace import owned_project, store_upload_batch, validate_append, rebuild_project, entry_file_ids
from project_uploads import (
    ProjectUploadError,
    ProjectUploadTooLarge,
    UploadLimits,
    decode_relative_paths,
    ingest_folder,
    ingest_files,
    ingest_zip,
    safe_project_name,
)
from language_adapters import adapter_statuses
from project_inventory import inventory_project_database
from project_parsing import parse_project_database
from project_function_analysis import (
    StaleSymbolSource,
    analyze_project_functions,
    load_function_analysis_task,
    refresh_project_function_analysis,
)
from project_call_compatibility import check_project_call_compatibility
from project_tree_metadata import compact_source_excerpt, function_source_metadata

LOGGER = logging.getLogger("uvicorn.error")
NTFY_MESSAGE_MAX_BYTES = 3_500
NtfyNotification = tuple[str, str, str]


def bounded_ntfy_message(message: str) -> str:
    """Keep notifications readable and below ntfy's normal message-size limit."""
    encoded = message.encode("utf-8")
    if len(encoded) <= NTFY_MESSAGE_MAX_BYTES:
        return message
    marker = b"\n\n... notification shortened ...\n\n"
    tail_length = 700
    head_length = NTFY_MESSAGE_MAX_BYTES - tail_length - len(marker)
    head = encoded[:head_length].decode("utf-8", errors="ignore")
    tail = encoded[-tail_length:].decode("utf-8", errors="ignore")
    return head + marker.decode("ascii") + tail


class NtfyErrorHandler(logging.Handler):
    """Queue ERROR/CRITICAL records without doing network I/O in the logging thread."""

    def __init__(
        self,
        notification_queue: queue.Queue[NtfyNotification],
        dedup_seconds: float,
    ) -> None:
        super().__init__(level=logging.ERROR)
        self.notification_queue = notification_queue
        self.dedup_seconds = dedup_seconds
        self.recent_record_capacity = max(notification_queue.maxsize * 2, 100)
        self.recent_records: dict[bytes, float] = {}
        self.recent_records_lock = threading.Lock()
        self.dropped_notifications = 0
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            exception_identity: tuple[str, str] | None = None
            if record.exc_info and record.exc_info[1] is not None:
                exception_identity = (
                    type(record.exc_info[1]).__name__,
                    str(record.exc_info[1]),
                )
            digest = hashlib.sha256()
            for part in (
                record.name,
                str(record.levelno),
                record.getMessage(),
                repr(exception_identity),
            ):
                digest.update(part.encode("utf-8", errors="replace"))
                digest.update(b"\0")
            fingerprint = digest.digest()
            now = time.monotonic()
            if self.dedup_seconds:
                with self.recent_records_lock:
                    previous = self.recent_records.get(fingerprint)
                    if previous is not None and now - previous < self.dedup_seconds:
                        return
                    cutoff = now - self.dedup_seconds
                    self.recent_records = {
                        key: timestamp
                        for key, timestamp in self.recent_records.items()
                        if timestamp >= cutoff
                    }
                    if len(self.recent_records) >= self.recent_record_capacity:
                        oldest = min(self.recent_records, key=self.recent_records.get)
                        del self.recent_records[oldest]
                    self.recent_records[fingerprint] = now
            priority = "urgent" if record.levelno >= logging.CRITICAL else "high"
            notification = (
                f"Apokalypse Code Analysis System {record.levelname}",
                bounded_ntfy_message(self.format(record)),
                priority,
            )
            try:
                self.notification_queue.put_nowait(notification)
            except queue.Full:
                self.dropped_notifications += 1
        except Exception:
            # A logging handler must never break the code path that reported the error.
            self.dropped_notifications += 1


NTFY_NOTIFICATION_QUEUE: queue.Queue[NtfyNotification] = queue.Queue(
    maxsize=NTFY_QUEUE_CAPACITY
)
NTFY_NOTIFICATION_STOP = threading.Event()
NTFY_NOTIFICATION_THREAD: threading.Thread | None = None
NTFY_NOTIFICATION_HANDLER: NtfyErrorHandler | None = None
NTFY_NOTIFICATION_LIFECYCLE_LOCK = threading.Lock()
NTFY_FAILURE_LOG_LOCK = threading.Lock()
NTFY_LAST_FAILURE_LOGGED_AT = 0.0
JOB_CANCEL_EVENTS: dict[str, threading.Event] = {}
JOB_PAUSE_EVENTS: dict[str, threading.Event] = {}
JOB_CANCEL_LOCK = threading.Lock()
OLLAMA_JOB_QUEUE: queue.Queue[str] = queue.Queue(maxsize=JOB_QUEUE_CAPACITY)
OLLAMA_WORKER_STOP = threading.Event()
OLLAMA_WORKER_THREAD: threading.Thread | None = None
OLLAMA_WORKER_LIFECYCLE_LOCK = threading.Lock()
MAINTENANCE_STOP = threading.Event()
MAINTENANCE_THREAD: threading.Thread | None = None
MAINTENANCE_LIFECYCLE_LOCK = threading.Lock()
DATABASE_BACKUP_LOCK = threading.Lock()
DATABASE_MAINTENANCE_LOCK = threading.Lock()
READINESS_CACHE_LOCK = threading.Lock()
READINESS_CACHE: tuple[float, dict[str, object]] | None = None
PROCESS_STARTED_AT = int(time.time())
PROCESS_STARTED_MONOTONIC = time.monotonic()
PROJECT_UPLOAD_LIMITS = UploadLimits(
    max_archive_bytes=PROJECT_UPLOAD_MAX_ARCHIVE_BYTES,
    max_expanded_bytes=PROJECT_UPLOAD_MAX_EXPANDED_BYTES,
    max_file_bytes=PROJECT_UPLOAD_MAX_FILE_BYTES,
    max_files=PROJECT_UPLOAD_MAX_FILES,
    max_compression_ratio=PROJECT_UPLOAD_MAX_COMPRESSION_RATIO,
)
PROJECT_UPLOAD_MAX_REQUEST_BYTES = (
    max(PROJECT_UPLOAD_MAX_ARCHIVE_BYTES, PROJECT_UPLOAD_MAX_EXPANDED_BYTES)
    + PROJECT_UPLOAD_MAX_FILES * 1_024
    + 2_000_000
)
FUNCTION_TREE_DESCRIPTION_MAX_CHARS = 400


from analysis_quality import project_review_quality, response_quality
from analysis_engine import (
    AnalysisCancelled,
    EvidenceFinding,
    EvidenceReview,
    VerifiedFeature,
    analyze_large_input,
    ask_ollama,
    assignment_names,
    build_evidence_repair_material,
    build_verified_source_inventory,
    clean_evidence_excerpt,
    compact_source_text,
    deterministic_source_review,
    dotted_ast_name,
    evidence_options_from_source_region,
    evidence_review_schema,
    extract_python_source,
    extract_stored_analysis_notes,
    final_response_json_to_markdown,
    finding_contradicts_inventory,
    generate_reply,
    interrupt_ollama_response,
    markdown_from_structure,
    match_inventory_identifier,
    one_line,
    readable_json_key,
    related_inventory_identifiers,
    render_evidence_review,
    response_is_degenerate,
    reviewable_source_identifiers,
    source_window_for_identifier,
    split_large_text,
    trim_history,
    trim_source_region,
    validate_evidence_review,
    verify_analysis_notes,
)
from database import connect_database
from changelog import (
    build_changelog_entries,
    render_changelog_html,
    render_changelog_markdown,
)
from migrations import apply_migrations, pending_migration_versions


def deliver_ntfy_notification(notification: NtfyNotification) -> None:
    """Publish one queued notification using ntfy's HTTP API."""
    title, message, priority = notification
    topic = urllib.parse.quote(NTFY_TOPIC, safe="")
    request = urllib.request.Request(
        f"{NTFY_SERVER_URL}/{topic}",
        data=message.encode("utf-8"),
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Title": title,
            "Priority": priority,
            "Tags": "warning,computer",
        },
        method="POST",
    )
    if NTFY_ACCESS_TOKEN:
        request.add_header("Authorization", f"Bearer {NTFY_ACCESS_TOKEN}")
    with urllib.request.urlopen(request, timeout=NTFY_TIMEOUT_SECONDS) as response:
        status = getattr(response, "status", 200)
        response.read(1)
        if status >= 400:
            raise OSError(f"ntfy returned HTTP {status}")


def log_ntfy_delivery_failure(exc: Exception) -> None:
    """Report delivery trouble sparingly without recursively creating notifications."""
    global NTFY_LAST_FAILURE_LOGGED_AT
    now = time.monotonic()
    with NTFY_FAILURE_LOG_LOCK:
        if now - NTFY_LAST_FAILURE_LOGGED_AT < 300:
            return
        NTFY_LAST_FAILURE_LOGGED_AT = now
    LOGGER.warning("NTFY error notification delivery failed: %s", exc)


def ntfy_notification_worker() -> None:
    while not NTFY_NOTIFICATION_STOP.is_set():
        try:
            notification = NTFY_NOTIFICATION_QUEUE.get(timeout=0.25)
        except queue.Empty:
            continue
        try:
            deliver_ntfy_notification(notification)
        except Exception as exc:
            log_ntfy_delivery_failure(exc)
        finally:
            NTFY_NOTIFICATION_QUEUE.task_done()


def start_ntfy_error_notifier() -> None:
    """Attach the error handler and ensure its single delivery worker is running."""
    global NTFY_NOTIFICATION_HANDLER, NTFY_NOTIFICATION_THREAD
    if not NTFY_TOPIC:
        return
    with NTFY_NOTIFICATION_LIFECYCLE_LOCK:
        if NTFY_NOTIFICATION_HANDLER is None:
            NTFY_NOTIFICATION_HANDLER = NtfyErrorHandler(
                NTFY_NOTIFICATION_QUEUE,
                NTFY_DEDUP_SECONDS,
            )
        if NTFY_NOTIFICATION_HANDLER not in LOGGER.handlers:
            LOGGER.addHandler(NTFY_NOTIFICATION_HANDLER)
        if NTFY_NOTIFICATION_THREAD is not None and NTFY_NOTIFICATION_THREAD.is_alive():
            return
        NTFY_NOTIFICATION_STOP.clear()
        NTFY_NOTIFICATION_THREAD = threading.Thread(
            target=ntfy_notification_worker,
            name="ntfy-error-notifier",
            daemon=True,
        )
        NTFY_NOTIFICATION_THREAD.start()


def stop_ntfy_error_notifier() -> None:
    """Detach the handler, briefly flush queued errors, and stop its worker."""
    global NTFY_NOTIFICATION_HANDLER, NTFY_NOTIFICATION_THREAD
    with NTFY_NOTIFICATION_LIFECYCLE_LOCK:
        handler = NTFY_NOTIFICATION_HANDLER
        thread = NTFY_NOTIFICATION_THREAD
        if handler is not None and handler in LOGGER.handlers:
            LOGGER.removeHandler(handler)
        NTFY_NOTIFICATION_HANDLER = None
    if thread is None:
        return

    flush_deadline = time.monotonic() + min(NTFY_TIMEOUT_SECONDS + 0.5, 6.0)
    while (
        NTFY_NOTIFICATION_QUEUE.unfinished_tasks
        and thread.is_alive()
        and time.monotonic() < flush_deadline
    ):
        time.sleep(0.05)
    NTFY_NOTIFICATION_STOP.set()
    thread.join(timeout=0.75)
    with NTFY_NOTIFICATION_LIFECYCLE_LOCK:
        if NTFY_NOTIFICATION_THREAD is thread and not thread.is_alive():
            NTFY_NOTIFICATION_THREAD = None


@contextmanager
def connect_db():
    with connect_database(DB_PATH, SQLITE_BUSY_TIMEOUT_MS) as connection:
        yield connection


def prune_expired_security_records(
    db: sqlite3.Connection,
    *,
    now: int | None = None,
) -> dict[str, int]:
    """Remove expired credentials and stale throttles without touching active records."""
    current_time = int(time.time()) if now is None else now
    stale_login_cutoff = current_time - 24 * 60 * 60
    statements = {
        "login_sessions": ("DELETE FROM login_sessions WHERE expires_at <= ?", current_time),
        "registration_tokens": (
            "DELETE FROM registration_tokens WHERE expires_at <= ?",
            current_time,
        ),
        "password_reset_tokens": (
            "DELETE FROM password_reset_tokens WHERE expires_at <= ?",
            current_time,
        ),
        "login_throttles": (
            "DELETE FROM login_throttles WHERE updated_at < ? AND locked_until <= ?",
            stale_login_cutoff,
            current_time,
        ),
        "auth_request_rate_limits": (
            "DELETE FROM auth_request_rate_limits WHERE updated_at < ?",
            current_time
            - 2
            * max(REGISTRATION_IP_WINDOW_SECONDS, PASSWORD_RESET_IP_WINDOW_SECONDS),
        ),
        "admin_audit_events": (
            "DELETE FROM admin_audit_events WHERE created_at < ?",
            current_time - ADMIN_AUDIT_RETENTION_DAYS * 24 * 60 * 60,
        ),
    }
    deleted: dict[str, int] = {}
    for table, (statement, *parameters) in statements.items():
        deleted[table] = db.execute(statement, parameters).rowcount
    return deleted


def run_security_record_cleanup() -> dict[str, int]:
    with connect_db() as db:
        deleted = prune_expired_security_records(db)
    total = sum(deleted.values())
    if total:
        LOGGER.info(
            "Expired security-record cleanup removed %s row(s): %s",
            total,
            ", ".join(f"{table}={count}" for table, count in deleted.items() if count),
        )
    return deleted


def compact_terminal_chat_job_payloads(
    db: sqlite3.Connection,
    *,
    now: int | None = None,
) -> int:
    """Clear old duplicate job payloads while preserving terminal status and message links."""
    current_time = int(time.time()) if now is None else now
    cutoff = current_time - TERMINAL_JOB_PAYLOAD_RETENTION_DAYS * 24 * 60 * 60
    return db.execute(
        """
        UPDATE chat_jobs
        SET message = CASE WHEN EXISTS (
                SELECT 1 FROM messages AS stored_prompt
                WHERE stored_prompt.id = chat_jobs.user_message_id
                  AND stored_prompt.session_id =
                      ('user-' || chat_jobs.user_id || ':' || chat_jobs.chat_id)
                  AND stored_prompt.role = 'user'
            ) THEN NULL ELSE message END,
            reply = CASE WHEN EXISTS (
                SELECT 1 FROM messages AS stored_reply
                WHERE stored_reply.id = chat_jobs.reply_message_id
                  AND stored_reply.session_id =
                      ('user-' || chat_jobs.user_id || ':' || chat_jobs.chat_id)
                  AND stored_reply.role = 'assistant'
            ) THEN NULL ELSE reply END,
            progress_log = '[]'
        WHERE status IN ('completed', 'failed')
          AND updated_at < datetime(?, 'unixepoch')
          AND (
              progress_log != '[]'
              OR (message IS NOT NULL AND EXISTS (
                  SELECT 1 FROM messages AS stored_prompt
                  WHERE stored_prompt.id = chat_jobs.user_message_id
                    AND stored_prompt.session_id =
                        ('user-' || chat_jobs.user_id || ':' || chat_jobs.chat_id)
                    AND stored_prompt.role = 'user'
              ))
              OR (reply IS NOT NULL AND EXISTS (
                  SELECT 1 FROM messages AS stored_reply
                  WHERE stored_reply.id = chat_jobs.reply_message_id
                    AND stored_reply.session_id =
                        ('user-' || chat_jobs.user_id || ':' || chat_jobs.chat_id)
                    AND stored_reply.role = 'assistant'
              ))
          )
        """,
        (cutoff,),
    ).rowcount


def prune_function_analysis_cache(
    db: sqlite3.Connection,
    *,
    now: int | None = None,
) -> int:
    """Bound shared function-contract cache age and per-user row growth."""
    current_time = int(time.time()) if now is None else now
    cutoff = current_time - FUNCTION_ANALYSIS_CACHE_RETENTION_DAYS * 24 * 60 * 60
    expired = db.execute(
        """
        DELETE FROM function_analysis_cache
        WHERE COALESCE(last_used_at, updated_at, created_at) < datetime(?, 'unixepoch')
        """,
        (cutoff,),
    ).rowcount
    excess = db.execute(
        """
        DELETE FROM function_analysis_cache
        WHERE rowid IN (
            SELECT rowid
            FROM (
                SELECT rowid,
                       ROW_NUMBER() OVER (
                           PARTITION BY user_id
                           ORDER BY COALESCE(last_used_at, updated_at, created_at) DESC,
                                    rowid DESC
                       ) AS row_number
                FROM function_analysis_cache
            ) AS ranked
            WHERE row_number > ?
        )
        """,
        (FUNCTION_ANALYSIS_CACHE_MAX_ROWS_PER_USER,),
    ).rowcount
    return int(expired or 0) + int(excess or 0)


def delete_project_function_analysis_cache(
    db: sqlite3.Connection,
    *,
    project_id: str,
    user_id: int,
) -> tuple[int, int]:
    """Delete reusable cache rows matching the current indexed project functions."""
    symbols = db.execute(
        """
        SELECT id FROM project_symbols
        WHERE project_id = ? AND symbol_kind IN ('function', 'method')
        ORDER BY id
        """,
        (project_id,),
    ).fetchall()
    cache_keys: set[tuple[str, str]] = set()
    stale_key_count = 0
    for symbol in symbols:
        try:
            task = load_function_analysis_task(db, int(symbol["id"]))
            inferred_task = load_function_analysis_task(db, int(symbol["id"]), include_inferred=True)
        except (LookupError, StaleSymbolSource, ValueError):
            stale_key_count += 1
            continue
        for cache_sha256 in {
            task.function_sha256,
            getattr(task, "semantic_function_sha256", "") or "",
            getattr(task, "cache_function_sha256", "") or "",
            getattr(task, "semantic_cache_function_sha256", "") or "",
            inferred_task.cache_function_sha256,
            inferred_task.semantic_cache_function_sha256,
        }:
            if cache_sha256:
                cache_keys.add((task.language, cache_sha256))
    cache_deleted = 0
    for language, function_sha256 in sorted(cache_keys):
        cache_deleted += int(
            db.execute(
                """
                DELETE FROM function_analysis_cache
                WHERE user_id = ? AND language = ? AND function_sha256 = ?
                """,
                (user_id, language, function_sha256),
            ).rowcount
            or 0
        )
    return cache_deleted, stale_key_count


def run_database_maintenance() -> tuple[dict[str, int], int]:
    with DATABASE_MAINTENANCE_LOCK, connect_db() as db:
        deleted = prune_expired_security_records(db)
        compacted_jobs = compact_terminal_chat_job_payloads(db)
        deleted["function_analysis_cache"] = prune_function_analysis_cache(db)
    total = sum(deleted.values())
    if total:
        LOGGER.info(
            "Expired security-record cleanup removed %s row(s): %s",
            total,
            ", ".join(f"{table}={count}" for table, count in deleted.items() if count),
        )
    if compacted_jobs:
        LOGGER.info("Compacted duplicate payloads from %s terminal chat job(s)", compacted_jobs)
    if deleted.get("function_analysis_cache"):
        LOGGER.info(
            "Pruned %s function-analysis cache row(s)",
            deleted["function_analysis_cache"],
        )
    return deleted, compacted_jobs


def create_verified_sqlite_backup(
    source: sqlite3.Connection,
    backup_path: Path,
) -> Path:
    """Atomically publish an online SQLite backup only after an integrity check."""
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = backup_path.with_name(
        f".{backup_path.name}.{uuid.uuid4().hex}.tmp"
    )
    destination = sqlite3.connect(temporary_path)
    try:
        source.backup(destination)
        result = destination.execute("PRAGMA quick_check").fetchone()[0]
        if str(result).casefold() != "ok":
            raise RuntimeError(f"SQLite rejected the database backup: {result}")
        destination.close()
        os.replace(temporary_path, backup_path)
    except Exception:
        destination.close()
        temporary_path.unlink(missing_ok=True)
        raise
    return backup_path


def safe_database_stem() -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", DB_PATH.stem) or "database"


def create_pre_migration_backup(
    source: sqlite3.Connection,
    pending_versions: list[int],
) -> Path:
    """Create and validate a point-in-time SQLite copy before schema changes."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target_version = max(pending_versions)
    backup_path = MIGRATION_BACKUP_DIR / (
        f"{safe_database_stem()}-pre-v{target_version}-{timestamp}-{uuid.uuid4().hex[:8]}.db"
    )
    create_verified_sqlite_backup(source, backup_path)
    LOGGER.info(
        "Created pre-migration database backup at %s before version(s) %s",
        backup_path,
        ", ".join(str(version) for version in pending_versions),
    )
    return backup_path


def periodic_backup_paths() -> list[Path]:
    if not PERIODIC_BACKUP_DIR.is_dir():
        return []
    pattern = f"{safe_database_stem()}-periodic-*.db"
    dated_paths: list[tuple[int, str, Path]] = []
    for path in PERIODIC_BACKUP_DIR.glob(pattern):
        try:
            dated_paths.append((path.stat().st_mtime_ns, path.name, path))
        except OSError:
            continue
    dated_paths.sort(reverse=True)
    return [path for _modified, _name, path in dated_paths]


def prune_periodic_backups() -> list[Path]:
    """Retain only the configured number of routine backups."""
    removed: list[Path] = []
    for backup_path in periodic_backup_paths()[PERIODIC_BACKUP_RETENTION_COUNT:]:
        try:
            backup_path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            LOGGER.warning("Could not prune periodic backup %s: %s", backup_path, exc)
            continue
        removed.append(backup_path)
    return removed


def seconds_until_periodic_backup(*, now: float | None = None) -> float:
    backups = periodic_backup_paths()
    if not backups:
        return 0
    current_time = time.time() if now is None else now
    age = max(0, current_time - backups[0].stat().st_mtime)
    return max(0, PERIODIC_BACKUP_INTERVAL_SECONDS - age)


def create_periodic_database_backup() -> Path:
    """Create, validate, publish, then rotate a routine online backup."""
    with DATABASE_BACKUP_LOCK:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = PERIODIC_BACKUP_DIR / (
            f"{safe_database_stem()}-periodic-{timestamp}-{uuid.uuid4().hex[:8]}.db"
        )
        with connect_db() as source:
            create_verified_sqlite_backup(source, backup_path)
        removed = prune_periodic_backups()
        LOGGER.info(
            "Created verified periodic database backup at %s; pruned %s old copy/copies",
            backup_path,
            len(removed),
        )
        return backup_path


def initialise_db() -> None:
    interrupted_job_ids: list[str] = []
    database_existed = DB_PATH.is_file() and DB_PATH.stat().st_size > 0
    with connect_db() as db:
        pending_versions = pending_migration_versions(db)
        if database_existed and pending_versions and CREATE_MIGRATION_BACKUPS:
            create_pre_migration_backup(db, pending_versions)
        journal_mode = db.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            LOGGER.warning("SQLite did not enable WAL mode; journal mode is %s", journal_mode)
        apply_migrations(db, target_version=1)
        db.execute(
            """
            UPDATE users SET is_admin = 1, is_owner = 1, is_banned = 0
            WHERE username = ? COLLATE NOCASE AND email = ? COLLATE NOCASE
            """,
            (OWNER_USERNAME, OWNER_EMAIL),
        )
        orphaned_job_count = db.execute(
            """
            SELECT COUNT(*) FROM chat_jobs AS job
            WHERE NOT EXISTS (
                SELECT 1 FROM chat_histories AS history
                WHERE history.id = job.chat_id AND history.user_id = job.user_id
            )
            """
        ).fetchone()[0]
        if orphaned_job_count:
            db.execute(
                """
                DELETE FROM chat_jobs
                WHERE NOT EXISTS (
                    SELECT 1 FROM chat_histories
                    WHERE chat_histories.id = chat_jobs.chat_id
                      AND chat_histories.user_id = chat_jobs.user_id
                )
                """
            )
            LOGGER.warning(
                "Removed %s orphaned chat job record(s)", orphaned_job_count
            )
        interrupted_job_ids = [
            row["id"]
            for row in db.execute(
                """
                SELECT id FROM chat_jobs
                WHERE status IN ('queued', 'processing') AND job_kind = 'chat'
                  AND user_message_id IS NOT NULL
                """
            ).fetchall()
        ]
        db.execute(
            """
            UPDATE chat_jobs
            SET status = 'failed', error = 'The server restarted before analysis completed.',
                message = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE status IN ('queued', 'processing')
            """
        )
        apply_migrations(db)
        db.execute(
            """
            UPDATE project_symbols
            SET analysis_status = 'pending', analysis_error = NULL
            WHERE analysis_status = 'processing'
            """
        )
        db.execute(
            """
            UPDATE projects
            SET function_analysis_status = CASE
                    WHEN function_analysis_completed_count > 0 THEN 'partial'
                    ELSE 'pending'
                END,
                function_analysis_error = 'The server restarted before analysis completed.',
                function_analysis_updated_at = CURRENT_TIMESTAMP
            WHERE function_analysis_status = 'running'
            """
        )
        pending_project_ids = [
            str(row["id"])
            for row in db.execute(
                "SELECT id FROM projects WHERE inventory_status = 'pending' ORDER BY created_at, id"
            ).fetchall()
        ]
        inventoried_projects = 0
        for project_id in pending_project_ids:
            try:
                inventory_project_database(db, project_id)
            except (OSError, ValueError, sqlite3.Error, UnicodeError) as exc:
                db.execute(
                    """
                    UPDATE projects
                    SET inventory_status = 'failed', inventory_error = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (f"{type(exc).__name__}: {exc}"[:1_000], project_id),
                )
                LOGGER.warning("Could not inventory uploaded project %s: %s", project_id, exc)
            else:
                inventoried_projects += 1
        if inventoried_projects:
            LOGGER.info(
                "Built deterministic file inventories for %s uploaded project(s)",
                inventoried_projects,
            )
        pending_parser_project_ids = [
            str(row["id"])
            for row in db.execute(
                """
                SELECT id FROM projects
                WHERE inventory_status = 'completed'
                  AND (parser_status = 'pending' OR structure_status = 'pending')
                ORDER BY created_at, id
                """
            ).fetchall()
        ]
        parsed_projects = 0
        for project_id in pending_parser_project_ids:
            try:
                parse_project_database(db, project_id)
            except (OSError, ValueError, sqlite3.Error, UnicodeError) as exc:
                db.execute(
                    """
                    UPDATE projects
                    SET parser_status = 'failed', parser_error = ?,
                        structure_status = 'failed', structure_error = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        f"{type(exc).__name__}: {exc}"[:1_000],
                        f"{type(exc).__name__}: {exc}"[:1_000],
                        project_id,
                    ),
                )
                LOGGER.warning("Could not parse uploaded project %s: %s", project_id, exc)
            else:
                parsed_projects += 1
        if parsed_projects:
            LOGGER.info(
                "Ran Tree-sitter adapters for %s uploaded project(s)",
                parsed_projects,
            )
        db.execute(
            """
            UPDATE users
            SET is_admin = 1, is_owner = 1, is_banned = 0,
                ban_reason = NULL, banned_until = NULL
            WHERE username = ? COLLATE NOCASE AND email = ? COLLATE NOCASE
            """,
            (OWNER_USERNAME, OWNER_EMAIL),
        )
        deleted_security_records = prune_expired_security_records(db)
        if sum(deleted_security_records.values()):
            LOGGER.info(
                "Startup security-record cleanup removed %s expired row(s)",
                sum(deleted_security_records.values()),
            )
        compacted_jobs = compact_terminal_chat_job_payloads(db)
        if compacted_jobs:
            LOGGER.info(
                "Startup database maintenance compacted %s terminal chat job(s)",
                compacted_jobs,
            )
        pruned_cache_rows = prune_function_analysis_cache(db)
        if pruned_cache_rows:
            LOGGER.info(
                "Startup function-analysis cache cleanup removed %s row(s)",
                pruned_cache_rows,
            )
        existing_sessions = db.execute(
            "SELECT session_id, MIN(created_at) AS created_at, MAX(created_at) AS updated_at "
            "FROM messages GROUP BY session_id"
        ).fetchall()
        for session in existing_sessions:
            match = re.fullmatch(r"user-(\d+):(.+)", session["session_id"])
            if not match:
                continue
            user_id, chat_id = int(match.group(1)), match.group(2)
            first_message = db.execute(
                """
                SELECT content FROM messages
                WHERE session_id = ? AND role = 'user' ORDER BY id LIMIT 1
                """,
                (session["session_id"],),
            ).fetchone()
            title = title_from_message(first_message["content"] if first_message else "")
            db.execute(
                """
                INSERT OR IGNORE INTO chat_histories(id, user_id, title, created_at, updated_at)
                SELECT ?, id, ?, ?, ? FROM users WHERE id = ?
                """,
                (
                    chat_id,
                    title,
                    session["created_at"],
                    session["updated_at"],
                    user_id,
                ),
            )
        quick_check = db.execute("PRAGMA quick_check").fetchone()[0]
        if str(quick_check).casefold() != "ok":
            raise RuntimeError(f"SQLite integrity check failed after migration: {quick_check}")
    for interrupted_job_id in interrupted_job_ids:
        with connect_db() as db:
            record_chat_job_progress(
                db, interrupted_job_id, "failed", update_stage=False
            )
        persist_failed_job_message(
            interrupted_job_id,
            "Error: The server restarted before this request completed.",
        )


def history_for(
    session_id: str, exclude_message_id: int | None = None
) -> list[dict[str, str]]:
    with connect_db() as db:
        rows = db.execute(
            """
            SELECT role, content FROM (
                SELECT id, role, COALESCE(context_content, content) AS content FROM messages
                WHERE session_id = ? AND (? IS NULL OR id != ?)
                ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
            """,
            (session_id, exclude_message_id, exclude_message_id, MAX_HISTORY_MESSAGES),
        ).fetchall()
    messages = []
    for row in rows:
        content = row["content"]
        if row["role"] == "assistant" and response_is_degenerate(content):
            content = "[Previous repetitive model response omitted from active context.]"
        if len(content) > HISTORY_CONTEXT_MESSAGE_CHARS:
            content = (
                content[:HISTORY_CONTEXT_MESSAGE_CHARS]
                + "\n\n[Earlier oversized message truncated for the active model context.]"
            )
        messages.append({"role": row["role"], "content": content})
    return messages


def title_from_message(message: str) -> str:
    first_line = next((line.strip() for line in message.splitlines() if line.strip()), "")
    title = re.sub(r"\s+", " ", first_line)[:40].rstrip()
    return title or "New chat"


def save_message(
    session_id: str,
    role: Literal["user", "assistant"],
    content: str,
    context_content: str | None = None,
) -> int:
    with connect_db() as db:
        cursor = db.execute(
            """
            INSERT INTO messages(session_id, role, content, context_content)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, role, content, context_content),
        )
        return int(cursor.lastrowid)


DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


def login_account_scope(user_id: int | None, identity: str) -> str:
    value = f"user:{user_id}" if user_id is not None else f"identity:{identity.casefold()}"
    return token_digest(value)


def login_ip_scope(request: Request) -> str:
    address = request.client.host if request.client else "unknown"
    return token_digest(f"ip:{address}")


def reserve_auth_request_ip_attempt(
    request: Request,
    *,
    action: str,
    window_seconds: int,
    maximum_attempts: int,
) -> bool:
    """Persist an IP allowance for an email-producing authentication action."""
    now = int(time.time())
    address = request.client.host if request.client else "unknown"
    scope_hash = token_digest(f"{action}-ip:{address}")
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "DELETE FROM auth_request_rate_limits WHERE updated_at < ?",
            (
                now
                - 2
                * max(
                    REGISTRATION_IP_WINDOW_SECONDS,
                    PASSWORD_RESET_IP_WINDOW_SECONDS,
                ),
            ),
        )
        row = db.execute(
            """
            SELECT window_started_at, attempt_count
            FROM auth_request_rate_limits WHERE scope_hash = ? AND action = ?
            """,
            (scope_hash, action),
        ).fetchone()
        if row is None or now - row["window_started_at"] >= window_seconds:
            window_started_at = now
            attempt_count = 1
        else:
            window_started_at = int(row["window_started_at"])
            attempt_count = int(row["attempt_count"]) + 1
        db.execute(
            """
            INSERT INTO auth_request_rate_limits(
                scope_hash, action, window_started_at, attempt_count, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scope_hash) DO UPDATE SET
                action = excluded.action,
                window_started_at = excluded.window_started_at,
                attempt_count = excluded.attempt_count,
                updated_at = excluded.updated_at
            """,
            (scope_hash, action, window_started_at, attempt_count, now),
        )
    return attempt_count <= maximum_attempts


def reserve_registration_ip_attempt(request: Request) -> bool:
    return reserve_auth_request_ip_attempt(
        request,
        action="registration",
        window_seconds=REGISTRATION_IP_WINDOW_SECONDS,
        maximum_attempts=REGISTRATION_MAX_REQUESTS_PER_IP,
    )


def reserve_password_reset_ip_attempt(request: Request) -> bool:
    return reserve_auth_request_ip_attempt(
        request,
        action="password_reset",
        window_seconds=PASSWORD_RESET_IP_WINDOW_SECONDS,
        maximum_attempts=PASSWORD_RESET_MAX_REQUESTS_PER_IP,
    )


def generic_auth_email_response(
    started_at: float,
    message: str,
) -> dict[str, str]:
    """Apply the same minimum response time to every generic email-action outcome."""
    remaining = AUTH_EMAIL_RESPONSE_FLOOR_SECONDS - (time.monotonic() - started_at)
    if remaining > 0:
        time.sleep(remaining)
    return {"message": message}


def reserve_login_attempt(account_scope: str, ip_scope: str) -> tuple[int, bool]:
    now = int(time.time())
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "DELETE FROM login_throttles WHERE updated_at < ? AND locked_until <= ?",
            (now - 24 * 60 * 60, now),
        )
        account = db.execute(
            """
            SELECT failed_attempts, last_attempt_at, locked_until
            FROM login_throttles WHERE scope_hash = ?
            """,
            (account_scope,),
        ).fetchone()
        ip = db.execute(
            "SELECT last_attempt_at FROM login_throttles WHERE scope_hash = ?",
            (ip_scope,),
        ).fetchone()
        if account and account["locked_until"] > now:
            return account["locked_until"] - now, True
        if account and now - account["last_attempt_at"] >= LOGIN_LOCKOUT_SECONDS:
            db.execute(
                """
                UPDATE login_throttles
                SET failed_attempts = 0, locked_until = 0, updated_at = ?
                WHERE scope_hash = ?
                """,
                (now, account_scope),
            )
        waits = []
        if account:
            waits.append(LOGIN_ATTEMPT_COOLDOWN_SECONDS - (now - account["last_attempt_at"]))
        if ip:
            waits.append(LOGIN_ATTEMPT_COOLDOWN_SECONDS - (now - ip["last_attempt_at"]))
        retry_after = max(waits, default=0)
        if retry_after > 0:
            return retry_after, False
        for scope in {account_scope, ip_scope}:
            db.execute(
                """
                INSERT INTO login_throttles(
                    scope_hash, failed_attempts, last_attempt_at, locked_until, updated_at
                ) VALUES (?, 0, ?, 0, ?)
                ON CONFLICT(scope_hash) DO UPDATE SET
                    last_attempt_at = excluded.last_attempt_at,
                    updated_at = excluded.updated_at
                """,
                (scope, now, now),
            )
    return 0, False


def record_failed_login(account_scope: str) -> tuple[int, bool]:
    now = int(time.time())
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT failed_attempts FROM login_throttles WHERE scope_hash = ?",
            (account_scope,),
        ).fetchone()
        failures = (row["failed_attempts"] if row else 0) + 1
        locked = failures >= LOGIN_MAX_FAILED_ATTEMPTS
        locked_until = now + LOGIN_LOCKOUT_SECONDS if locked else 0
        db.execute(
            """
            INSERT INTO login_throttles(
                scope_hash, failed_attempts, last_attempt_at, locked_until, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scope_hash) DO UPDATE SET
                failed_attempts = excluded.failed_attempts,
                last_attempt_at = excluded.last_attempt_at,
                locked_until = excluded.locked_until,
                updated_at = excluded.updated_at
            """,
            (account_scope, failures, now, locked_until, now),
        )
    return (LOGIN_LOCKOUT_SECONDS if locked else LOGIN_ATTEMPT_COOLDOWN_SECONDS), locked


def clear_login_failures(account_scope: str) -> None:
    with connect_db() as db:
        db.execute("DELETE FROM login_throttles WHERE scope_hash = ?", (account_scope,))


def current_user(request: Request) -> sqlite3.Row | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    now = int(time.time())
    with connect_db() as db:
        db.execute("DELETE FROM login_sessions WHERE expires_at <= ?", (now,))
        statement = """
            SELECT users.id, users.email, users.username,
                   users.is_admin, users.is_owner, users.is_banned,
                   users.ban_reason, users.banned_until
            FROM login_sessions JOIN users ON users.id = login_sessions.user_id
            WHERE login_sessions.token_hash = ? AND login_sessions.expires_at > ?
            """
        parameters = (token_digest(token), now)
        user = db.execute(statement, parameters).fetchone()
        if (
            user is not None
            and user["is_banned"]
            and user["banned_until"] is not None
            and int(user["banned_until"]) <= now
        ):
            expire_user_bans(db, now=now, user_id=int(user["id"]))
            user = db.execute(statement, parameters).fetchone()
        return user


def require_user(request: Request) -> sqlite3.Row:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Please log in")
    if user["is_banned"]:
        raise HTTPException(status_code=403, detail=banned_account_detail(user))
    return user


def require_admin(request: Request) -> sqlite3.Row:
    user = require_user(request)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Administrator access required")
    return user


def expire_user_bans(
    db: sqlite3.Connection,
    *,
    now: int | None = None,
    user_id: int | None = None,
) -> int:
    """Clear temporary bans once their configured expiry has passed."""
    current_time = int(time.time()) if now is None else now
    parameters: list[object] = [current_time]
    user_clause = ""
    if user_id is not None:
        user_clause = " AND id = ?"
        parameters.append(user_id)
    return db.execute(
        """
        UPDATE users
        SET is_banned = 0, ban_reason = NULL, banned_until = NULL
        WHERE is_banned = 1 AND banned_until IS NOT NULL AND banned_until <= ?
        """
        + user_clause,
        parameters,
    ).rowcount


def banned_account_detail(user: sqlite3.Row | dict[str, object]) -> str:
    detail = "This account is banned"
    banned_until = user["banned_until"]
    if banned_until:
        expiry = datetime.fromtimestamp(int(banned_until), timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        detail += f" until {expiry}"
    reason = str(user["ban_reason"] or "").strip()
    if reason:
        detail += f": {reason}"
    return detail


def registration_is_enabled(db: sqlite3.Connection | None = None) -> bool:
    if db is None:
        with connect_db() as connection:
            return registration_is_enabled(connection)
    row = db.execute(
        "SELECT value FROM application_settings WHERE key = 'registration_enabled'"
    ).fetchone()
    return row is None or row["value"] == "1"


def ai_work_is_enabled(db: sqlite3.Connection | None = None) -> bool:
    if db is None:
        with connect_db() as connection:
            return ai_work_is_enabled(connection)
    row = db.execute(
        "SELECT value FROM application_settings WHERE key = 'ai_work_enabled'"
    ).fetchone()
    return row is None or row["value"] == "1"


def require_ai_work_enabled(db: sqlite3.Connection | None = None) -> None:
    if not ai_work_is_enabled(db):
        raise HTTPException(
            status_code=503,
            detail="AI requests are temporarily paused by the site owner",
            headers={"Retry-After": "60"},
        )


def active_announcement(
    db: sqlite3.Connection | None = None,
    *,
    now: int | None = None,
) -> dict[str, object] | None:
    if db is None:
        with connect_db() as connection:
            return active_announcement(connection, now=now)
    row = db.execute(
        "SELECT value, updated_at FROM application_settings WHERE key = 'announcement'"
    ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row["value"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    announcement_id = value.get("id")
    message = value.get("message")
    level = value.get("level")
    expires_at = value.get("expires_at")
    if (
        not isinstance(announcement_id, str)
        or not isinstance(message, str)
        or not message.strip()
        or level not in {"info", "warning", "critical"}
        or (expires_at is not None and not isinstance(expires_at, int))
    ):
        return None
    current_time = int(time.time()) if now is None else now
    if expires_at is not None and expires_at <= current_time:
        return None
    return {
        "id": announcement_id,
        "message": message,
        "level": level,
        "expires_at": expires_at,
        "published_at": int(row["updated_at"]),
    }


def set_login_cookie(response: Response, request: Request, user_id: int) -> None:
    token = secrets.token_urlsafe(32)
    max_age = SESSION_DAYS * 24 * 60 * 60
    now = int(time.time())
    previous_token = request.cookies.get(SESSION_COOKIE)
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM login_sessions WHERE expires_at <= ?", (now,))
        if previous_token:
            db.execute(
                "DELETE FROM login_sessions WHERE token_hash = ?",
                (token_digest(previous_token),),
            )
        db.execute(
            "INSERT INTO login_sessions(user_id, token_hash, expires_at) VALUES (?, ?, ?)",
            (user_id, token_digest(token), now + max_age),
        )
        db.execute(
            """
            DELETE FROM login_sessions
            WHERE user_id = ? AND id NOT IN (
                SELECT id FROM login_sessions
                WHERE user_id = ? ORDER BY id DESC LIMIT ?
            )
            """,
            (user_id, user_id, MAX_SESSIONS_PER_USER),
        )
    forwarded_scheme = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=request.url.scheme == "https" or forwarded_scheme == "https",
        samesite="lax",
        path="/",
    )


def verification_base_url() -> str:
    public_origin = normalise_origin(PUBLIC_BASE_URL) if PUBLIC_BASE_URL else None
    if public_origin is None:
        raise RuntimeError(
            "Email links require PUBLIC_BASE_URL to be a valid HTTP(S) origin"
        )
    return public_origin


def send_verification_email(recipient: str, verification_url: str) -> None:
    if not SMTP_HOST or not SMTP_FROM:
        raise RuntimeError("Email is not configured: set SMTP_HOST and SMTP_FROM")
    message = EmailMessage()
    message["Subject"] = "Verify your Coding AI account"
    message["From"] = SMTP_FROM
    message["To"] = recipient
    message.set_content(
        "Use this one-time link to finish creating your account. "
        f"It expires in {VERIFICATION_MINUTES} minutes:\n\n{verification_url}\n\n"
        "If you did not request this, you can ignore this email."
    )
    smtp_class = smtplib.SMTP_SSL if SMTP_USE_SSL else smtplib.SMTP
    with smtp_class(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        if SMTP_USE_TLS and not SMTP_USE_SSL:
            smtp.starttls()
        if SMTP_USERNAME:
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
        smtp.send_message(message)


def deliver_verification_email(recipient: str, verification_url: str, digest: str) -> None:
    try:
        send_verification_email(recipient, verification_url)
    except (OSError, RuntimeError, smtplib.SMTPException) as exc:
        LOGGER.error(
            "Could not send registration verification email",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        with connect_db() as db:
            db.execute("DELETE FROM registration_tokens WHERE token_hash = ?", (digest,))


def send_password_reset_email(recipient: str, reset_url: str) -> None:
    if not SMTP_HOST or not SMTP_FROM:
        raise RuntimeError("Email is not configured: set SMTP_HOST and SMTP_FROM")
    message = EmailMessage()
    message["Subject"] = "Reset your Coding AI password"
    message["From"] = SMTP_FROM
    message["To"] = recipient
    message.set_content(
        "Use this one-time link to reset your account password. "
        f"It expires in {PASSWORD_RESET_MINUTES} minutes:\n\n{reset_url}\n\n"
        "If you did not request this, you can ignore this email. Your password has not changed."
    )
    smtp_class = smtplib.SMTP_SSL if SMTP_USE_SSL else smtplib.SMTP
    with smtp_class(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        if SMTP_USE_TLS and not SMTP_USE_SSL:
            smtp.starttls()
        if SMTP_USERNAME:
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
        smtp.send_message(message)


def deliver_password_reset_email(recipient: str, reset_url: str, digest: str) -> None:
    try:
        send_password_reset_email(recipient, reset_url)
    except (OSError, RuntimeError, smtplib.SMTPException) as exc:
        LOGGER.error(
            "Could not send password reset email",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        with connect_db() as db:
            db.execute("DELETE FROM password_reset_tokens WHERE token_hash = ?", (digest,))


UNSAFE_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def normalise_origin(value: str, *, allow_path: bool = False) -> str | None:
    """Return a canonical HTTP(S) origin, or None for a malformed value."""
    value = value.strip()
    if not value or value.lower() == "null" or any(char.isspace() for char in value):
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or (
            not allow_path
            and (parsed.path not in {"", "/"} or parsed.query or parsed.fragment)
        )
    ):
        return None
    host = host.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = 80 if scheme == "http" else 443
    port_suffix = "" if port is None or port == default_port else f":{port}"
    return f"{scheme}://{host}{port_suffix}"


def normalise_trusted_host_pattern(value: str) -> str:
    """Validate one host or leading-wildcard domain for TrustedHostMiddleware."""
    value = value.strip().casefold().rstrip(".")
    wildcard = value.startswith("*.")
    host = value[2:] if wildcard else value
    if (
        not host
        or value == "*"
        or "*" in host
        or "://" in host
        or "/" in host
        or "\\" in host
        or ":" in host
        or any(character.isspace() for character in host)
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9_.-]*[a-z0-9])?", host)
    ):
        raise RuntimeError(
            f"Invalid TRUSTED_HOSTS entry {value!r}; use hostnames without schemes, ports, or paths"
        )
    return f"*.{host}" if wildcard else host


def public_origin() -> str | None:
    if not PUBLIC_BASE_URL:
        return None
    origin = normalise_origin(PUBLIC_BASE_URL)
    if origin is None:
        raise RuntimeError(
            "PUBLIC_BASE_URL must be a complete HTTP(S) origin without a path, query, or fragment"
        )
    return origin


def public_origin_host(origin: str) -> str:
    host = urllib.parse.urlsplit(origin).hostname
    if not host:
        raise RuntimeError("PUBLIC_BASE_URL does not contain a hostname")
    if ":" in host:
        raise RuntimeError("IPv6 PUBLIC_BASE_URL hosts are not supported by host validation")
    return normalise_trusted_host_pattern(host)


def build_trusted_hosts() -> list[str]:
    hosts = ["localhost", "127.0.0.1"]
    bind_host = HOST.strip().casefold().rstrip(".")
    if bind_host and bind_host not in {"0.0.0.0", "::", "[::]"}:
        hosts.append(normalise_trusted_host_pattern(bind_host))
    for configured_host in TRUSTED_HOSTS_CONFIG.split(","):
        if configured_host.strip():
            hosts.append(normalise_trusted_host_pattern(configured_host))
    origin = public_origin()
    if origin:
        hosts.append(public_origin_host(origin))
    return list(dict.fromkeys(hosts))


def add_public_origin_to_trusted_hosts(origin: str) -> None:
    host = public_origin_host(origin)
    if host not in TRUSTED_HOSTS:
        TRUSTED_HOSTS.append(host)


def validate_runtime_configuration() -> None:
    validate_application_configuration()
    origin = public_origin()
    email_settings_present = bool(
        SMTP_HOST or SMTP_FROM or SMTP_USERNAME or SMTP_PASSWORD
    )
    if email_settings_present and (not SMTP_HOST or not SMTP_FROM):
        raise RuntimeError(
            "Email configuration requires both SMTP_HOST and SMTP_FROM"
        )
    if SMTP_HOST and origin is None:
        raise RuntimeError(
            "Email is enabled, but PUBLIC_BASE_URL is unset. Configure the canonical public "
            "HTTP(S) origin used in verification and password-reset links."
        )


def allowed_request_origins(request: Request) -> set[str]:
    """Build exact allowed origins from this request and the configured public URL."""
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    scheme = forwarded_proto if forwarded_proto in {"http", "https"} else request.url.scheme
    host = request.headers.get("host", "").strip()
    allowed: set[str] = set()
    request_origin = normalise_origin(f"{scheme}://{host}") if host else None
    if request_origin:
        allowed.add(request_origin)
    public_origin = normalise_origin(PUBLIC_BASE_URL) if PUBLIC_BASE_URL else None
    if public_origin:
        allowed.add(public_origin)
    return allowed


TRUSTED_HOSTS = build_trusted_hosts()


@asynccontextmanager
async def lifespan(_: FastAPI):
    start_ntfy_error_notifier()
    ollama_worker_started = False
    backup_worker_started = False
    try:
        validate_runtime_configuration()
        initialise_db()
        start_ollama_worker()
        ollama_worker_started = True
        start_periodic_backup_worker()
        backup_worker_started = True
        try:
            yield
        finally:
            if backup_worker_started:
                stop_periodic_backup_worker()
            if ollama_worker_started:
                stop_ollama_worker()
    except Exception:
        LOGGER.exception("Application lifecycle failed")
        raise
    finally:
        stop_ntfy_error_notifier()


app = FastAPI(title="Ollama DeepSeek Chat", lifespan=lifespan)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=TRUSTED_HOSTS,
    www_redirect=False,
)
app.mount(
    "/assets",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "assets")),
    name="assets",
)


@app.middleware("http")
async def limit_project_upload_requests(request: Request, call_next):
    """Reject declared oversized multipart bodies before Starlette spools their files."""
    if request.method.upper() == "POST" and request.url.path == "/api/projects":
        declared_length = request.headers.get("content-length")
        if declared_length:
            try:
                body_bytes = int(declared_length)
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
            if body_bytes < 0:
                return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
            if body_bytes > PROJECT_UPLOAD_MAX_REQUEST_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "The project upload request is too large"},
                )
    return await call_next(request)


@app.middleware("http")
async def protect_unsafe_requests(request: Request, call_next):
    """Block cross-origin state changes while allowing local and ngrok same-origin use."""
    if request.method.upper() in UNSAFE_HTTP_METHODS:
        fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
        origin_header = request.headers.get("origin", "")
        referer_header = request.headers.get("referer", "")
        source_origin = (
            normalise_origin(origin_header)
            if origin_header
            else normalise_origin(referer_header, allow_path=True)
            if referer_header
            else None
        )
        allowed_origins = allowed_request_origins(request)
        if fetch_site == "cross-site" or source_origin not in allowed_origins:
            LOGGER.warning(
                "Blocked cross-origin request: request_id=%s method=%s path=%s "
                "source=%s fetch_site=%s",
                getattr(request.state, "request_id", "unavailable"),
                request.method,
                request.url.path,
                source_origin or "missing-or-invalid",
                fetch_site or "missing",
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "Cross-origin request blocked"},
            )
    return await call_next(request)


def content_security_policy(nonce: str) -> str:
    return "; ".join(
        (
            "default-src 'self'",
            "base-uri 'none'",
            "object-src 'none'",
            "frame-ancestors 'none'",
            "form-action 'self'",
            "connect-src 'self'",
            "img-src 'self' data:",
            "font-src 'self'",
            f"style-src 'self' 'nonce-{nonce}'",
            "style-src-attr 'unsafe-inline'",
            f"script-src 'nonce-{nonce}'",
            "script-src-attr 'none'",
            "worker-src 'none'",
        )
    )


def secure_html_response(
    request: Request,
    content: str,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    """Attach the current response nonce to trusted inline style and script blocks."""
    nonce = getattr(request.state, "csp_nonce", None)
    if not isinstance(nonce, str) or not nonce:
        raise RuntimeError("Content Security Policy nonce was not initialized")
    escaped_nonce = html.escape(nonce, quote=True)
    secured_content = content.replace(
        "<style>", f'<style nonce="{escaped_nonce}">'
    ).replace("<script>", f'<script nonce="{escaped_nonce}">')
    return HTMLResponse(secured_content, status_code=status_code, headers=headers)


def request_uses_https(request: Request) -> bool:
    forwarded_scheme = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    return request.url.scheme == "https" or forwarded_scheme == "https"


@app.middleware("http")
async def add_browser_security_headers(request: Request, call_next):
    """Apply browser protections consistently to successful and error responses."""
    nonce = secrets.token_urlsafe(24)
    request.state.csp_nonce = nonce
    response = await call_next(request)
    headers = {
        "Cache-Control": "no-store",
        "Content-Security-Policy": content_security_policy(nonce),
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }
    for name, value in headers.items():
        if name not in response.headers:
            response.headers[name] = value
    if request_uses_https(request):
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")


def request_id_from_header(value: str | None) -> str:
    """Accept safe upstream IDs and replace malformed/log-injection values."""
    if value and REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return uuid.uuid4().hex


def is_routine_successful_job_poll(method: str, path: str, status_code: int) -> bool:
    return (
        method == "GET"
        and status_code == 200
        and (
            re.fullmatch(r"/api/chat-jobs/[^/]+", path) is not None
            or path in {
                "/api/admin/jobs",
                "/api/admin/system-health",
                "/api/announcement",
            }
        )
    )


@app.middleware("http")
async def correlate_and_log_request(request: Request, call_next):
    request_id = request_id_from_header(request.headers.get("x-request-id"))
    request.state.request_id = request_id
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        duration_ms = (time.perf_counter() - started) * 1_000
        if not is_routine_successful_job_poll(
            request.method, request.url.path, status_code
        ):
            LOGGER.info(
                "HTTP request completed: request_id=%s method=%s path=%s status=%s "
                "duration_ms=%.1f",
                request_id,
                request.method,
                request.url.path,
                status_code,
                duration_ms,
            )


@app.exception_handler(RequestValidationError)
async def readable_validation_error(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    messages: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        if location == "message" and error.get("type") == "string_too_long":
            message = f"Message is too long. The maximum is {MAX_MESSAGE_CHARS:,} characters."
        else:
            message = str(error.get("msg", "Invalid request"))
            if location:
                message = f"{location}: {message}"
        messages.append(message)
    return JSONResponse(status_code=422, content={"detail": " ".join(messages)})


@app.exception_handler(Exception)
async def unexpected_server_error(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unavailable")
    LOGGER.error(
        "Unhandled HTTP error: request_id=%s method=%s path=%s",
        request_id,
        request.method,
        request.url.path,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "The server failed while processing the request.",
            "request_id": request_id,
        },
    )


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> Response:
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user["is_banned"]:
        return secure_html_response(request, FORBIDDEN_HTML, status_code=403)
    admin_link = '<a class="admin-link" href="/admin">Administration</a>' if user["is_admin"] else ""
    account_links = admin_link + '<a href="/changelog">Changelog</a>'
    page = HTML.replace("{{USERNAME}}", html.escape(user["username"]))
    page = page.replace("{{ADMIN_LINK}}", account_links)
    page = page.replace("{{OLLAMA_MODEL}}", html.escape(OLLAMA_MODEL))
    page = page.replace("{{DIRECT_MESSAGE_CHARS}}", str(DIRECT_MESSAGE_CHARS))
    page = page.replace("{{MAX_MESSAGE_CHARS}}", str(MAX_MESSAGE_CHARS))
    return secure_html_response(request, page, headers={"Cache-Control": "no-store"})


@app.get("/changelog", response_class=HTMLResponse)
def changelog_page(request: Request) -> Response:
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user["is_banned"]:
        return secure_html_response(request, FORBIDDEN_HTML, status_code=403)
    with connect_db() as db:
        entries = build_changelog_entries(db)
    page = CHANGELOG_HTML.replace("{{USERNAME}}", html.escape(user["username"]))
    page = page.replace("{{CHANGELOG_ENTRIES}}", render_changelog_html(entries))
    return secure_html_response(request, page, headers={"Cache-Control": "no-store"})


@app.get("/api/changelog.md")
def download_changelog(request: Request) -> Response:
    require_user(request)
    with connect_db() as db:
        entries = build_changelog_entries(db)
    return Response(
        content=render_changelog_markdown(entries, include_installation=True),
        media_type="text/markdown",
        headers={
            "Content-Disposition": 'attachment; filename="changelog.md"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return secure_html_response(request, LOGIN_HTML)


@app.get("/register", response_class=HTMLResponse)
def registration_page(request: Request) -> Response:
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    if not registration_is_enabled():
        return secure_html_response(
            request,
            REGISTRATION_CLOSED_HTML,
            status_code=403,
            headers={"Cache-Control": "no-store"},
        )
    return secure_html_response(request, REGISTER_HTML)


@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request) -> Response:
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return secure_html_response(
        request, FORGOT_PASSWORD_HTML, headers={"Cache-Control": "no-store"}
    )


@app.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str = "") -> Response:
    row = None
    if token:
        with connect_db() as db:
            row = db.execute(
                """
                SELECT id FROM password_reset_tokens
                WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?
                """,
                (token_digest(token), int(time.time())),
            ).fetchone()
    if row is None:
        return secure_html_response(request, RESET_EXPIRED_HTML, status_code=400)
    page = RESET_PASSWORD_HTML.replace("{{TOKEN}}", html.escape(token, quote=True))
    return secure_html_response(request, page, headers={"Cache-Control": "no-store"})


@app.get("/forbidden", response_class=HTMLResponse)
def forbidden_page(request: Request) -> HTMLResponse:
    return secure_html_response(request, FORBIDDEN_HTML, status_code=403)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request) -> Response:
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user["is_banned"] or not user["is_admin"]:
        return secure_html_response(request, FORBIDDEN_HTML, status_code=403)
    page = ADMIN_HTML.replace("{{USERNAME}}", html.escape(user["username"]))
    return secure_html_response(
        request,
        page.replace("{{IS_OWNER}}", "true" if user["is_owner"] else "false"),
    )


@app.get("/verify", response_class=HTMLResponse)
def verification_page(request: Request, token: str = "") -> Response:
    row = None
    if token:
        with connect_db() as db:
            row = db.execute(
                """
                SELECT email FROM registration_tokens
                WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?
                """,
                (token_digest(token), int(time.time())),
            ).fetchone()
    if row is None:
        return secure_html_response(request, EXPIRED_HTML, status_code=400)
    page = VERIFY_HTML.replace("{{TOKEN}}", html.escape(token, quote=True))
    return secure_html_response(
        request, page.replace("{{EMAIL}}", html.escape(row["email"]))
    )


@app.get("/health")
def health() -> dict[str, str]:
    """Cheap liveness probe that does not touch downstream dependencies."""
    return {"status": "ok"}


def installed_ollama_models() -> set[str]:
    """Return model names advertised by Ollama, with a bounded response and timeout."""
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/tags",
        headers={"Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(
        request, timeout=READINESS_OLLAMA_TIMEOUT_SECONDS
    ) as response:
        raw_payload = response.read(1_000_001)
    if len(raw_payload) > 1_000_000:
        raise RuntimeError("Ollama model-list response exceeded 1 MB")
    payload = json.loads(raw_payload)
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise RuntimeError("Ollama returned an invalid model list")
    names: set[str] = set()
    for model in models:
        if not isinstance(model, dict):
            continue
        for key in ("name", "model"):
            value = model.get(key)
            if isinstance(value, str) and value.strip():
                names.add(value.strip().casefold())
    return names


def configured_ollama_model_is_installed(models: set[str]) -> bool:
    configured = OLLAMA_MODEL.strip().casefold()
    if configured in models:
        return True
    return ":" not in configured and f"{configured}:latest" in models


def build_readiness_report() -> dict[str, object]:
    """Check every dependency required to accept and complete chat work."""
    checks: dict[str, str] = {}
    try:
        with connect_db() as db:
            db.execute("SELECT 1").fetchone()
        checks["database"] = "ok"
    except (OSError, sqlite3.Error) as exc:
        checks["database"] = "unavailable"
        LOGGER.warning("Readiness database check failed: %s", exc)

    worker = OLLAMA_WORKER_THREAD
    checks["job_worker"] = (
        "ok"
        if worker is not None
        and worker.is_alive()
        and not OLLAMA_WORKER_STOP.is_set()
        else "unavailable"
    )

    try:
        models = installed_ollama_models()
        checks["ollama"] = "ok"
        checks["configured_model"] = (
            "ok" if configured_ollama_model_is_installed(models) else "unavailable"
        )
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        checks["ollama"] = "unavailable"
        checks["configured_model"] = "unknown"
        LOGGER.warning("Readiness Ollama check failed: %s", exc)

    ready = all(value == "ok" for value in checks.values())
    return {"status": "ready" if ready else "not_ready", "checks": checks}


def cached_readiness_report() -> dict[str, object]:
    global READINESS_CACHE
    now = time.monotonic()
    with READINESS_CACHE_LOCK:
        cached = READINESS_CACHE
        if cached is not None and now - cached[0] < READINESS_CACHE_SECONDS:
            return cached[1]
    report = build_readiness_report()
    with READINESS_CACHE_LOCK:
        READINESS_CACHE = (time.monotonic(), report)
    return report


@app.get("/ready")
def readiness() -> JSONResponse:
    report = cached_readiness_report()
    return JSONResponse(
        status_code=200 if report["status"] == "ready" else 503,
        content=report,
    )


@app.post("/api/register")
def request_registration(
    payload: RegistrationRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    started_at = time.monotonic()
    if not registration_is_enabled():
        raise HTTPException(status_code=403, detail="Registration is currently closed")
    email = validate_email(payload.email)
    generic_message = "If this address can be registered, a verification email has been sent."
    if not reserve_registration_ip_attempt(request):
        return generic_auth_email_response(started_at, generic_message)

    now = int(time.time())
    token = secrets.token_urlsafe(32)
    digest = token_digest(token)
    should_send = False
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        if not registration_is_enabled(db):
            raise HTTPException(status_code=403, detail="Registration is currently closed")
        existing = db.execute(
            "SELECT 1 FROM users WHERE email = ?", (email,)
        ).fetchone()
        if existing is None:
            recent = db.execute(
                """
                SELECT requested_at FROM registration_tokens
                WHERE email = ? AND used_at IS NULL
                ORDER BY requested_at DESC LIMIT 1
                """,
                (email,),
            ).fetchone()
            if not recent or now - int(recent["requested_at"]) >= REGISTRATION_RESEND_SECONDS:
                db.execute(
                    "UPDATE registration_tokens SET used_at = CURRENT_TIMESTAMP "
                    "WHERE email = ? AND used_at IS NULL",
                    (email,),
                )
                db.execute(
                    """
                    INSERT INTO registration_tokens(email, token_hash, expires_at, requested_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (email, digest, now + VERIFICATION_MINUTES * 60, now),
                )
                should_send = True
    if should_send:
        url = f"{verification_base_url()}/verify?token={token}"
        background_tasks.add_task(deliver_verification_email, email, url, digest)
    return generic_auth_email_response(started_at, generic_message)


@app.post("/api/complete-registration")
def complete_registration(payload: CompleteRegistrationRequest) -> dict[str, str]:
    if not registration_is_enabled():
        raise HTTPException(status_code=403, detail="Registration is currently closed")
    username = validate_username(payload.username)
    validate_password(payload.password)
    encoded_password = hash_password(payload.password)
    now = int(time.time())
    try:
        with connect_db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not registration_is_enabled(db):
                raise HTTPException(status_code=403, detail="Registration is currently closed")
            registration = db.execute(
                """
                SELECT id, email FROM registration_tokens
                WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?
                """,
                (token_digest(payload.token), now),
            ).fetchone()
            if registration is None:
                raise HTTPException(status_code=400, detail="This verification link is invalid or expired")
            owner_username = username.casefold() == OWNER_USERNAME.casefold()
            owner_email = registration["email"].casefold() == OWNER_EMAIL.casefold()
            if owner_username != owner_email:
                raise HTTPException(status_code=409, detail="That username or email address is reserved")
            db.execute(
                """
                INSERT INTO users(email, username, password_hash, is_admin, is_owner)
                VALUES (?, ?, ?, ?, ?)
                """,
                (registration["email"], username, encoded_password, int(owner_email), int(owner_email)),
            )
            db.execute(
                "UPDATE registration_tokens SET used_at = CURRENT_TIMESTAMP WHERE id = ?",
                (registration["id"],),
            )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="That email or username is already registered") from exc
    return {"message": "Account created. You can now log in."}


@app.post("/api/login")
def login(payload: LoginRequest, request: Request, response: Response) -> dict[str, str]:
    identity = payload.username.strip()
    with connect_db() as db:
        expire_user_bans(db)
        user = db.execute(
            """
            SELECT id, username, password_hash, is_banned, ban_reason, banned_until
            FROM users WHERE username = ? OR email = ?
            """,
            (identity, identity),
        ).fetchone()
    account_scope = login_account_scope(user["id"] if user else None, identity)
    retry_after, locked = reserve_login_attempt(account_scope, login_ip_scope(request))
    if retry_after:
        detail = (
            "Too many failed attempts. This login is temporarily locked."
            if locked
            else "Please wait before trying to log in again."
        )
        raise HTTPException(
            status_code=429,
            detail=detail,
            headers={"Retry-After": str(retry_after)},
        )
    password_hash = user["password_hash"] if user else DUMMY_PASSWORD_HASH
    password_valid = password_matches(payload.password, password_hash)
    if user is None or not password_valid:
        retry_after, locked = record_failed_login(account_scope)
        raise HTTPException(
            status_code=429 if locked else 401,
            detail=(
                "Too many failed attempts. This login is locked for five minutes."
                if locked
                else "Invalid username/email or password"
            ),
            headers={"Retry-After": str(retry_after)},
        )
    if user["is_banned"]:
        clear_login_failures(account_scope)
        raise HTTPException(status_code=403, detail=banned_account_detail(user))
    clear_login_failures(account_scope)
    now = int(time.time())
    if password_needs_rehash(user["password_hash"]):
        upgraded_hash = hash_password(payload.password)
        with connect_db() as db:
            db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ? AND password_hash = ?",
                (upgraded_hash, user["id"], user["password_hash"]),
            )
    with connect_db() as db:
        db.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (now, user["id"]),
        )
    set_login_cookie(response, request, user["id"])
    return {"message": "Logged in", "username": user["username"]}


@app.post("/api/forgot-password")
def request_password_reset(
    payload: PasswordResetRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    started_at = time.monotonic()
    identity = payload.identity.strip()
    generic_message = (
        "If that username or email address exists, a password reset link has been sent."
    )
    if not reserve_password_reset_ip_attempt(request):
        return generic_auth_email_response(started_at, generic_message)
    now = int(time.time())
    token = ""
    digest = ""
    recipient = ""
    should_send = False
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        user = db.execute(
            "SELECT id, email FROM users WHERE username = ? OR email = ?",
            (identity, identity),
        ).fetchone()
        if user is not None:
            recent = db.execute(
                """
                SELECT requested_at FROM password_reset_tokens
                WHERE user_id = ? ORDER BY requested_at DESC LIMIT 1
                """,
                (user["id"],),
            ).fetchone()
            if not recent or now - recent["requested_at"] >= PASSWORD_RESET_RESEND_SECONDS:
                token = secrets.token_urlsafe(32)
                digest = token_digest(token)
                recipient = user["email"]
                db.execute(
                    "UPDATE password_reset_tokens SET used_at = CURRENT_TIMESTAMP "
                    "WHERE user_id = ? AND used_at IS NULL",
                    (user["id"],),
                )
                db.execute(
                    """
                    INSERT INTO password_reset_tokens(
                        user_id, token_hash, expires_at, requested_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (user["id"], digest, now + PASSWORD_RESET_MINUTES * 60, now),
                )
                should_send = True
    if should_send:
        reset_url = f"{verification_base_url()}/reset-password?token={token}"
        background_tasks.add_task(deliver_password_reset_email, recipient, reset_url, digest)
    return generic_auth_email_response(started_at, generic_message)


@app.post("/api/complete-password-reset")
def complete_password_reset(payload: CompletePasswordResetRequest) -> dict[str, str]:
    validate_password(payload.password)
    digest = token_digest(payload.token)
    now = int(time.time())
    with connect_db() as db:
        reset = db.execute(
            """
            SELECT user_id FROM password_reset_tokens
            WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?
            """,
            (digest, now),
        ).fetchone()
    if reset is None:
        raise HTTPException(status_code=400, detail="This password reset link is invalid or expired")
    encoded_password = hash_password(payload.password)
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        reset = db.execute(
            """
            SELECT user_id FROM password_reset_tokens
            WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?
            """,
            (digest, int(time.time())),
        ).fetchone()
        if reset is None:
            raise HTTPException(status_code=400, detail="This password reset link is invalid or expired")
        db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (encoded_password, reset["user_id"]),
        )
        db.execute(
            "UPDATE password_reset_tokens SET used_at = CURRENT_TIMESTAMP "
            "WHERE user_id = ? AND used_at IS NULL",
            (reset["user_id"],),
        )
        db.execute("DELETE FROM login_sessions WHERE user_id = ?", (reset["user_id"],))
        db.execute(
            "DELETE FROM login_throttles WHERE scope_hash = ?",
            (login_account_scope(reset["user_id"], ""),),
        )
    return {"message": "Password updated. You can now log in."}


@app.post("/api/logout")
def logout(request: Request, response: Response) -> dict[str, str]:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with connect_db() as db:
            db.execute("DELETE FROM login_sessions WHERE token_hash = ?", (token_digest(token),))
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        secure=request_uses_https(request),
        samesite="lax",
    )
    return {"message": "Logged out"}


def stream_account_export(
    user_id: int,
    account: dict[str, object],
    exported_at: str,
) -> Iterator[str]:
    """Stream portable account/chat data without loading the full export into memory."""
    def encode(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    yield '{"format_version":1,"exported_at":' + encode(exported_at)
    yield ',"account":' + encode(account) + ',"chats":['
    first_chat = True
    last_chat_created_at: str | None = None
    last_chat_id = ""
    while True:
        with connect_db() as db:
            if last_chat_created_at is None:
                chat_rows = db.execute(
                    """
                    SELECT id, title, title_is_custom, created_at, updated_at
                    FROM chat_histories WHERE user_id = ?
                    ORDER BY created_at, id LIMIT 100
                    """,
                    (user_id,),
                ).fetchall()
            else:
                chat_rows = db.execute(
                    """
                    SELECT id, title, title_is_custom, created_at, updated_at
                    FROM chat_histories
                    WHERE user_id = ? AND (
                        created_at > ? OR (created_at = ? AND id > ?)
                    )
                    ORDER BY created_at, id LIMIT 100
                    """,
                    (user_id, last_chat_created_at, last_chat_created_at, last_chat_id),
                ).fetchall()
            chats = [dict(row) for row in chat_rows]
        if not chats:
            break
        for chat_data in chats:
            if not first_chat:
                yield ","
            first_chat = False
            yield encode(chat_data)[:-1] + ',"messages":['
            first_message = True
            session_id = f"user-{user_id}:{chat_data['id']}"
            last_message_id = 0
            while True:
                with connect_db() as db:
                    message_rows = db.execute(
                        """
                        SELECT id, role, content, context_content, created_at
                        FROM messages
                        WHERE session_id = ? AND id > ?
                        ORDER BY id LIMIT 100
                        """,
                        (session_id, last_message_id),
                    ).fetchall()
                    messages = [dict(row) for row in message_rows]
                if not messages:
                    break
                for message_data in messages:
                    if not first_message:
                        yield ","
                    first_message = False
                    yield encode(message_data)
                last_message_id = int(messages[-1]["id"])
            yield '],"projects":['
            first_project = True
            last_project_created_at: str | None = None
            last_project_id = ""
            while True:
                with connect_db() as db:
                    if last_project_created_at is None:
                        project_rows = db.execute(
                            """
                            SELECT id, name, source_kind, status, file_count,
                                   skipped_file_count, total_bytes, error,
                                   primary_language, languages_json, main_file_path,
                                   inventory_status, inventory_error,
                                   created_at, updated_at
                            FROM projects
                            WHERE chat_id = ? AND user_id = ?
                            ORDER BY created_at, id LIMIT 50
                            """,
                            (chat_data["id"], user_id),
                        ).fetchall()
                    else:
                        project_rows = db.execute(
                            """
                            SELECT id, name, source_kind, status, file_count,
                                   skipped_file_count, total_bytes, error,
                                   primary_language, languages_json, main_file_path,
                                   inventory_status, inventory_error,
                                   created_at, updated_at
                            FROM projects
                            WHERE chat_id = ? AND user_id = ? AND (
                                created_at > ? OR (created_at = ? AND id > ?)
                            )
                            ORDER BY created_at, id LIMIT 50
                            """,
                            (
                                chat_data["id"],
                                user_id,
                                last_project_created_at,
                                last_project_created_at,
                                last_project_id,
                            ),
                        ).fetchall()
                    projects = [dict(row) for row in project_rows]
                if not projects:
                    break
                for project_data in projects:
                    with connect_db() as db:
                        project_data["uploads"] = [dict(row) for row in db.execute(
                            "SELECT id, source_kind, name, created_at FROM project_upload_batches WHERE project_id = ? ORDER BY id",
                            (project_data["id"],),
                        )]
                    if not first_project:
                        yield ","
                    first_project = False
                    yield encode(project_data)[:-1] + ',"files":['
                    first_file = True
                    last_file_id = 0
                    while True:
                        with connect_db() as db:
                            file_rows = db.execute(
                                """
                                SELECT id, path, content, size_bytes, sha256, upload_batch_id,
                                       is_binary, file_kind, language,
                                       language_confidence, detection_method,
                                       encoding, line_count, is_generated,
                                       is_entrypoint_candidate, analysis_eligible,
                                       created_at
                                FROM project_files
                                WHERE project_id = ? AND id > ?
                                ORDER BY id LIMIT 100
                                """,
                                (project_data["id"], last_file_id),
                            ).fetchall()
                        if not file_rows:
                            break
                        for file_row in file_rows:
                            if not first_file:
                                yield ","
                            first_file = False
                            file_data = dict(file_row)
                            file_data["content_base64"] = base64.b64encode(
                                bytes(file_data.pop("content"))
                            ).decode("ascii")
                            yield encode(file_data)
                        last_file_id = int(file_rows[-1]["id"])
                    yield "]}"
                last_project_created_at = str(projects[-1]["created_at"])
                last_project_id = str(projects[-1]["id"])
            yield "]}"
        last_chat_created_at = str(chats[-1]["created_at"])
        last_chat_id = str(chats[-1]["id"])
    yield "]}"


@app.get("/api/account/export")
def export_account_data(request: Request) -> StreamingResponse:
    user = require_user(request)
    with connect_db() as db:
        account_row = db.execute(
            """
            SELECT id, email, username, created_at, email_verified_at,
                   is_admin, is_owner, is_banned
            FROM users WHERE id = ?
            """,
            (user["id"],),
        ).fetchone()
    if account_row is None:
        raise HTTPException(status_code=404, detail="Account not found")
    exported_at = datetime.now(timezone.utc).isoformat()
    safe_username = re.sub(r"[^A-Za-z0-9_-]", "_", str(account_row["username"]))
    filename = f"{safe_username}-chat-export-{exported_at[:10]}.json"
    return StreamingResponse(
        stream_account_export(int(user["id"]), dict(account_row), exported_at),
        media_type="application/json",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


def encode_chat_list_cursor(row: sqlite3.Row) -> str:
    payload = json.dumps(
        [row["updated_at"], row["created_at"], row["id"]],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_chat_list_cursor(value: str) -> tuple[str, str, str]:
    try:
        padding = "=" * (-len(value) % 4)
        payload = base64.b64decode(
            (value + padding).encode("ascii"), altchars=b"-_", validate=True
        )
        decoded = json.loads(payload)
    except (
        UnicodeEncodeError,
        UnicodeDecodeError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise HTTPException(status_code=422, detail="Invalid chat-list cursor") from exc
    if (
        not isinstance(decoded, list)
        or len(decoded) != 3
        or any(not isinstance(item, str) or not item or len(item) > 200 for item in decoded)
        or any(any(ord(character) < 32 for character in item) for item in decoded)
    ):
        raise HTTPException(status_code=422, detail="Invalid chat-list cursor")
    return decoded[0], decoded[1], decoded[2]


@app.get("/api/chats")
def list_chats(
    request: Request,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> dict[str, object]:
    user = require_user(request)
    boundary = decode_chat_list_cursor(cursor) if cursor else None
    with connect_db() as db:
        if boundary is None:
            rows = db.execute(
                """
                SELECT id, title, created_at, updated_at
                FROM chat_histories WHERE user_id = ?
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT ?
                """,
                (user["id"], CHAT_LIST_PAGE_SIZE + 1),
            ).fetchall()
        else:
            updated_at, created_at, chat_id = boundary
            rows = db.execute(
                """
                SELECT id, title, created_at, updated_at
                FROM chat_histories
                WHERE user_id = ? AND (
                    updated_at < ?
                    OR (updated_at = ? AND created_at < ?)
                    OR (updated_at = ? AND created_at = ? AND id < ?)
                )
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT ?
                """,
                (
                    user["id"],
                    updated_at,
                    updated_at,
                    created_at,
                    updated_at,
                    created_at,
                    chat_id,
                    CHAT_LIST_PAGE_SIZE + 1,
                ),
            ).fetchall()
    has_more = len(rows) > CHAT_LIST_PAGE_SIZE
    page = rows[:CHAT_LIST_PAGE_SIZE]
    return {
        "chats": [dict(row) for row in page],
        "next_cursor": encode_chat_list_cursor(page[-1]) if has_more and page else None,
    }


@app.post("/api/chats", status_code=201)
def create_chat(request: Request) -> dict[str, str]:
    user = require_user(request)
    chat_id = str(uuid.uuid4())
    with connect_db() as db:
        db.execute(
            "INSERT INTO chat_histories(id, user_id, title) VALUES (?, ?, 'New chat')",
            (chat_id, user["id"]),
        )
    return {"id": chat_id, "title": "New chat"}


def call_compatibility_coverage(
    values: dict[str, object],
) -> dict[str, int | float]:
    """Return coverage of project-local calls, excluding calls we cannot own-check."""
    checked = max(0, int(values.get("call_compatibility_checked_count") or 0))
    not_checked = max(
        0,
        min(checked, int(values.get("call_compatibility_not_checked_count") or 0)),
    )
    in_scope = checked - not_checked
    unknown = max(
        0,
        min(in_scope, int(values.get("call_compatibility_unknown_count") or 0)),
    )
    resolved = in_scope - unknown
    coverage = round((resolved * 100.0) / in_scope, 1) if in_scope else (
        100.0 if checked else 0.0
    )
    return {
        "call_compatibility_in_scope_count": in_scope,
        "call_compatibility_resolved_count": resolved,
        "call_compatibility_coverage_percent": coverage,
    }


def project_response(project: sqlite3.Row | dict[str, object]) -> dict[str, object]:
    values = dict(project)
    try:
        languages = json.loads(str(values.get("languages_json") or "[]"))
    except json.JSONDecodeError:
        languages = []
    if not isinstance(languages, list):
        languages = []
    response = {
        key: values.get(key)
        for key in (
            "id",
            "chat_id",
            "name",
            "source_kind",
            "main_file_path",
            "status",
            "file_count",
            "skipped_file_count",
            "total_bytes",
            "created_at",
            "primary_language",
            "inventory_status",
            "parser_status",
            "parser_supported_file_count",
            "parser_parsed_file_count",
            "parser_syntax_error_file_count",
            "parser_failed_file_count",
            "structure_status",
            "indexed_file_count",
            "definition_count",
            "dependency_count",
            "call_count",
            "resolved_dependency_count",
            "ambiguous_dependency_count",
            "function_analysis_status",
            "function_analysis_total_count",
            "function_analysis_completed_count",
            "function_analysis_failed_count",
            "function_analysis_skipped_count",
            "function_analysis_cache_hit_count",
            "function_analysis_model_request_count",
            "function_analysis_batch_request_count",
            "function_analysis_deterministic_count",
            "function_analysis_batch_fallback_count",
            "function_analysis_batch_error",
            "call_compatibility_status",
            "call_compatibility_checked_count",
            "call_compatibility_incompatible_count",
            "call_compatibility_unknown_count",
            "call_compatibility_not_checked_count",
        )
    }
    response["languages"] = languages
    response.update(call_compatibility_coverage(values))
    return response


def enforce_project_storage_limit(
    db: sqlite3.Connection,
    user_id: int,
    additional_bytes: int,
) -> None:
    limit_row = db.execute(
        "SELECT storage_limit_bytes FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if limit_row is None:
        raise HTTPException(status_code=401, detail="Please log in")
    if limit_row["storage_limit_bytes"] is None:
        return
    stored_bytes = int(
        db.execute(
            """
            SELECT
                (SELECT COALESCE(SUM(length(CAST(content AS BLOB))), 0)
                 FROM messages WHERE session_id LIKE ('user-' || ? || ':%'))
                +
                (SELECT COALESCE(SUM(file.size_bytes), 0)
                 FROM project_files AS file
                 JOIN projects AS project ON project.id = file.project_id
                 WHERE project.user_id = ?)
            """,
            (user_id, user_id),
        ).fetchone()[0]
    )
    if stored_bytes + additional_bytes > int(limit_row["storage_limit_bytes"]):
        raise HTTPException(
            status_code=413,
            detail=(
                "Your account has reached its storage limit. "
                "Delete an older chat or project, or contact the site owner."
            ),
        )


@app.post("/api/projects", status_code=201)
async def upload_project(
    request: Request,
    chat_id: Annotated[str, Form(min_length=1, max_length=100)],
    source_kind: Annotated[Literal["zip", "folder", "files"], Form()],
    files: Annotated[list[UploadFile], File()],
    relative_paths: Annotated[str, Form()] = "[]",
    project_id: Annotated[str | None, Form(max_length=100)] = None,
    project_name: Annotated[str | None, Form(max_length=200)] = None,
) -> dict[str, object]:
    """Safely expand and persist one project upload without executing its contents."""
    user = require_user(request)
    with connect_db() as db:
        owned_chat = db.execute(
            "SELECT 1 FROM chat_histories WHERE id = ? AND user_id = ?",
            (chat_id, user["id"]),
        ).fetchone()
    if owned_chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    target_project_id = project_id
    upload_name = files[0].filename if source_kind == "zip" and files else None
    try:
        if source_kind == "zip":
            if len(files) != 1:
                raise ProjectUploadError("Select exactly one ZIP file")
            bundle = ingest_zip(
                files[0].file,
                files[0].filename or "Uploaded project.zip",
                PROJECT_UPLOAD_LIMITS,
            )
        elif source_kind == "files":
            bundle = ingest_files(
                [(upload.filename or "", upload.file) for upload in files],
                PROJECT_UPLOAD_LIMITS,
            )
        else:
            paths = decode_relative_paths(relative_paths, len(files))
            bundle = ingest_folder(
                [(path, upload.file) for path, upload in zip(paths, files)],
                PROJECT_UPLOAD_LIMITS,
            )
    except ProjectUploadTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ProjectUploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        for upload in files:
            await upload.close()

    project_id = target_project_id or str(uuid.uuid4())
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        owned_chat = db.execute(
            "SELECT 1 FROM chat_histories WHERE id = ? AND user_id = ?",
            (chat_id, user["id"]),
        ).fetchone()
        if owned_chat is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        enforce_project_storage_limit(db, int(user["id"]), bundle.total_bytes)
        if target_project_id:
            project = owned_project(db, project_id, int(user["id"]), editing=True)
            if project["chat_id"] != chat_id:
                raise HTTPException(404, "Project not found in this chat")
            validate_append(db, project_id, bundle, PROJECT_UPLOAD_LIMITS)
            delete_project_function_analysis_cache(db, project_id=project_id, user_id=int(user["id"]))
            db.execute("UPDATE projects SET skipped_file_count = skipped_file_count + ? WHERE id = ?", (bundle.skipped_files, project_id))
        else:
            db.execute(
                """
                INSERT INTO projects(
                    id, user_id, chat_id, name, source_kind, file_count,
                    skipped_file_count, total_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id, user["id"], chat_id, safe_project_name(project_name or "", bundle.name), bundle.source_kind,
                    len(bundle.files), bundle.skipped_files, bundle.total_bytes,
                ),
            )
        store_upload_batch(db, project_id, bundle, source_kind, upload_name or bundle.name)
        if target_project_id:
            rebuild_project(db, project_id)
            return project_response(db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())
        inventory_completed = False
        try:
            inventory_project_database(db, project_id)
        except (OSError, ValueError, sqlite3.Error, UnicodeError) as exc:
            db.execute(
                """
                UPDATE projects
                SET inventory_status = 'failed', inventory_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (f"{type(exc).__name__}: {exc}"[:1_000], project_id),
            )
            LOGGER.warning("Could not inventory new uploaded project %s: %s", project_id, exc)
        else:
            inventory_completed = True
        if inventory_completed:
            try:
                parse_project_database(db, project_id)
            except (OSError, ValueError, sqlite3.Error, UnicodeError) as exc:
                db.execute(
                    """
                    UPDATE projects
                    SET parser_status = 'failed', parser_error = ?,
                        structure_status = 'failed', structure_error = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        f"{type(exc).__name__}: {exc}"[:1_000],
                        f"{type(exc).__name__}: {exc}"[:1_000],
                        project_id,
                    ),
                )
                LOGGER.warning("Could not parse new uploaded project %s: %s", project_id, exc)
        row = db.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    if row is None:
        raise RuntimeError("The uploaded project record could not be read back")
    return project_response(row)


@app.get("/api/projects/{project_id}/tree")
def get_project_tree(project_id: str, request: Request) -> dict[str, object]:
    user = require_user(request)
    with connect_db() as db:
        project = owned_project(db, project_id, int(user["id"]))
        uploads = [dict(row) for row in db.execute(
            "SELECT id, name, source_kind FROM project_upload_batches WHERE project_id = ? ORDER BY id",
            (project_id,),
        )]
        files = [dict(row) for row in db.execute(
            """SELECT f.id, f.path, f.upload_batch_id, f.size_bytes, f.language,
                      f.is_binary, f.analysis_eligible, COUNT(s.id) AS function_count,
                      COALESCE(SUM(s.analysis_status IN ('completed','failed','skipped')),0) AS processed_function_count,
                      COALESCE(SUM(s.analysis_status='processing'),0) AS processing_function_count,
                      COALESCE(SUM(s.analysis_status='failed'),0) AS failed_function_count,
                      COALESCE(SUM(s.analysis_status='skipped'),0) AS skipped_function_count,
                      COALESCE(SUM(CASE WHEN EXISTS(
                          SELECT 1 FROM project_symbol_issues i
                          WHERE i.symbol_id=s.id AND i.severity='error'
                      ) THEN 1 ELSE 0 END),0) AS error_function_count,
                      COALESCE(SUM(CASE WHEN EXISTS(
                          SELECT 1 FROM project_symbol_issues i
                          WHERE i.symbol_id=s.id AND i.severity IN ('warning','unsafe')
                      ) THEN 1 ELSE 0 END),0) AS warning_function_count
               FROM project_files f LEFT JOIN project_symbols s ON s.file_id=f.id
                   AND s.symbol_kind IN ('function','method')
               WHERE f.project_id=? GROUP BY f.id ORDER BY f.path COLLATE NOCASE, f.path""",
            (project_id,),
        )]
        functions = [dict(row) for row in db.execute(
            """SELECT s.id, s.file_id, s.name, s.qualified_name,
                      s.start_line, s.end_line, s.start_byte, s.end_byte,
                      s.analysis_status, f.language,
                      a.summary,
                      CASE WHEN json_valid(a.response_json)
                           THEN json_extract(a.response_json, '$.analysis_method') END
                          AS analysis_method,
                      CASE WHEN json_valid(a.response_json)
                           THEN json_extract(a.response_json, '$.review_status') END
                          AS review_status,
                      (SELECT COUNT(*) FROM project_symbol_issues i
                       WHERE i.symbol_id=s.id AND i.severity='error') AS error_count,
                      (SELECT COUNT(*) FROM project_symbol_issues i
                       WHERE i.symbol_id=s.id AND i.severity IN ('warning','unsafe')) AS warning_count
               FROM project_symbols s
               JOIN project_files f ON f.id=s.file_id
               LEFT JOIN project_symbol_analyses a ON a.symbol_id=s.id
               WHERE s.project_id=? AND s.symbol_kind IN ('function','method')
               ORDER BY s.file_id, s.start_byte, s.id""",
            (project_id,),
        )]
        source_by_file = {
            int(row["id"]): bytes(row["content"])
            for row in db.execute(
                "SELECT id,content FROM project_files WHERE project_id=?",
                (project_id,),
            ).fetchall()
        }
        job = db.execute(
            """SELECT status, progress_stage FROM chat_jobs WHERE project_id=?
               AND job_kind='project_analysis' AND status IN ('queued','processing')
               ORDER BY created_at DESC LIMIT 1""", (project_id,),
        ).fetchone()
        for file in files:
            active = bool(job and job["status"] == "processing" and file["processing_function_count"])
            file["analysis_state"] = (
                ("paused" if job["progress_stage"] == "paused" else "analysing") if active
                else "processed" if file["function_count"] and file["processed_function_count"] == file["function_count"]
                else "pending" if file["function_count"] else "no_functions"
            )
        active_job = bool(job and job["status"] == "processing")
        for function in functions:
            function["content"] = source_by_file.get(int(function["file_id"]), b"")
            function.update(function_source_metadata(function))
            for internal_key in ("content", "language", "start_byte", "end_byte"):
                function.pop(internal_key)
            summary = function.pop("summary")
            analysis_method = function.pop("analysis_method")
            review_status = function.pop("review_status")
            description = None
            if (
                function["analysis_status"] == "completed"
                and analysis_method in {"model", "deterministic"}
                and review_status == "complete"
            ):
                compact = " ".join(str(summary or "").split())
                if compact:
                    description = (
                        compact
                        if len(compact) <= FUNCTION_TREE_DESCRIPTION_MAX_CHARS
                        else compact[: FUNCTION_TREE_DESCRIPTION_MAX_CHARS - 3].rstrip() + "..."
                    )
            function["description"] = description
            function["description_status"] = (
                "available" if description and analysis_method == "model"
                else "deterministic" if description else "unknown"
            )
            status = str(function["analysis_status"])
            function["analysis_state"] = (
                ("paused" if job["progress_stage"] == "paused" else "analysing")
                if active_job and status == "processing"
                else "processed" if status == "completed"
                else "failed" if status in {"failed", "stale"}
                else "skipped" if status == "skipped"
                else "pending"
            )
        budgets = db.execute("""SELECT s.file_id,s.analysis_status,b.budget_json FROM function_analysis_budgets b
                                JOIN project_symbols s ON s.id=b.symbol_id WHERE s.project_id=?
                                ORDER BY (s.analysis_status='processing') DESC,b.updated_at DESC,s.id DESC""", (project_id,)).fetchall()
        by_file = {}
        for row in budgets:
            if row["file_id"] not in by_file:
                by_file[row["file_id"]] = json.loads(row["budget_json"])
        for file in files:
            file["analysis_budget"] = by_file.get(file["id"])
    return {
        "project": project_response(project),
        "uploads": uploads,
        "files": files,
        "functions": functions,
    }


@app.get("/api/projects/{project_id}/functions/{symbol_id}/callers")
def get_project_function_callers(
    project_id: str,
    symbol_id: int,
    request: Request,
) -> dict[str, object]:
    """Return statically resolved project-local calls to one function."""
    user = require_user(request)
    with connect_db() as db:
        project = owned_project(db, project_id, int(user["id"]))
        target_row = db.execute(
            """SELECT s.id,s.name,s.qualified_name,s.start_line,s.end_line,
                      s.start_byte,s.end_byte,f.path,f.language,f.content
               FROM project_symbols s
               JOIN project_files f ON f.id=s.file_id
               WHERE s.project_id=? AND s.id=?
                     AND s.symbol_kind IN ('function','method')""",
            (project_id, symbol_id),
        ).fetchone()
        if target_row is None:
            raise HTTPException(status_code=404, detail="Function not found")
        call_rows = db.execute(
            """SELECT call.id,call.callee,call.usage_kind,call.start_line,
                      call.start_column,call.end_line,call.end_column,
                      call.start_byte,call.end_byte,call.file_id,file.path,
                      caller.id AS caller_symbol_id,
                      caller.qualified_name AS caller_name
               FROM project_calls call
               JOIN project_files file ON file.id=call.file_id
               LEFT JOIN project_symbols caller ON caller.id=call.caller_symbol_id
               WHERE call.project_id=? AND call.resolution_status='internal'
                     AND call.resolved_symbol_id=?
               ORDER BY file.path COLLATE NOCASE,call.start_byte,call.id""",
            (project_id, symbol_id),
        ).fetchall()
        caller_source_by_file = {
            int(row["id"]): bytes(row["content"])
            for row in db.execute(
                "SELECT id,content FROM project_files WHERE project_id=?",
                (project_id,),
            ).fetchall()
        }
    target = dict(target_row)
    target.update(function_source_metadata(target))
    for internal_key in ("content", "language", "start_byte", "end_byte"):
        target.pop(internal_key)
    callers: list[dict[str, object]] = []
    for row in call_rows:
        value = dict(row)
        content = caller_source_by_file.get(int(value.pop("file_id")), b"")
        value["code"] = compact_source_excerpt(
            content,
            int(value.pop("start_byte")),
            int(value.pop("end_byte")),
        )
        callers.append(value)
    return {
        "project": {"id": project["id"], "name": project["name"]},
        "function": target,
        "caller_count": len(callers),
        "callers": callers,
    }


@app.delete("/api/projects/{project_id}/entries")
def delete_project_entry(project_id: str, payload: ProjectEntryDelete, request: Request) -> dict[str, object]:
    user = require_user(request)
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        owned_project(db, project_id, int(user["id"]), editing=True)
        try:
            file_ids = entry_file_ids(db, project_id, payload.kind, payload.file_id, payload.batch_id, payload.path)
        except ProjectUploadError as exc:
            raise HTTPException(422, str(exc)) from exc
        delete_project_function_analysis_cache(db, project_id=project_id, user_id=int(user["id"]))
        db.executemany("DELETE FROM project_files WHERE project_id = ? AND id = ?", [(project_id, file_id) for file_id in file_ids])
        db.execute("DELETE FROM project_upload_batches WHERE project_id = ? AND NOT EXISTS(SELECT 1 FROM project_files WHERE upload_batch_id = project_upload_batches.id)", (project_id,))
        rebuild_project(db, project_id)
        project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return {"project": project_response(project), "deleted_file_count": len(file_ids)}


@app.put("/api/projects/{project_id}/main-file")
def set_project_main_file(project_id: str, payload: ProjectMainFile, request: Request) -> dict[str, object]:
    user = require_user(request)
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        owned_project(db, project_id, int(user["id"]), editing=True)
        path = None
        if payload.file_id is not None:
            file = db.execute("SELECT path, is_binary, analysis_eligible FROM project_files WHERE id = ? AND project_id = ?", (payload.file_id, project_id)).fetchone()
            if file is None:
                raise HTTPException(404, "Project file not found")
            if file["is_binary"] or not file["analysis_eligible"]:
                raise HTTPException(422, "Select a source file as the main file")
            path = file["path"]
        db.execute("UPDATE projects SET main_file_path = ? WHERE id = ?", (path, project_id))
        rebuild_project(db, project_id)
        project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return {"project": project_response(project)}


@app.get("/api/projects/{project_id}")
def get_project_inventory(project_id: str, request: Request) -> dict[str, object]:
    """Return deterministic file metadata without exposing stored file contents."""
    user = require_user(request)
    with connect_db() as db:
        project = db.execute(
            "SELECT * FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user["id"]),
        ).fetchone()
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        files = db.execute(
            """
            SELECT path, size_bytes, sha256, is_binary, file_kind, language,
                   language_confidence, detection_method, encoding, line_count,
                   is_generated, is_entrypoint_candidate, analysis_eligible,
                   parser_adapter, parser_status, grammar_name, grammar_version,
                   parser_source_sha256, parser_error_count, parser_missing_count,
                   parser_diagnostics_json, parser_diagnostics_truncated, parser_error,
                   parser_updated_at, structure_status, definition_count,
                   dependency_count, call_count, structure_error, structure_updated_at
            FROM project_files
            WHERE project_id = ?
            ORDER BY path COLLATE NOCASE, path
            """,
            (project_id,),
        ).fetchall()
    result = project_response(project)
    file_results: list[dict[str, object]] = []
    for row in files:
        file_result = dict(row)
        try:
            diagnostics = json.loads(str(file_result.pop("parser_diagnostics_json") or "[]"))
        except json.JSONDecodeError:
            diagnostics = []
        file_result["parser_diagnostics"] = diagnostics if isinstance(diagnostics, list) else []
        file_results.append(file_result)
    result["files"] = file_results
    result["adapters"] = [status.as_dict() for status in adapter_statuses()]
    return result


@app.get("/api/projects/{project_id}/compatibility")
def get_project_compatibility(project_id: str, request: Request) -> dict[str, object]:
    """Return call-level compatibility outcomes without exposing uploaded source."""
    user = require_user(request)
    with connect_db() as db:
        project = db.execute(
            "SELECT * FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user["id"]),
        ).fetchone()
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        rows = db.execute(
            """
            SELECT compatibility.call_id, compatibility.status,
                   compatibility.argument_status, compatibility.return_status,
                   compatibility.scope_status,
                   call.callee, call.usage_kind, call.start_line, call.end_line,
                   file.path, caller.qualified_name AS caller_name,
                   callee.qualified_name AS resolved_callee_name
            FROM project_call_compatibility AS compatibility
            JOIN project_calls AS call ON call.id = compatibility.call_id
            JOIN project_files AS file ON file.id = call.file_id
            LEFT JOIN project_symbols AS caller ON caller.id = compatibility.caller_symbol_id
            LEFT JOIN project_symbols AS callee ON callee.id = compatibility.callee_symbol_id
            WHERE compatibility.project_id = ?
            ORDER BY file.path COLLATE NOCASE, call.start_byte, call.id
            """,
            (project_id,),
        ).fetchall()
        findings = db.execute(
            """
            SELECT finding.call_id, finding.severity, finding.finding_kind,
                   finding.message, finding.argument_ordinal,
                   finding.expected_types_json, finding.actual_types_json
            FROM project_call_findings AS finding
            JOIN project_call_compatibility AS compatibility
              ON compatibility.call_id = finding.call_id
            WHERE compatibility.project_id = ?
            ORDER BY finding.call_id, finding.ordinal
            """,
            (project_id,),
        ).fetchall()
    findings_by_call: dict[int, list[dict[str, object]]] = {}
    for finding in findings:
        value = dict(finding)
        call_id = int(value.pop("call_id"))
        for key in ("expected_types_json", "actual_types_json"):
            output_key = key.removesuffix("_json")
            try:
                decoded = json.loads(str(value.pop(key) or "[]"))
            except json.JSONDecodeError:
                decoded = []
            value[output_key] = decoded if isinstance(decoded, list) else []
        findings_by_call.setdefault(call_id, []).append(value)
    call_results = []
    for row in rows:
        value = dict(row)
        if value.pop("scope_status") == "out_of_scope":
            value["status"] = "not_checked"
            value["argument_status"] = "not_checked"
            value["return_status"] = "not_checked"
        value["findings"] = findings_by_call.get(int(value["call_id"]), [])
        call_results.append(value)
    return {"project": project_response(project), "calls": call_results}


PROJECT_REPORT_PAGE_SIZE = 200
SOURCE_INTEGRITY_WORKSPACE_ROOT = Path.cwd()


def _safe_workspace_candidate(path: Path) -> Path | None:
    try:
        root = SOURCE_INTEGRITY_WORKSPACE_ROOT.resolve()
        resolved = path.resolve()
    except OSError:
        return None
    if resolved == root or root not in resolved.parents:
        return None
    return resolved


def _workspace_file_candidates(upload_path: str, *, project_file_count: int) -> list[Path]:
    normalized = upload_path.replace("\\", "/")
    parts = [
        part
        for part in normalized.split("/")
        if part and part not in {".", ".."}
    ]
    if not parts:
        return []
    candidates: list[Path] = []
    exact = _safe_workspace_candidate(SOURCE_INTEGRITY_WORKSPACE_ROOT.joinpath(*parts))
    if exact is not None:
        candidates.append(exact)
    if project_file_count == 1:
        basename = _safe_workspace_candidate(SOURCE_INTEGRITY_WORKSPACE_ROOT / parts[-1])
        if basename is not None and basename not in candidates:
            candidates.append(basename)
    return candidates


def build_project_source_integrity_report(
    db: sqlite3.Connection,
    project_id: str,
) -> dict[str, object]:
    """Compare stored uploaded source hashes with matching workspace files, when found."""
    rows = db.execute(
        """
        SELECT path, size_bytes, sha256, is_binary
        FROM project_files
        WHERE project_id = ?
        ORDER BY path COLLATE NOCASE, path
        """,
        (project_id,),
    ).fetchall()
    project_file_count = len(rows)
    files: list[dict[str, object]] = []
    warning_count = 0
    matched_count = 0
    checked_count = 0
    for row in rows:
        path = str(row["path"])
        stored_sha256 = str(row["sha256"])
        entry: dict[str, object] = {
            "path": path,
            "stored_sha256": stored_sha256,
            "stored_size_bytes": int(row["size_bytes"] or 0),
            "workspace_status": "not_found",
        }
        for candidate in _workspace_file_candidates(
            path,
            project_file_count=project_file_count,
        ):
            if not candidate.is_file():
                continue
            try:
                content = candidate.read_bytes()
            except OSError as exc:
                entry.update(
                    {
                        "workspace_status": "unreadable",
                        "workspace_path": str(candidate.relative_to(SOURCE_INTEGRITY_WORKSPACE_ROOT.resolve())),
                        "warning": f"Workspace file could not be read: {type(exc).__name__}",
                    }
                )
                warning_count += 1
                break
            checked_count += 1
            workspace_sha256 = hashlib.sha256(content).hexdigest()
            matched = workspace_sha256 == stored_sha256
            if matched:
                matched_count += 1
            else:
                warning_count += 1
            entry.update(
                {
                    "workspace_status": "matched" if matched else "changed",
                    "workspace_path": str(candidate.relative_to(SOURCE_INTEGRITY_WORKSPACE_ROOT.resolve())),
                    "workspace_sha256": workspace_sha256,
                    "workspace_size_bytes": len(content),
                }
            )
            if not matched:
                entry["warning"] = (
                    "Stored analysis source differs from the matching workspace file. "
                    "Re-upload or reset/re-run analysis before comparing results."
                )
            break
        files.append(entry)
    if warning_count:
        status = "warning"
    elif checked_count and checked_count == matched_count:
        status = "matched"
    else:
        status = "unknown"
    return {
        "status": status,
        "checked_file_count": checked_count,
        "matched_file_count": matched_count,
        "warning_count": warning_count,
        "files": files,
    }


@app.get("/api/projects/{project_id}/analysis-report")
def get_project_analysis_report(
    project_id: str,
    request: Request,
    function_offset: Annotated[int, Query(ge=0)] = 0,
    call_offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    """Return a bounded source-free report of stored function and call analyses."""
    user = require_user(request)
    with connect_db() as db:
        project = db.execute(
            "SELECT * FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user["id"]),
        ).fetchone()
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        source_integrity = build_project_source_integrity_report(db, project_id)
        review_quality = project_review_quality(db, project_id)
        function_total = int(
            db.execute(
                """
                SELECT COUNT(*) FROM project_symbols
                WHERE project_id = ? AND symbol_kind IN ('function', 'method')
                """,
                (project_id,),
            ).fetchone()[0]
        )
        call_total = int(
            db.execute(
                "SELECT COUNT(*) FROM project_call_compatibility WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
        )
        function_rows = db.execute(
            """
            SELECT symbol.id, symbol.symbol_kind, symbol.qualified_name,
                   symbol.start_line, symbol.end_line, symbol.analysis_status,
                   symbol.analysis_error, symbol.analyzed_at, file.path,
                   analysis.summary, analysis.syntax_valid,
                   analysis.may_return_value, analysis.return_nullable,
                   analysis.return_description, analysis.raised_errors_json,
                   analysis.side_effects_json, analysis.confidence, analysis.response_json, budget.budget_json
            FROM project_symbols AS symbol
            JOIN project_files AS file ON file.id = symbol.file_id
            LEFT JOIN project_symbol_analyses AS analysis ON analysis.symbol_id = symbol.id
            LEFT JOIN function_analysis_budgets AS budget ON budget.symbol_id = symbol.id
            WHERE symbol.project_id = ? AND symbol.symbol_kind IN ('function', 'method')
            ORDER BY file.path COLLATE NOCASE, file.path, symbol.start_byte, symbol.id
            LIMIT ? OFFSET ?
            """,
            (project_id, PROJECT_REPORT_PAGE_SIZE, function_offset),
        ).fetchall()
        function_ids = [int(row["id"]) for row in function_rows]
        parameters_by_symbol: dict[int, list[dict[str, object]]] = {}
        returns_by_symbol: dict[int, list[dict[str, object]]] = {}
        issues_by_symbol: dict[int, list[dict[str, object]]] = {}
        advisories_by_symbol: dict[int, list[dict[str, object]]] = {}
        if function_ids:
            placeholders = ",".join("?" for _ in function_ids)
            for row in db.execute(
                f"""
                SELECT symbol_id, ordinal, name, parameter_kind, required,
                       accepted_types_json, default_description, description
                FROM project_symbol_parameters
                WHERE symbol_id IN ({placeholders}) ORDER BY symbol_id, ordinal
                """,
                function_ids,
            ).fetchall():
                value = dict(row)
                symbol_id = int(value.pop("symbol_id"))
                try:
                    accepted_types = json.loads(str(value.pop("accepted_types_json") or "[]"))
                except json.JSONDecodeError:
                    accepted_types = []
                value["accepted_types"] = accepted_types if isinstance(accepted_types, list) else []
                parameters_by_symbol.setdefault(symbol_id, []).append(value)
            for row in db.execute(
                f"""
                SELECT symbol_id, ordinal, type_name, description
                FROM project_symbol_return_types
                WHERE symbol_id IN ({placeholders}) ORDER BY symbol_id, ordinal
                """,
                function_ids,
            ).fetchall():
                value = dict(row)
                symbol_id = int(value.pop("symbol_id"))
                returns_by_symbol.setdefault(symbol_id, []).append(value)
            for row in db.execute(
                f"""
                SELECT symbol_id, ordinal, severity, category, title,
                       description, start_line, end_line, provenance,
                       proof, evidence, failure_type, trigger, report_tier
                FROM project_symbol_issues
                WHERE symbol_id IN ({placeholders}) ORDER BY symbol_id, ordinal
                """,
                function_ids,
            ).fetchall():
                value = dict(row)
                symbol_id = int(value.pop("symbol_id"))
                if value.get("report_tier") == "advisory":
                    advisories_by_symbol.setdefault(symbol_id, []).append(value)
                else:
                    issues_by_symbol.setdefault(symbol_id, []).append(value)

        functions: list[dict[str, object]] = []
        for row in function_rows:
            value = dict(row)
            value["review_quality"] = response_quality(value.pop("response_json"), value["analysis_status"])
            value["output_budget"] = json.loads(value.pop("budget_json") or "null")
            symbol_id = int(value["id"])
            for json_key in ("raised_errors_json", "side_effects_json"):
                output_key = json_key.removesuffix("_json")
                try:
                    decoded = json.loads(str(value.pop(json_key) or "[]"))
                except json.JSONDecodeError:
                    decoded = []
                value[output_key] = (
                    [str(item) for item in decoded]
                    if isinstance(decoded, list)
                    else []
                )
            value["parameters"] = parameters_by_symbol.get(symbol_id, [])
            value["return_types"] = returns_by_symbol.get(symbol_id, [])
            value["issues"] = issues_by_symbol.get(symbol_id, [])
            value["advisories"] = advisories_by_symbol.get(symbol_id, [])
            functions.append(value)

        call_rows = db.execute(
            """
            SELECT compatibility.call_id, compatibility.status,
                   compatibility.argument_status, compatibility.return_status,
                   compatibility.scope_status,
                   compatibility.checked_at, call.callee, call.usage_kind,
                   call.start_line, call.end_line, file.path,
                   caller.qualified_name AS caller_name,
                   callee.qualified_name AS resolved_callee_name
            FROM project_call_compatibility AS compatibility
            JOIN project_calls AS call ON call.id = compatibility.call_id
            JOIN project_files AS file ON file.id = call.file_id
            LEFT JOIN project_symbols AS caller ON caller.id = compatibility.caller_symbol_id
            LEFT JOIN project_symbols AS callee ON callee.id = compatibility.callee_symbol_id
            WHERE compatibility.project_id = ?
            ORDER BY file.path COLLATE NOCASE, file.path, call.start_byte, call.id
            LIMIT ? OFFSET ?
            """,
            (project_id, PROJECT_REPORT_PAGE_SIZE, call_offset),
        ).fetchall()
        call_ids = [int(row["call_id"]) for row in call_rows]
        findings_by_call: dict[int, list[dict[str, object]]] = {}
        if call_ids:
            placeholders = ",".join("?" for _ in call_ids)
            for row in db.execute(
                f"""
                SELECT call_id, ordinal, severity, finding_kind, message,
                       argument_ordinal, expected_types_json, actual_types_json
                FROM project_call_findings
                WHERE call_id IN ({placeholders}) ORDER BY call_id, ordinal
                """,
                call_ids,
            ).fetchall():
                value = dict(row)
                call_id = int(value.pop("call_id"))
                for key in ("expected_types_json", "actual_types_json"):
                    output_key = key.removesuffix("_json")
                    try:
                        decoded = json.loads(str(value.pop(key) or "[]"))
                    except json.JSONDecodeError:
                        decoded = []
                    value[output_key] = decoded if isinstance(decoded, list) else []
                findings_by_call.setdefault(call_id, []).append(value)
        calls: list[dict[str, object]] = []
        for row in call_rows:
            value = dict(row)
            if value.pop("scope_status") == "out_of_scope":
                value["status"] = "not_checked"
                value["argument_status"] = "not_checked"
                value["return_status"] = "not_checked"
            value["findings"] = findings_by_call.get(int(value["call_id"]), [])
            calls.append(value)

    next_function_offset = function_offset + len(functions)
    next_call_offset = call_offset + len(calls)
    return {
        "project": project_response(project),
        "functions": functions,
        "calls": calls,
        "source_integrity": source_integrity,
        "review_quality": review_quality,
        "function_total": function_total,
        "call_total": call_total,
        "next_function_offset": (
            next_function_offset if next_function_offset < function_total else None
        ),
        "next_call_offset": next_call_offset if next_call_offset < call_total else None,
    }


def _queue_project_analysis_job(
    project_id: str,
    payload: ProjectAnalysisRequest,
    request: Request,
    *,
    project_symbol_id: int | None = None,
) -> JSONResponse:
    user = require_user(request)
    require_ai_work_enabled()
    job_id = str(uuid.uuid4())
    statuses = ["pending", "processing"]
    if payload.retry_failed:
        statuses.extend(("failed", "stale", "skipped"))
    placeholders = ",".join("?" for _ in statuses)
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        require_ai_work_enabled(db)
        project = db.execute(
            "SELECT * FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user["id"]),
        ).fetchone()
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        if project["structure_status"] not in {"completed", "partial"}:
            raise HTTPException(
                status_code=409,
                detail="The project structure is not ready for function analysis",
            )
        if int(project["function_analysis_total_count"] or 0) == 0:
            raise HTTPException(
                status_code=409,
                detail="No indexed functions are available for analysis",
            )
        if project_symbol_id is None:
            target = db.execute(
                f"""
                SELECT COUNT(*) AS symbol_count,
                       COUNT(DISTINCT file_id) AS file_count,
                       COALESCE(SUM(end_byte - start_byte), 0) AS input_char_count
                FROM project_symbols
                WHERE project_id = ? AND symbol_kind IN ('function', 'method')
                  AND analysis_status IN ({placeholders})
                """,
                (project_id, *statuses),
            ).fetchone()
        else:
            symbol = db.execute(
                """SELECT s.id, s.end_byte - s.start_byte AS input_char_count,
                          s.analysis_status, a.summary, a.response_json
                   FROM project_symbols s
                   LEFT JOIN project_symbol_analyses a ON a.symbol_id=s.id
                   WHERE s.id=? AND s.project_id=?
                     AND s.symbol_kind IN ('function','method')""",
                (project_symbol_id, project_id),
            ).fetchone()
            if symbol is None:
                raise HTTPException(status_code=404, detail="Project function not found")
            quality = response_quality(
                symbol["response_json"], str(symbol["analysis_status"])
            )
            if (
                quality["method"] == "model"
                and quality["status"] == "complete"
                and str(symbol["summary"] or "").strip()
            ):
                raise HTTPException(
                    status_code=409,
                    detail="This function already has an LLM description",
                )
            target = {
                "symbol_count": 1,
                "file_count": 1,
                "input_char_count": int(symbol["input_char_count"] or 0),
            }
        symbol_count = int(target["symbol_count"])
        if symbol_count == 0:
            detail = (
                "No failed functions are available to retry"
                if payload.retry_failed
                else "All indexed functions have already been analysed"
            )
            raise HTTPException(status_code=409, detail=detail)
        input_char_count = int(target["input_char_count"])
        enforce_job_admission(db, int(user["id"]), input_char_count)
        insert_chat_job(
            db,
            """
            INSERT INTO chat_jobs(
                id, user_id, chat_id, project_id, project_retry_failed,
                project_symbol_id,
                input_char_count, mode, job_kind, status, progress_stage,
                progress_total, progress_file_total, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'project', 'project_analysis',
                      'queued', 'queued', ?, ?, ?)
            """,
            (
                job_id,
                user["id"],
                project["chat_id"],
                project_id,
                int(payload.retry_failed or project_symbol_id is not None),
                project_symbol_id,
                input_char_count,
                symbol_count,
                int(target["file_count"]),
                int(time.time()),
            ),
            int(user["id"]),
            str(project["chat_id"]),
        )
        db.execute(
            """
            UPDATE projects
            SET function_analysis_status = 'running', function_analysis_error = NULL,
                function_analysis_updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (project_id,),
        )
        record_chat_job_progress(db, job_id, "queued", update_stage=False)
        project = db.execute(
            "SELECT * FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
    with JOB_CANCEL_LOCK:
        JOB_CANCEL_EVENTS[job_id] = threading.Event()
        JOB_PAUSE_EVENTS[job_id] = threading.Event()
    enqueue_ollama_job(job_id)
    return JSONResponse(
        status_code=202,
        content={
            "status": "queued",
            "job_id": job_id,
            "job_kind": "project_analysis",
            "chat_id": project["chat_id"],
            "project_id": project_id,
            "project_name": project["name"],
            "project_symbol_id": project_symbol_id,
            "description_only": project_symbol_id is not None,
            "progress_stage": "queued",
            "progress_current": 0,
            "progress_total": symbol_count,
            "progress_file_current": 0,
            "progress_file_total": int(target["file_count"]),
            "progress_function_current": 0,
            "progress_function_total": 0,
            "project": project_response(project),
        },
    )


@app.post("/api/projects/{project_id}/analysis-jobs", response_model=None)
def start_project_analysis_job(
    project_id: str,
    payload: ProjectAnalysisRequest,
    request: Request,
) -> JSONResponse:
    return _queue_project_analysis_job(project_id, payload, request)


@app.post(
    "/api/projects/{project_id}/functions/{symbol_id}/description-jobs",
    response_model=None,
)
def start_project_function_description_job(
    project_id: str,
    symbol_id: int,
    request: Request,
) -> JSONResponse:
    """Queue one forced model review to populate a function-tree description."""
    return _queue_project_analysis_job(
        project_id,
        ProjectAnalysisRequest(retry_failed=True),
        request,
        project_symbol_id=symbol_id,
    )


@app.delete("/api/projects/{project_id}/analysis-cache")
def clear_project_analysis_cache(project_id: str, request: Request) -> dict[str, object]:
    """Reset this project's stored analysis state while preserving reusable cache rows."""
    user = require_user(request)
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        project = db.execute(
            "SELECT * FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user["id"]),
        ).fetchone()
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        active = db.execute(
            """
            SELECT 1 FROM chat_jobs
            WHERE project_id = ? AND status IN ('queued', 'processing')
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if active is not None:
            raise HTTPException(
                status_code=409,
                detail="Stop the project analysis before resetting stored analysis",
            )
        cache_deleted = 0
        stale_key_count = 0
        db.execute(
            """
            DELETE FROM project_symbol_issues
            WHERE symbol_id IN (
                SELECT id FROM project_symbols
                WHERE project_id = ? AND symbol_kind IN ('function', 'method')
            )
            """,
            (project_id,),
        )
        db.execute(
            """
            DELETE FROM project_symbol_return_types
            WHERE symbol_id IN (
                SELECT id FROM project_symbols
                WHERE project_id = ? AND symbol_kind IN ('function', 'method')
            )
            """,
            (project_id,),
        )
        db.execute(
            """
            DELETE FROM project_symbol_parameters
            WHERE symbol_id IN (
                SELECT id FROM project_symbols
                WHERE project_id = ? AND symbol_kind IN ('function', 'method')
            )
            """,
            (project_id,),
        )
        analyses_deleted = int(
            db.execute(
                "DELETE FROM project_symbol_analyses WHERE project_id = ?",
                (project_id,),
            ).rowcount
            or 0
        )
        db.execute(
            """
            DELETE FROM project_call_findings
            WHERE call_id IN (
                SELECT call_id FROM project_call_compatibility WHERE project_id = ?
            )
            """,
            (project_id,),
        )
        db.execute(
            "DELETE FROM project_call_compatibility WHERE project_id = ?",
            (project_id,),
        )
        db.execute(
            """
            UPDATE project_symbols
            SET analysis_status = 'pending', analysis_error = NULL, analyzed_at = NULL
            WHERE project_id = ? AND symbol_kind IN ('function', 'method')
            """,
            (project_id,),
        )
        db.execute(
            """
            UPDATE projects
            SET function_analysis_status = CASE
                    WHEN function_analysis_total_count > 0 THEN 'pending'
                    ELSE 'completed'
                END,
                function_analysis_completed_count = 0,
                function_analysis_failed_count = 0,
                function_analysis_skipped_count = 0,
                function_analysis_cache_hit_count = 0,
                function_analysis_model_request_count = 0,
                function_analysis_batch_request_count = 0,
                function_analysis_deterministic_count = 0,
                function_analysis_batch_fallback_count = 0,
                function_analysis_batch_error = NULL,
                function_analysis_error = NULL,
                function_analysis_updated_at = CURRENT_TIMESTAMP,
                call_compatibility_status = 'pending',
                call_compatibility_checked_count = 0,
                call_compatibility_incompatible_count = 0,
                call_compatibility_unknown_count = 0,
                call_compatibility_not_checked_count = 0,
                call_compatibility_error = NULL,
                call_compatibility_updated_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (project_id,),
        )
        refreshed = db.execute(
            "SELECT * FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
    return {
        "message": (
            f"Reset {analyses_deleted} stored analysis result"
            f"{'' if analyses_deleted == 1 else 's'}. Reusable cache was preserved."
        ),
        "cache_deleted": cache_deleted,
        "cache_preserved": True,
        "analysis_deleted": analyses_deleted,
        "stale_cache_key_count": stale_key_count,
        "project": project_response(refreshed),
    }


@app.delete("/api/projects/{project_id}/function-analysis-cache")
def purge_project_function_analysis_cache(project_id: str, request: Request) -> dict[str, object]:
    """Delete reusable function-analysis cache rows for this project's current functions."""
    user = require_user(request)
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        project = db.execute(
            "SELECT * FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user["id"]),
        ).fetchone()
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        active = db.execute(
            """
            SELECT 1 FROM chat_jobs
            WHERE project_id = ? AND status IN ('queued', 'processing')
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if active is not None:
            raise HTTPException(
                status_code=409,
                detail="Stop the project analysis before purging reusable cache",
            )
        cache_deleted, stale_key_count = delete_project_function_analysis_cache(
            db,
            project_id=project_id,
            user_id=int(user["id"]),
        )
        refreshed = db.execute(
            "SELECT * FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
    return {
        "message": (
            f"Purged {cache_deleted} reusable cached function result"
            f"{'' if cache_deleted == 1 else 's'}."
        ),
        "cache_deleted": cache_deleted,
        "analysis_preserved": True,
        "stale_cache_key_count": stale_key_count,
        "project": project_response(refreshed),
    }


@app.patch("/api/projects/{project_id}")
def rename_project(
    project_id: str,
    payload: RenameProjectRequest,
    request: Request,
) -> dict[str, object]:
    user = require_user(request)
    name = safe_project_name(payload.name, "")
    if not name:
        raise HTTPException(status_code=422, detail="Project name cannot be empty")
    with connect_db() as db:
        cursor = db.execute(
            """
            UPDATE projects SET name = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND user_id = ?
            """,
            (name, project_id, user["id"]),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Project not found")
        project = db.execute(
            "SELECT * FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user["id"]),
        ).fetchone()
    return {"project": project_response(project)}


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str, request: Request) -> dict[str, str]:
    user = require_user(request)
    with connect_db() as db:
        project = db.execute(
            "SELECT name FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user["id"]),
        ).fetchone()
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        active = db.execute(
            """
            SELECT 1 FROM chat_jobs
            WHERE project_id = ? AND status IN ('queued', 'processing')
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if active is not None:
            raise HTTPException(
                status_code=409,
                detail="Stop the project analysis before deleting this project",
            )
        cache_deleted, _stale_key_count = delete_project_function_analysis_cache(
            db,
            project_id=project_id,
            user_id=int(user["id"]),
        )
        job_rows = db.execute(
            "SELECT id FROM chat_jobs WHERE project_id = ? AND user_id = ?",
            (project_id, user["id"]),
        ).fetchall()
        project_job_ids = [str(row["id"]) for row in job_rows]
        db.execute(
            "DELETE FROM chat_jobs WHERE project_id = ? AND user_id = ?",
            (project_id, user["id"]),
        )
        db.execute(
            "DELETE FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user["id"]),
        )
    if project_job_ids:
        with JOB_CANCEL_LOCK:
            for job_id in project_job_ids:
                JOB_CANCEL_EVENTS.pop(job_id, None)
                JOB_PAUSE_EVENTS.pop(job_id, None)
    return {
        "message": (
            f"Cleared {project['name']} and {cache_deleted} cached function result"
            f"{'' if cache_deleted == 1 else 's'}"
        )
    }


@app.get("/api/chats/{chat_id}")
def load_chat(
    chat_id: str,
    request: Request,
    before: Annotated[int | None, Query(gt=0)] = None,
) -> dict[str, object]:
    user = require_user(request)
    with connect_db() as db:
        chat_row = db.execute(
            "SELECT id, title FROM chat_histories WHERE id = ? AND user_id = ?",
            (chat_id, user["id"]),
        ).fetchone()
        if chat_row is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        memory_id = f"user-{user['id']}:{chat_id}"
        fetched_rows = db.execute(
            """
            SELECT id, role, content FROM messages
            WHERE session_id = ? AND (? IS NULL OR id < ?)
            ORDER BY id DESC LIMIT ?
            """,
            (memory_id, before, before, CHAT_HISTORY_PAGE_SIZE + 1),
        ).fetchall()
        has_older_messages = len(fetched_rows) > CHAT_HISTORY_PAGE_SIZE
        rows = list(reversed(fetched_rows[:CHAT_HISTORY_PAGE_SIZE]))
        active_job = db.execute(
            """
            SELECT id AS job_id, status, progress_stage, progress_current, progress_total,
                   job_kind, mode, project_id, project_symbol_id,
                   started_at, paused_at, paused_seconds,
                   progress_log,
                   progress_file_path, progress_symbol_name,
                   progress_file_current, progress_file_total,
                   progress_function_current, progress_function_total
            FROM chat_jobs
            WHERE chat_id = ? AND user_id = ? AND status IN ('queued', 'processing')
            ORDER BY created_at DESC LIMIT 1
            """,
            (chat_id, user["id"]),
        ).fetchone()
        reusable_analysis = db.execute(
            """
            SELECT id FROM messages
            WHERE session_id = ? AND role = 'user'
              AND context_content LIKE '[Oversized input analyzed in chunks]%'
            ORDER BY id DESC LIMIT 1
            """,
            (memory_id,),
        ).fetchone()
        project_rows = db.execute(
            """
            SELECT id, chat_id, name, source_kind, status, file_count,
                   skipped_file_count, total_bytes, created_at,
                   primary_language, languages_json, inventory_status,
                   parser_status, parser_supported_file_count, parser_parsed_file_count,
                   parser_syntax_error_file_count, parser_failed_file_count,
                   structure_status, indexed_file_count, definition_count,
                   dependency_count, call_count, resolved_dependency_count,
                   ambiguous_dependency_count, function_analysis_status,
                   function_analysis_total_count, function_analysis_completed_count,
                   function_analysis_failed_count, function_analysis_skipped_count,
                   function_analysis_cache_hit_count,
                   function_analysis_model_request_count,
                   function_analysis_batch_request_count,
                   function_analysis_deterministic_count,
                   function_analysis_batch_fallback_count,
                   function_analysis_batch_error,
                   call_compatibility_status, call_compatibility_checked_count,
                   call_compatibility_incompatible_count,
                   call_compatibility_unknown_count,
                   call_compatibility_not_checked_count
            FROM projects
            WHERE chat_id = ? AND user_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 20
            """,
            (chat_id, user["id"]),
        ).fetchall()
        reply_message_ids = [row["id"] for row in rows if row["role"] == "assistant"]
        completed_job_logs: list[sqlite3.Row] = []
        if reply_message_ids:
            placeholders = ",".join("?" for _ in reply_message_ids)
            completed_job_logs = db.execute(
                f"""
                SELECT reply_message_id, progress_log
                FROM chat_jobs
                WHERE chat_id = ? AND user_id = ?
                  AND reply_message_id IN ({placeholders})
                  AND progress_log != '[]'
                ORDER BY updated_at DESC, created_at DESC
                """,
                (chat_id, user["id"], *reply_message_ids),
            ).fetchall()
    logs_by_reply: dict[int, list[dict[str, object]]] = {}
    for job_log in completed_job_logs:
        reply_id = int(job_log["reply_message_id"])
        logs_by_reply.setdefault(reply_id, decode_progress_log(job_log["progress_log"]))
    messages: list[dict[str, object]] = []
    for row in rows:
        if row["role"] == "assistant" and row["id"] in logs_by_reply:
            messages.append({"role": "progress", "events": logs_by_reply[row["id"]]})
        content = row["content"]
        if row["role"] == "assistant":
            content = final_response_json_to_markdown(content)
        messages.append({"role": row["role"], "content": content})
    projects = [project_response(row) for row in project_rows]
    active_job_data = dict(active_job) if active_job is not None else None
    if active_job_data is not None:
        active_job_data["progress_log"] = decode_progress_log(active_job_data["progress_log"])
        active_job_data["elapsed_seconds"] = active_job_elapsed_seconds(
            active_job_data.get("started_at"),
            active_job_data.get("paused_at"),
            active_job_data.get("paused_seconds"),
        )
        if active_job_data.get("job_kind") == "project_analysis":
            active_job_data["project"] = next(
                (
                    project
                    for project in projects
                    if project["id"] == active_job_data.get("project_id")
                ),
                None,
            )
    return {
        "id": chat_row["id"],
        "title": chat_row["title"],
        "messages": messages,
        "next_before": int(rows[0]["id"]) if has_older_messages and rows else None,
        "active_job": active_job_data,
        "can_retry_verification": reusable_analysis is not None and active_job is None,
        "projects": projects,
    }


@app.patch("/api/chats/{chat_id}")
def rename_chat(chat_id: str, payload: RenameChatRequest, request: Request) -> dict[str, str]:
    user = require_user(request)
    title = re.sub(r"\s+", " ", payload.title).strip()
    if not title:
        raise HTTPException(status_code=422, detail="Chat name cannot be empty")
    with connect_db() as db:
        cursor = db.execute(
            """
            UPDATE chat_histories SET title = ?, title_is_custom = 1
            WHERE id = ? AND user_id = ?
            """,
            (title, chat_id, user["id"]),
        )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"id": chat_id, "title": title}


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str, request: Request) -> dict[str, str]:
    user = require_user(request)
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        owned = db.execute(
            "SELECT title FROM chat_histories WHERE id = ? AND user_id = ?",
            (chat_id, user["id"]),
        ).fetchone()
        if owned is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        active = db.execute(
            """
            SELECT 1 FROM chat_jobs
            WHERE chat_id = ? AND user_id = ? AND status IN ('queued', 'processing')
            LIMIT 1
            """,
            (chat_id, user["id"]),
        ).fetchone()
        if active is not None:
            raise HTTPException(
                status_code=409,
                detail="Stop the active request before deleting this chat",
            )
        memory_id = f"user-{user['id']}:{chat_id}"
        db.execute(
            "DELETE FROM chat_jobs WHERE chat_id = ? AND user_id = ?",
            (chat_id, user["id"]),
        )
        db.execute("DELETE FROM messages WHERE session_id = ?", (memory_id,))
        db.execute("DELETE FROM chat_histories WHERE id = ? AND user_id = ?", (chat_id, user["id"]))
    return {"message": f"Deleted {owned['title']}"}


def enforce_job_admission(
    db: sqlite3.Connection,
    user_id: int,
    input_characters: int,
    *,
    storage_bytes_to_add: int = 0,
) -> None:
    """Apply global and per-user limits inside the caller's write transaction."""
    global_active = db.execute(
        "SELECT COUNT(*) FROM chat_jobs WHERE status IN ('queued', 'processing')"
    ).fetchone()[0]
    if global_active >= JOB_QUEUE_CAPACITY:
        raise HTTPException(
            status_code=503,
            detail="The Ollama request queue is full. Try again after another request finishes.",
            headers={"Retry-After": "10"},
        )
    limits = db.execute(
        """
        SELECT storage_limit_bytes, active_job_limit, pending_input_char_limit
        FROM users WHERE id = ?
        """,
        (user_id,),
    ).fetchone()
    if limits is None:
        raise HTTPException(status_code=401, detail="Please log in")
    active_job_limit = (
        int(limits["active_job_limit"])
        if limits["active_job_limit"] is not None
        else MAX_ACTIVE_JOBS_PER_USER
    )
    pending_input_limit = (
        int(limits["pending_input_char_limit"])
        if limits["pending_input_char_limit"] is not None
        else MAX_PENDING_INPUT_CHARS_PER_USER
    )
    user_usage = db.execute(
        """
        SELECT COUNT(*) AS active_jobs,
               COALESCE(SUM(
                   CASE WHEN input_char_count > 0
                        THEN input_char_count
                        ELSE length(COALESCE(message, '')) END
               ), 0) AS input_characters
        FROM chat_jobs
        WHERE user_id = ? AND status IN ('queued', 'processing')
        """,
        (user_id,),
    ).fetchone()
    if user_usage["active_jobs"] >= active_job_limit:
        raise HTTPException(
            status_code=429,
            detail=(
                "You already have the maximum number of requests in progress. "
                "Wait for one to finish or cancel it before submitting another."
            ),
            headers={"Retry-After": "10"},
        )
    if user_usage["input_characters"] + input_characters > pending_input_limit:
        raise HTTPException(
            status_code=429,
            detail=(
                "Your queued requests exceed the pending input allowance. "
                "Wait for an existing request to finish or cancel it before submitting more."
            ),
            headers={"Retry-After": "10"},
        )
    if limits["storage_limit_bytes"] is not None and storage_bytes_to_add:
        enforce_project_storage_limit(db, user_id, storage_bytes_to_add)


def insert_chat_job(
    db: sqlite3.Connection,
    statement: str,
    parameters: tuple[object, ...],
    user_id: int,
    chat_id: str,
) -> sqlite3.Cursor:
    """Insert a job while translating the active-chat invariant into an API conflict."""
    try:
        return db.execute(statement, parameters)
    except sqlite3.IntegrityError as exc:
        active = db.execute(
            """
            SELECT 1 FROM chat_jobs
            WHERE user_id = ? AND chat_id = ?
              AND status IN ('queued', 'processing')
            LIMIT 1
            """,
            (user_id, chat_id),
        ).fetchone()
        if active is not None:
            raise HTTPException(
                status_code=409,
                detail="This chat already has a request in progress",
            ) from exc
        raise


def enqueue_ollama_job(job_id: str) -> None:
    try:
        OLLAMA_JOB_QUEUE.put_nowait(job_id)
    except queue.Full as exc:
        error = "The Ollama request queue reached capacity before the job could start."
        with connect_db() as db:
            job = db.execute(
                "SELECT project_id FROM chat_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            db.execute(
                """
                UPDATE chat_jobs SET status = 'failed', message = NULL, error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'queued'
                """,
                (error, job_id),
            )
            record_chat_job_progress(db, job_id, "failed", update_stage=False)
            if job is not None and job["project_id"] is not None:
                refresh_project_function_analysis(db, str(job["project_id"]))
        persist_failed_job_message(job_id, f"Error: {error}")
        with JOB_CANCEL_LOCK:
            JOB_CANCEL_EVENTS.pop(job_id, None)
            JOB_PAUSE_EVENTS.pop(job_id, None)
        raise HTTPException(
            status_code=503,
            detail=error,
            headers={"Retry-After": "10"},
        ) from exc


@app.post("/api/chats/{chat_id}/retry-verification", response_model=None)
def retry_chat_verification(
    chat_id: str, request: Request
) -> JSONResponse:
    user = require_user(request)
    require_ai_work_enabled()
    job_id = str(uuid.uuid4())
    memory_id = f"user-{user['id']}:{chat_id}"
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        require_ai_work_enabled(db)
        chat_row = db.execute(
            "SELECT title FROM chat_histories WHERE id = ? AND user_id = ?",
            (chat_id, user["id"]),
        ).fetchone()
        if chat_row is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        active = db.execute(
            """
            SELECT id FROM chat_jobs
            WHERE chat_id = ? AND user_id = ? AND status IN ('queued', 'processing')
            LIMIT 1
            """,
            (chat_id, user["id"]),
        ).fetchone()
        if active is not None:
            raise HTTPException(
                status_code=409,
                detail="This chat already has a request in progress",
            )
        source_message = db.execute(
            """
            SELECT id, context_content FROM messages
            WHERE session_id = ? AND role = 'user'
              AND context_content LIKE '[Oversized input analyzed in chunks]%'
            ORDER BY id DESC LIMIT 1
            """,
            (memory_id,),
        ).fetchone()
        if source_message is None or not extract_stored_analysis_notes(
            source_message["context_content"]
        ):
            raise HTTPException(
                status_code=409,
                detail="This chat has no stored analysis notes to verify again",
            )
        reply_row = db.execute(
            """
            SELECT id FROM messages
            WHERE session_id = ? AND role = 'assistant' AND id > ?
            ORDER BY id LIMIT 1
            """,
            (memory_id, source_message["id"]),
        ).fetchone()
        reply_message_id = int(reply_row["id"]) if reply_row is not None else None
        enforce_job_admission(db, int(user["id"]), 0)
        insert_chat_job(
            db,
            """
            INSERT INTO chat_jobs(
                id, user_id, chat_id, mode, job_kind, user_message_id, reply_message_id,
                status, progress_stage, started_at
            )
            VALUES (?, ?, ?, 'analyse', 'verification_retry', ?, ?, 'queued', 'verifying', ?)
            """,
            (
                job_id,
                user["id"],
                chat_id,
                int(source_message["id"]),
                reply_message_id,
                int(time.time()),
            ),
            int(user["id"]),
            chat_id,
        )
        record_chat_job_progress(db, job_id, "queued", update_stage=False)
    with JOB_CANCEL_LOCK:
        JOB_CANCEL_EVENTS[job_id] = threading.Event()
        JOB_PAUSE_EVENTS[job_id] = threading.Event()
    enqueue_ollama_job(job_id)
    return JSONResponse(
        status_code=202,
        content={
            "status": "queued",
            "job_id": job_id,
            "chat_id": chat_id,
            "title": chat_row["title"],
            "job_kind": "verification_retry",
            "progress_stage": "verifying",
            "progress_current": 0,
            "progress_total": 0,
        },
    )


@app.get("/api/admin/users")
def list_users(
    request: Request,
    q: Annotated[str, Query(max_length=100)] = "",
    role: Literal["all", "owner", "admin", "member"] = "all",
    status: Literal["all", "active", "banned"] = "all",
    sort: Literal[
        "role", "username", "email", "created", "last_login", "chats", "storage"
    ] = "role",
    direction: Literal["asc", "desc"] = "desc",
    page: Annotated[int, Query(ge=1, le=100_000)] = 1,
) -> dict[str, object]:
    require_admin(request)
    now = int(time.time())
    clauses: list[str] = []
    parameters: list[object] = []
    search = q.strip()
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        clauses.append(
            "(u.username LIKE ? ESCAPE '\\' OR u.email LIKE ? ESCAPE '\\')"
        )
        parameters.extend((pattern, pattern))
    if role == "owner":
        clauses.append("u.is_owner = 1")
    elif role == "admin":
        clauses.append("u.is_admin = 1 AND u.is_owner = 0")
    elif role == "member":
        clauses.append("u.is_admin = 0 AND u.is_owner = 0")
    if status == "active":
        clauses.append("u.is_banned = 0")
    elif status == "banned":
        clauses.append("u.is_banned = 1")
    where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
    sort_expressions = {
        "role": "role_rank",
        "username": "u.username COLLATE NOCASE",
        "email": "u.email COLLATE NOCASE",
        "created": "u.created_at",
        "last_login": "COALESCE(u.last_login_at, 0)",
        "chats": "history_count",
        "storage": "history_size",
    }
    order_direction = "ASC" if direction == "asc" else "DESC"
    order_sql = (
        f"{sort_expressions[sort]} {order_direction}, "
        "u.username COLLATE NOCASE ASC, u.id ASC"
    )
    offset = (page - 1) * ADMIN_USER_PAGE_SIZE
    with connect_db() as db:
        expire_user_bans(db, now=now)
        total = int(
            db.execute(
                f"SELECT COUNT(*) FROM users AS u{where_sql}", parameters
            ).fetchone()[0]
        )
        rows = db.execute(
            f"""
            SELECT u.id, u.username, u.email, u.created_at, u.last_login_at,
                   u.is_admin, u.is_owner, u.is_banned, u.is_anonymized,
                   u.ban_reason, u.banned_until, u.storage_limit_bytes,
                   u.active_job_limit, u.pending_input_char_limit,
                   (SELECT COUNT(*) FROM login_sessions AS s
                    WHERE s.user_id = u.id AND s.expires_at > ?) AS active_session_count,
                   (SELECT COUNT(*) FROM chat_histories AS h WHERE h.user_id = u.id)
                       AS history_count,
                   ((SELECT COALESCE(SUM(length(CAST(m.content AS BLOB))), 0)
                     FROM messages AS m
                     WHERE m.session_id LIKE ('user-' || u.id || ':%'))
                    +
                    (SELECT COALESCE(SUM(f.size_bytes), 0)
                     FROM project_files AS f
                     JOIN projects AS p ON p.id = f.project_id
                     WHERE p.user_id = u.id)) AS history_size,
                   CASE WHEN u.is_owner = 1 THEN 3 WHEN u.is_admin = 1 THEN 2
                        WHEN u.is_banned = 1 THEN 0 ELSE 1 END AS role_rank
            FROM users AS u
            {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            (now, *parameters, ADMIN_USER_PAGE_SIZE, offset),
        ).fetchall()
    total_pages = max(1, (total + ADMIN_USER_PAGE_SIZE - 1) // ADMIN_USER_PAGE_SIZE)
    users = []
    throttle_scopes = {
        login_account_scope(int(row["id"]), ""): int(row["id"]) for row in rows
    }
    throttle_by_user: dict[int, sqlite3.Row] = {}
    if throttle_scopes:
        placeholders = ",".join("?" for _ in throttle_scopes)
        with connect_db() as db:
            throttle_rows = db.execute(
                f"""
                SELECT scope_hash, failed_attempts, locked_until
                FROM login_throttles WHERE scope_hash IN ({placeholders})
                """,
                tuple(throttle_scopes),
            ).fetchall()
        throttle_by_user = {
            throttle_scopes[str(row["scope_hash"])]: row for row in throttle_rows
        }
    for row in rows:
        user_data = dict(row)
        user_data.pop("role_rank", None)
        throttle = throttle_by_user.get(int(row["id"]))
        user_data["failed_login_attempts"] = (
            int(throttle["failed_attempts"]) if throttle is not None else 0
        )
        user_data["locked_until"] = (
            int(throttle["locked_until"]) if throttle is not None else 0
        )
        users.append(user_data)
    return {
        "users": users,
        "page": page,
        "page_size": ADMIN_USER_PAGE_SIZE,
        "total": total,
        "total_pages": total_pages,
        "default_limits": {
            "active_jobs": MAX_ACTIVE_JOBS_PER_USER,
            "pending_input_characters": MAX_PENDING_INPUT_CHARS_PER_USER,
        },
    }


ADMIN_AUDIT_ACTIONS = frozenset(
    {
        "make_admin",
        "remove_admin",
        "ban",
        "unban",
        "revoke_sessions",
        "unlock",
        "cancel_job",
        "create_backup",
        "run_maintenance",
        "set_registration",
        "set_ai_work",
        "publish_announcement",
        "clear_announcement",
        "send_password_reset",
        "delete_account",
        "anonymize_account",
        "set_user_limits",
        "revoke_registration",
        "run_integrity_check",
    }
)


def admin_audit_filter(
    q: str,
    action: str,
    from_timestamp: int | None,
    to_timestamp: int | None,
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    parameters: list[object] = []
    search = q.strip()
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        clauses.append(
            "(actor_username LIKE ? ESCAPE '\\' OR target_username LIKE ? ESCAPE '\\' "
            "OR request_id LIKE ? ESCAPE '\\')"
        )
        parameters.extend((pattern, pattern, pattern))
    if action != "all":
        if action not in ADMIN_AUDIT_ACTIONS:
            raise HTTPException(status_code=422, detail="Unknown audit action")
        clauses.append("action = ?")
        parameters.append(action)
    if from_timestamp is not None:
        clauses.append("created_at >= ?")
        parameters.append(from_timestamp)
    if to_timestamp is not None:
        clauses.append("created_at <= ?")
        parameters.append(to_timestamp)
    if (
        from_timestamp is not None
        and to_timestamp is not None
        and from_timestamp > to_timestamp
    ):
        raise HTTPException(
            status_code=422,
            detail="Audit start time cannot be later than the end time",
        )
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), parameters


@app.get("/api/admin/audit")
def list_admin_audit_events(
    request: Request,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
    q: Annotated[str, Query(max_length=100)] = "",
    action: Annotated[str, Query(max_length=40)] = "all",
    from_timestamp: Annotated[int | None, Query(ge=0)] = None,
    to_timestamp: Annotated[int | None, Query(ge=0)] = None,
    page: Annotated[int, Query(ge=1, le=100_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, object]:
    actor = require_admin(request)
    if not actor["is_owner"]:
        raise HTTPException(status_code=403, detail="Owner access required")
    if limit is not None:
        page = 1
        page_size = limit
    where_sql, parameters = admin_audit_filter(
        q, action, from_timestamp, to_timestamp
    )
    offset = (page - 1) * page_size
    with connect_db() as db:
        total = int(
            db.execute(
                f"SELECT COUNT(*) FROM admin_audit_events{where_sql}", parameters
            ).fetchone()[0]
        )
        rows = db.execute(
            f"""
            SELECT id, actor_user_id, actor_username, target_user_id,
                   target_username, action, request_id, details, created_at
            FROM admin_audit_events
            {where_sql}
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (*parameters, page_size, offset),
        ).fetchall()
    total_pages = max(1, (total + page_size - 1) // page_size)
    return {
        "events": [dict(row) for row in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "actions": sorted(ADMIN_AUDIT_ACTIONS),
    }


def record_admin_audit_event(
    db: sqlite3.Connection,
    actor: sqlite3.Row,
    target: sqlite3.Row | dict[str, object],
    action: str,
    request_id: str,
    *,
    details: dict[str, object] | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO admin_audit_events(
            actor_user_id, actor_username, target_user_id, target_username,
            action, request_id, details, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            actor["id"],
            actor["username"],
            target["id"],
            target["username"],
            action,
            request_id,
            json.dumps(details, separators=(",", ":")) if details else None,
            int(time.time()),
        ),
    )


def csv_safe_cell(value: object) -> str:
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


@app.get("/api/admin/audit/export")
def export_admin_audit_events(
    request: Request,
    q: Annotated[str, Query(max_length=100)] = "",
    action: Annotated[str, Query(max_length=40)] = "all",
    from_timestamp: Annotated[int | None, Query(ge=0)] = None,
    to_timestamp: Annotated[int | None, Query(ge=0)] = None,
) -> StreamingResponse:
    actor = require_admin(request)
    if not actor["is_owner"]:
        raise HTTPException(status_code=403, detail="Owner access required")
    where_sql, parameters = admin_audit_filter(
        q, action, from_timestamp, to_timestamp
    )

    def rows() -> Iterator[str]:
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\r\n")
        output.write("\ufeff")
        writer.writerow(
            (
                "time_utc",
                "actor",
                "action",
                "target",
                "details",
                "request_id",
            )
        )
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)
        with connect_db() as db:
            cursor = db.execute(
                f"""
                SELECT actor_username, action, target_username, details,
                       request_id, created_at
                FROM admin_audit_events
                {where_sql}
                ORDER BY created_at DESC, id DESC
                """,
                parameters,
            )
            while True:
                batch = cursor.fetchmany(500)
                if not batch:
                    break
                for row in batch:
                    created = datetime.fromtimestamp(
                        int(row["created_at"]), timezone.utc
                    ).isoformat()
                    writer.writerow(
                        (
                            created,
                            csv_safe_cell(row["actor_username"]),
                            row["action"],
                            csv_safe_cell(row["target_username"]),
                            csv_safe_cell(row["details"]),
                            csv_safe_cell(row["request_id"]),
                        )
                    )
                    yield output.getvalue()
                    output.seek(0)
                    output.truncate(0)

    filename = datetime.now(timezone.utc).strftime("admin-audit-%Y%m%dT%H%M%SZ.csv")
    return StreamingResponse(
        rows(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def require_owner_actor(request: Request) -> sqlite3.Row:
    actor = require_admin(request)
    if not actor["is_owner"]:
        raise HTTPException(status_code=403, detail="Owner access required")
    return actor


def require_manageable_target(
    db: sqlite3.Connection,
    actor: sqlite3.Row,
    user_id: int,
    *,
    owner_required: bool = False,
) -> sqlite3.Row:
    target = db.execute(
        """
        SELECT id, email, username, is_admin, is_owner, is_banned, is_anonymized,
               storage_limit_bytes, active_job_limit, pending_input_char_limit
        FROM users WHERE id = ?
        """,
        (user_id,),
    ).fetchone()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target["is_owner"]:
        raise HTTPException(status_code=403, detail="The owner account cannot be changed")
    if owner_required and not actor["is_owner"]:
        raise HTTPException(status_code=403, detail="Owner access required")
    if target["is_admin"] and not actor["is_owner"]:
        raise HTTPException(
            status_code=403,
            detail="Only the owner can manage administrator accounts",
        )
    return target


@app.post("/api/admin/users/{user_id}/password-reset")
def send_admin_password_reset(
    user_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    actor = require_admin(request)
    if not SMTP_HOST or not SMTP_FROM:
        raise HTTPException(status_code=503, detail="Password-reset email is not configured")
    try:
        base_url = verification_base_url()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Password-reset links are not configured") from exc
    request_id = getattr(request.state, "request_id", None) or request_id_from_header(None)
    now = int(time.time())
    token = secrets.token_urlsafe(32)
    digest = token_digest(token)
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        target = require_manageable_target(db, actor, user_id)
        if target["is_anonymized"]:
            raise HTTPException(status_code=409, detail="This account has been anonymized")
        db.execute(
            "UPDATE password_reset_tokens SET used_at = CURRENT_TIMESTAMP "
            "WHERE user_id = ? AND used_at IS NULL",
            (user_id,),
        )
        db.execute(
            """
            INSERT INTO password_reset_tokens(user_id, token_hash, expires_at, requested_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, digest, now + PASSWORD_RESET_MINUTES * 60, now),
        )
        record_admin_audit_event(
            db, actor, target, "send_password_reset", request_id
        )
        recipient = str(target["email"])
        username = str(target["username"])
    background_tasks.add_task(
        deliver_password_reset_email,
        recipient,
        f"{base_url}/reset-password?token={token}",
        digest,
    )
    return {"message": f"Password-reset email queued for {username}"}


@app.put("/api/admin/users/{user_id}/limits")
def set_admin_user_limits(
    user_id: int,
    payload: AdminUserLimits,
    request: Request,
) -> dict[str, object]:
    actor = require_owner_actor(request)
    if (
        payload.active_job_limit is not None
        and payload.active_job_limit > MAX_ACTIVE_JOBS_PER_USER
    ):
        raise HTTPException(
            status_code=422,
            detail=f"Per-user active-job limit cannot exceed {MAX_ACTIVE_JOBS_PER_USER}",
        )
    if (
        payload.pending_input_char_limit is not None
        and payload.pending_input_char_limit > MAX_PENDING_INPUT_CHARS_PER_USER
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "Per-user pending-input limit cannot exceed "
                f"{MAX_PENDING_INPUT_CHARS_PER_USER}"
            ),
        )
    request_id = getattr(request.state, "request_id", None) or request_id_from_header(None)
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        target = require_manageable_target(
            db, actor, user_id, owner_required=True
        )
        if target["is_anonymized"]:
            raise HTTPException(status_code=409, detail="This account has been anonymized")
        db.execute(
            """
            UPDATE users
            SET storage_limit_bytes = ?, active_job_limit = ?,
                pending_input_char_limit = ?
            WHERE id = ?
            """,
            (
                payload.storage_limit_bytes,
                payload.active_job_limit,
                payload.pending_input_char_limit,
                user_id,
            ),
        )
        details = {
            "storage_limit_bytes": payload.storage_limit_bytes,
            "active_job_limit": payload.active_job_limit,
            "pending_input_char_limit": payload.pending_input_char_limit,
        }
        record_admin_audit_event(
            db,
            actor,
            target,
            "set_user_limits",
            request_id,
            details=details,
        )
    return {
        "message": f"Updated limits for {target['username']}",
        "limits": details,
    }


@app.post("/api/admin/users/{user_id}/account-data")
def dispose_admin_user_account(
    user_id: int,
    payload: AdminAccountDisposition,
    request: Request,
) -> dict[str, object]:
    actor = require_owner_actor(request)
    request_id = getattr(request.state, "request_id", None) or request_id_from_header(None)
    replacement_password_hash = (
        hash_password(secrets.token_urlsafe(32))
        if payload.mode == "anonymize"
        else None
    )
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        target = require_manageable_target(
            db, actor, user_id, owner_required=True
        )
        if target["is_anonymized"] and payload.mode == "anonymize":
            raise HTTPException(status_code=409, detail="This account is already anonymized")
        if payload.confirmation.casefold() != str(target["username"]).casefold():
            raise HTTPException(
                status_code=422,
                detail="Enter the exact username to confirm this account action",
            )
        active_jobs = int(
            db.execute(
                """
                SELECT COUNT(*) FROM chat_jobs
                WHERE user_id = ? AND status IN ('queued', 'processing')
                """,
                (user_id,),
            ).fetchone()[0]
        )
        if active_jobs:
            raise HTTPException(
                status_code=409,
                detail="Cancel this account's active jobs before removing its data",
            )
        counts = db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM chat_histories WHERE user_id = ?) AS chats,
                (SELECT COUNT(*) FROM messages
                 WHERE session_id LIKE ('user-' || ? || ':%')) AS messages,
                (SELECT COUNT(*) FROM login_sessions WHERE user_id = ?) AS sessions
            """,
            (user_id, user_id, user_id),
        ).fetchone()
        details = {
            "chats_removed": int(counts["chats"]),
            "messages_removed": int(counts["messages"]),
            "sessions_revoked": int(counts["sessions"]),
        }
        anonymized_label = f"Anonymized account #{user_id}"
        db.execute(
            """
            UPDATE admin_audit_events
            SET actor_user_id = NULL, actor_username = 'Former administrator'
            WHERE actor_user_id = ?
            """,
            (user_id,),
        )
        db.execute(
            """
            UPDATE admin_audit_events
            SET target_user_id = NULL, target_username = ?
            WHERE target_user_id = ?
            """,
            (anonymized_label, user_id),
        )
        db.execute(
            "DELETE FROM messages WHERE session_id LIKE ('user-' || ? || ':%')",
            (user_id,),
        )
        db.execute("DELETE FROM chat_jobs WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM chat_histories WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM login_sessions WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM password_reset_tokens WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM registration_tokens WHERE email = ?", (target["email"],))
        db.execute(
            "DELETE FROM login_throttles WHERE scope_hash = ?",
            (login_account_scope(user_id, ""),),
        )
        if payload.mode == "anonymize":
            replacement_username = f"anonymized_{user_id}_{uuid.uuid4().hex[:6]}"
            replacement_email = f"anonymized-{user_id}-{uuid.uuid4().hex[:8]}@invalid.local"
            db.execute(
                """
                UPDATE users
                SET username = ?, email = ?, password_hash = ?, is_admin = 0,
                    is_owner = 0, is_banned = 1, is_anonymized = 1,
                    ban_reason = 'Account anonymized by owner', banned_until = NULL,
                    storage_limit_bytes = NULL, active_job_limit = NULL,
                    pending_input_char_limit = NULL, last_login_at = NULL
                WHERE id = ?
                """,
                (
                    replacement_username,
                    replacement_email,
                    replacement_password_hash,
                    user_id,
                ),
            )
            target_for_audit = {"id": user_id, "username": anonymized_label}
            action = "anonymize_account"
            message = "Account identity and stored data were anonymized"
        else:
            target_for_audit = {"id": user_id, "username": f"Deleted account #{user_id}"}
            action = "delete_account"
            message = "Account and stored data were permanently deleted"
        record_admin_audit_event(
            db,
            actor,
            target_for_audit,
            action,
            request_id,
            details=details,
        )
        if payload.mode == "delete":
            db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return {"message": message, **details}


@app.get("/api/admin/registrations")
def list_pending_admin_registrations(
    request: Request,
    q: Annotated[str, Query(max_length=100)] = "",
    page: Annotated[int, Query(ge=1, le=100_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> dict[str, object]:
    require_owner_actor(request)
    clauses = ["used_at IS NULL"]
    parameters: list[object] = []
    search = q.strip()
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("email LIKE ? ESCAPE '\\'")
        parameters.append(f"%{escaped}%")
    where_sql = " WHERE " + " AND ".join(clauses)
    offset = (page - 1) * page_size
    with connect_db() as db:
        total = int(
            db.execute(
                f"SELECT COUNT(*) FROM registration_tokens{where_sql}", parameters
            ).fetchone()[0]
        )
        rows = db.execute(
            f"""
            SELECT id, email, requested_at, expires_at
            FROM registration_tokens
            {where_sql}
            ORDER BY requested_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (*parameters, page_size, offset),
        ).fetchall()
    return {
        "registrations": [dict(row) for row in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "generated_at": int(time.time()),
    }


@app.delete("/api/admin/registrations/{registration_id}")
def revoke_admin_registration(
    registration_id: int,
    request: Request,
) -> dict[str, str]:
    actor = require_owner_actor(request)
    request_id = getattr(request.state, "request_id", None) or request_id_from_header(None)
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        registration = db.execute(
            """
            SELECT id, email FROM registration_tokens
            WHERE id = ? AND used_at IS NULL
            """,
            (registration_id,),
        ).fetchone()
        if registration is None:
            raise HTTPException(status_code=404, detail="Pending registration not found")
        db.execute("DELETE FROM registration_tokens WHERE id = ?", (registration_id,))
        record_admin_audit_event(
            db,
            actor,
            {"id": None, "username": registration["email"]},
            "revoke_registration",
            request_id,
            details={"registration_id": registration_id},
        )
    return {"message": "Pending registration revoked"}


@app.post("/api/admin/database/integrity-check")
def run_admin_integrity_check(request: Request) -> dict[str, object]:
    actor = require_owner_actor(request)
    request_id = getattr(request.state, "request_id", None) or request_id_from_header(None)
    checked_at = int(time.time())
    try:
        with DATABASE_MAINTENANCE_LOCK, connect_db() as db:
            results = [str(row[0]) for row in db.execute("PRAGMA quick_check").fetchall()]
            if results != ["ok"]:
                raise RuntimeError("; ".join(results[:5]))
            db.execute(
                """
                INSERT INTO application_settings(key, value, updated_at, updated_by_user_id)
                VALUES ('last_integrity_check', 'ok', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at,
                    updated_by_user_id = excluded.updated_by_user_id
                """,
                (checked_at, actor["id"]),
            )
            record_admin_audit_event(
                db,
                actor,
                actor,
                "run_integrity_check",
                request_id,
            )
    except (RuntimeError, sqlite3.Error) as exc:
        LOGGER.error(
            "Owner-triggered database integrity check failed",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        raise HTTPException(status_code=500, detail="Database integrity check failed")
    return {
        "message": "Database integrity check passed",
        "status": "ok",
        "checked_at": checked_at,
    }


@app.post("/api/admin/backups")
def create_admin_database_backup(request: Request) -> dict[str, object]:
    actor = require_admin(request)
    if not actor["is_owner"]:
        raise HTTPException(status_code=403, detail="Owner access required")
    request_id = getattr(request.state, "request_id", None) or request_id_from_header(None)
    try:
        backup_path = create_periodic_database_backup()
        backup_size = int(backup_path.stat().st_size)
        created_at = int(backup_path.stat().st_mtime)
        backup_count = len(periodic_backup_paths())
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        LOGGER.error(
            "Owner-triggered database backup failed",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        raise HTTPException(status_code=500, detail="The database backup could not be created")
    with connect_db() as db:
        record_admin_audit_event(
            db,
            actor,
            actor,
            "create_backup",
            request_id,
            details={"size_bytes": backup_size, "backup_count": backup_count},
        )
    return {
        "message": "Verified database backup created",
        "created_at": created_at,
        "size_bytes": backup_size,
        "backup_count": backup_count,
    }


@app.post("/api/admin/maintenance")
def run_admin_database_maintenance(request: Request) -> dict[str, object]:
    actor = require_admin(request)
    if not actor["is_owner"]:
        raise HTTPException(status_code=403, detail="Owner access required")
    request_id = getattr(request.state, "request_id", None) or request_id_from_header(None)
    try:
        deleted, compacted_jobs = run_database_maintenance()
    except (OSError, sqlite3.Error) as exc:
        LOGGER.error(
            "Owner-triggered database maintenance failed",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        raise HTTPException(
            status_code=500, detail="Database maintenance could not be completed"
        )
    expired_records = sum(deleted.values())
    with connect_db() as db:
        record_admin_audit_event(
            db,
            actor,
            actor,
            "run_maintenance",
            request_id,
            details={
                "expired_records": expired_records,
                "compacted_jobs": compacted_jobs,
            },
        )
    return {
        "message": "Database maintenance completed",
        "expired_records_removed": expired_records,
        "terminal_jobs_compacted": compacted_jobs,
    }


@app.patch("/api/admin/settings/registration")
def set_admin_registration(
    payload: AdminRegistrationSetting, request: Request
) -> dict[str, object]:
    actor = require_admin(request)
    if not actor["is_owner"]:
        raise HTTPException(status_code=403, detail="Owner access required")
    request_id = getattr(request.state, "request_id", None) or request_id_from_header(None)
    desired_value = "1" if payload.enabled else "0"
    changed = False
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        current_enabled = registration_is_enabled(db)
        if current_enabled != payload.enabled:
            db.execute(
                """
                INSERT INTO application_settings(
                    key, value, updated_at, updated_by_user_id
                ) VALUES ('registration_enabled', ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at,
                    updated_by_user_id = excluded.updated_by_user_id
                """,
                (desired_value, int(time.time()), actor["id"]),
            )
            record_admin_audit_event(
                db,
                actor,
                actor,
                "set_registration",
                request_id,
                details={"enabled": payload.enabled},
            )
            changed = True
    state = "open" if payload.enabled else "closed"
    return {
        "message": f"Registration is {state}",
        "enabled": payload.enabled,
        "changed": changed,
    }


@app.patch("/api/admin/settings/ai-work")
def set_admin_ai_work(
    payload: AdminAiWorkSetting, request: Request
) -> dict[str, object]:
    actor = require_admin(request)
    if not actor["is_owner"]:
        raise HTTPException(status_code=403, detail="Owner access required")
    request_id = getattr(request.state, "request_id", None) or request_id_from_header(None)
    desired_value = "1" if payload.enabled else "0"
    changed = False
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        current_enabled = ai_work_is_enabled(db)
        if current_enabled != payload.enabled:
            db.execute(
                """
                INSERT INTO application_settings(
                    key, value, updated_at, updated_by_user_id
                ) VALUES ('ai_work_enabled', ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at,
                    updated_by_user_id = excluded.updated_by_user_id
                """,
                (desired_value, int(time.time()), actor["id"]),
            )
            record_admin_audit_event(
                db,
                actor,
                actor,
                "set_ai_work",
                request_id,
                details={"enabled": payload.enabled},
            )
            changed = True
    state = "available" if payload.enabled else "paused"
    return {
        "message": f"AI work is {state}",
        "enabled": payload.enabled,
        "changed": changed,
    }


@app.put("/api/admin/announcement")
def publish_admin_announcement(
    payload: AdminAnnouncementSetting, request: Request
) -> dict[str, object]:
    actor = require_admin(request)
    if not actor["is_owner"]:
        raise HTTPException(status_code=403, detail="Owner access required")
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Announcement message cannot be empty")
    now = int(time.time())
    expires_at = (
        now + payload.expires_in_hours * 60 * 60
        if payload.expires_in_hours is not None
        else None
    )
    announcement = {
        "id": uuid.uuid4().hex,
        "message": message,
        "level": payload.level,
        "expires_at": expires_at,
    }
    request_id = getattr(request.state, "request_id", None) or request_id_from_header(None)
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """
            INSERT INTO application_settings(
                key, value, updated_at, updated_by_user_id
            ) VALUES ('announcement', ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at,
                updated_by_user_id = excluded.updated_by_user_id
            """,
            (json.dumps(announcement, separators=(",", ":")), now, actor["id"]),
        )
        record_admin_audit_event(
            db,
            actor,
            actor,
            "publish_announcement",
            request_id,
            details={
                "announcement_id": announcement["id"],
                "level": payload.level,
                "expires_at": expires_at,
            },
        )
    return {
        "message": "Site announcement published",
        "announcement": {**announcement, "published_at": now},
    }


@app.delete("/api/admin/announcement")
def clear_admin_announcement(request: Request) -> dict[str, object]:
    actor = require_admin(request)
    if not actor["is_owner"]:
        raise HTTPException(status_code=403, detail="Owner access required")
    request_id = getattr(request.state, "request_id", None) or request_id_from_header(None)
    changed = False
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = active_announcement(db)
        deleted = db.execute(
            "DELETE FROM application_settings WHERE key = 'announcement'"
        ).rowcount
        changed = deleted > 0
        if changed:
            record_admin_audit_event(
                db,
                actor,
                actor,
                "clear_announcement",
                request_id,
                details={
                    "announcement_id": previous["id"] if previous is not None else None
                },
            )
    return {
        "message": "Site announcement cleared" if changed else "No site announcement was set",
        "changed": changed,
    }


@app.get("/api/announcement")
def get_current_announcement(request: Request, response: Response) -> dict[str, object]:
    require_user(request)
    response.headers["Cache-Control"] = "no-store"
    return {"announcement": active_announcement()}


@app.get("/api/admin/system-health")
def admin_system_health(request: Request) -> dict[str, object]:
    """Return a credential-free operational summary for the Admin WebUI."""
    actor = require_admin(request)
    now = int(time.time())
    readiness_report = cached_readiness_report()
    with connect_db() as db:
        counts = db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM users) AS users,
                (SELECT COUNT(*) FROM chat_histories) AS chats,
                (SELECT COUNT(*) FROM messages) AS messages,
                (SELECT COUNT(*) FROM chat_jobs WHERE status = 'queued') AS queued_jobs,
                (SELECT COUNT(*) FROM chat_jobs WHERE status = 'processing') AS processing_jobs
            """
        ).fetchone()
        page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(db.execute("PRAGMA page_size").fetchone()[0])
        schema_version = int(db.execute("PRAGMA user_version").fetchone()[0])
        registration_enabled = registration_is_enabled(db)
        ai_work_enabled = ai_work_is_enabled(db)
        announcement = active_announcement(db)
        integrity = db.execute(
            """
            SELECT value, updated_at FROM application_settings
            WHERE key = 'last_integrity_check'
            """
        ).fetchone()

    worker = OLLAMA_WORKER_THREAD
    job_worker_alive = bool(
        worker is not None
        and worker.is_alive()
        and not OLLAMA_WORKER_STOP.is_set()
    )
    maintenance_worker = MAINTENANCE_THREAD
    maintenance_worker_alive = bool(
        maintenance_worker is not None
        and maintenance_worker.is_alive()
        and not MAINTENANCE_STOP.is_set()
    )

    backups = periodic_backup_paths()
    latest_backup_at: int | None = None
    if backups:
        try:
            latest_backup_at = int(backups[0].stat().st_mtime)
        except OSError:
            latest_backup_at = None
    next_backup_in: int | None = None
    if CREATE_PERIODIC_BACKUPS:
        try:
            next_backup_in = int(seconds_until_periodic_backup(now=now))
        except OSError:
            next_backup_in = None

    database_report: dict[str, object] = {
        "status": readiness_report["checks"].get("database", "unknown"),
        "size_bytes": page_count * page_size,
        "schema_version": schema_version,
        "users": int(counts["users"]),
        "chats": int(counts["chats"]),
        "messages": int(counts["messages"]),
    }
    if actor["is_owner"]:
        database_report["integrity"] = {
            "status": str(integrity["value"]) if integrity and integrity["value"] else "unchecked",
            "checked_at": int(integrity["updated_at"]) if integrity and integrity["updated_at"] else None,
        }

    return {
        "status": readiness_report["status"],
        "generated_at": now,
        "started_at": PROCESS_STARTED_AT,
        "uptime_seconds": max(0, int(time.monotonic() - PROCESS_STARTED_MONOTONIC)),
        "checks": dict(readiness_report["checks"]),
        "model": OLLAMA_MODEL,
        "registration": {"enabled": registration_enabled},
        "ai_work": {"enabled": ai_work_enabled},
        "announcement": announcement,
        "jobs": {
            "worker_alive": job_worker_alive,
            "queued": int(counts["queued_jobs"]),
            "processing": int(counts["processing_jobs"]),
            "capacity": JOB_QUEUE_CAPACITY,
        },
        "database": database_report,
        "backups": {
            "enabled": CREATE_PERIODIC_BACKUPS,
            "worker_alive": maintenance_worker_alive,
            "count": len(backups),
            "latest_at": latest_backup_at,
            "next_in_seconds": next_backup_in,
            "retention_count": PERIODIC_BACKUP_RETENTION_COUNT,
        },
    }


@app.get("/api/admin/jobs")
def list_active_admin_jobs(request: Request) -> dict[str, object]:
    require_admin(request)
    now = int(time.time())
    with connect_db() as db:
        rows = db.execute(
            """
            SELECT job.id, job.user_id, user.username, user.is_admin, user.is_owner,
                   job.chat_id, COALESCE(history.title, job.title, 'Unknown chat') AS title,
                   job.status, job.job_kind, job.mode, job.progress_stage,
                   job.progress_current, job.progress_total, job.started_at,
                   job.paused_at, job.paused_seconds,
                   job.cancel_requested, job.updated_at, job.progress_log,
                   length(COALESCE(job.message, '')) AS input_chars
            FROM chat_jobs AS job
            JOIN users AS user ON user.id = job.user_id
            LEFT JOIN chat_histories AS history
              ON history.id = job.chat_id AND history.user_id = job.user_id
            WHERE job.status IN ('queued', 'processing')
            ORDER BY CASE job.status WHEN 'processing' THEN 0 ELSE 1 END,
                     job.created_at, job.id
            """
        ).fetchall()
    jobs: list[dict[str, object]] = []
    for row in rows:
        job = dict(row)
        progress_events = decode_progress_log(job.pop("progress_log", "[]"))
        job["progress_message"] = (
            progress_events[-1].get("message") if progress_events else None
        )
        job["elapsed_seconds"] = active_job_elapsed_seconds(
            job["started_at"],
            job["paused_at"],
            job["paused_seconds"],
            now=now,
        )
        jobs.append(job)
    worker = OLLAMA_WORKER_THREAD
    return {
        "jobs": jobs,
        "queue_depth": sum(job["status"] == "queued" for job in jobs),
        "worker_alive": bool(
            worker is not None
            and worker.is_alive()
            and not OLLAMA_WORKER_STOP.is_set()
        ),
    }


@app.post("/api/admin/chat-jobs/{job_id}/cancel")
def cancel_admin_chat_job(job_id: str, request: Request) -> dict[str, str]:
    actor = require_admin(request)
    request_id = getattr(request.state, "request_id", None) or request_id_from_header(None)
    cancelled_while_queued = False
    reason = "Cancelled by an administrator."
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        job = db.execute(
            """
            SELECT job.id, job.status, job.chat_id, job.user_id, job.project_id,
                   user.username, user.is_admin, user.is_owner
            FROM chat_jobs AS job
            JOIN users AS user ON user.id = job.user_id
            WHERE job.id = ?
            """,
            (job_id,),
        ).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="Active chat job not found")
        if job["status"] not in {"queued", "processing"}:
            raise HTTPException(status_code=409, detail="This chat job is already complete")
        if job["is_owner"]:
            raise HTTPException(status_code=403, detail="Owner jobs cannot be cancelled")
        if job["is_admin"] and not actor["is_owner"]:
            raise HTTPException(
                status_code=403,
                detail="Only the owner can cancel administrator jobs",
            )
        db.execute(
            """
            UPDATE chat_jobs
            SET cancel_requested = 1, cancel_reason = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (reason, job_id),
        )
        if job["status"] == "queued":
            cursor = db.execute(
                """
                UPDATE chat_jobs
                SET status = 'failed', message = NULL, error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'queued'
                """,
                (reason, job_id),
            )
            cancelled_while_queued = cursor.rowcount > 0
            if cancelled_while_queued:
                record_chat_job_progress(db, job_id, "cancelled", update_stage=False)
                if job["project_id"] is not None:
                    refresh_project_function_analysis(db, str(job["project_id"]))
        record_admin_audit_event(
            db,
            actor,
            {"id": job["user_id"], "username": job["username"]},
            "cancel_job",
            request_id,
            details={"job_id": job_id, "chat_id": job["chat_id"]},
        )
    with JOB_CANCEL_LOCK:
        event = JOB_CANCEL_EVENTS.get(job_id)
        if event:
            event.set()
    if cancelled_while_queued:
        persist_failed_job_message(job_id, "Process cancelled by an administrator.")
        return {"status": "cancelled"}
    return {"status": "cancellation_requested"}


@app.patch("/api/admin/users/{user_id}")
def change_user(user_id: int, payload: AdminUserAction, request: Request) -> dict[str, str]:
    actor = require_admin(request)
    request_id = getattr(request.state, "request_id", None) or request_id_from_header(None)
    with connect_db() as db:
        target = db.execute(
            """
            SELECT id, username, is_admin, is_owner, is_banned,
                   ban_reason, banned_until, is_anonymized
            FROM users WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if target is None:
            raise HTTPException(status_code=404, detail="User not found")
        if target["is_owner"]:
            raise HTTPException(status_code=403, detail="The owner account cannot be changed")

        message = f"{target['username']} was updated"
        if payload.action in {"make_admin", "remove_admin"}:
            if not actor["is_owner"]:
                raise HTTPException(status_code=403, detail="Only the owner can change administrators")
            make_admin = payload.action == "make_admin"
            db.execute(
                """
                UPDATE users
                SET is_admin = ?,
                    is_banned = CASE WHEN ? THEN 0 ELSE is_banned END,
                    ban_reason = CASE WHEN ? THEN NULL ELSE ban_reason END,
                    banned_until = CASE WHEN ? THEN NULL ELSE banned_until END
                WHERE id = ?
                """,
                (int(make_admin), int(make_admin), int(make_admin), int(make_admin), user_id),
            )
        elif payload.action in {"ban", "unban"}:
            if target["is_admin"]:
                raise HTTPException(status_code=403, detail="Administrators cannot be banned")
            should_ban = payload.action == "ban"
            reason = (payload.reason or "No reason provided").strip() or "No reason provided"
            banned_until = (
                int(time.time()) + payload.expires_in_hours * 60 * 60
                if should_ban and payload.expires_in_hours is not None
                else None
            )
            db.execute(
                """
                UPDATE users
                SET is_banned = ?, ban_reason = ?, banned_until = ?
                WHERE id = ?
                """,
                (
                    int(should_ban),
                    reason if should_ban else None,
                    banned_until if should_ban else None,
                    user_id,
                ),
            )
            if should_ban:
                revoked = db.execute(
                    "DELETE FROM login_sessions WHERE user_id = ?", (user_id,)
                ).rowcount
                message = (
                    f"Banned {target['username']}"
                    + (
                        " until "
                        + datetime.fromtimestamp(banned_until, timezone.utc).strftime(
                            "%Y-%m-%d %H:%M UTC"
                        )
                        if banned_until is not None
                        else " permanently"
                    )
                    + f" and revoked {revoked} session{'s' if revoked != 1 else ''}"
                )
            else:
                message = f"Unbanned {target['username']}"
        elif payload.action == "revoke_sessions":
            if target["is_admin"] and not actor["is_owner"]:
                raise HTTPException(
                    status_code=403,
                    detail="Only the owner can revoke administrator sessions",
                )
            revoked = db.execute(
                "DELETE FROM login_sessions WHERE user_id = ?", (user_id,)
            ).rowcount
            message = (
                f"Revoked {revoked} active session{'s' if revoked != 1 else ''} "
                f"for {target['username']}"
            )
        elif payload.action == "unlock":
            if target["is_admin"] and not actor["is_owner"]:
                raise HTTPException(
                    status_code=403,
                    detail="Only the owner can unlock administrators",
                )
            cleared = db.execute(
                "DELETE FROM login_throttles WHERE scope_hash = ?",
                (login_account_scope(user_id, ""),),
            ).rowcount
            message = (
                f"Cleared login restrictions for {target['username']}"
                if cleared
                else f"{target['username']} had no login restrictions"
            )
        else:
            raise HTTPException(status_code=422, detail="Unsupported administrator action")
        details = None
        if payload.action == "ban":
            details = {
                "reason": reason,
                "banned_until": banned_until,
            }
        record_admin_audit_event(
            db, actor, target, payload.action, request_id, details=details
        )
    return {"message": message}


def perform_chat(
    user_id: int,
    chat_id: str,
    message: str,
    mode: Literal["chat", "analyse"] = "chat",
    user_message_id: int | None = None,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> ChatResponse:
    with connect_db() as db:
        expire_user_bans(db, user_id=user_id)
        account = db.execute("SELECT is_banned FROM users WHERE id = ?", (user_id,)).fetchone()
        if account is None or account["is_banned"]:
            raise RuntimeError("This account cannot use the LLM")
        chat_row = db.execute(
            """
            SELECT title, title_is_custom FROM chat_histories
            WHERE id = ? AND user_id = ?
            """,
            (chat_id, user_id),
        ).fetchone()
    if chat_row is None:
        raise RuntimeError("Chat not found")
    memory_id = f"user-{user_id}:{chat_id}"
    previous = history_for(memory_id, exclude_message_id=user_message_id)
    reply, compact_context = generate_reply(
        previous,
        message,
        cancel_check,
        progress_callback,
        mode=mode,
    )
    if cancel_check and cancel_check():
        raise AnalysisCancelled("Analysis cancelled by user")
    if user_message_id is None:
        save_message(memory_id, "user", message, compact_context)
    else:
        with connect_db() as db:
            db.execute(
                """
                UPDATE messages SET context_content = ?
                WHERE id = ? AND session_id = ? AND role = 'user'
                """,
                (compact_context, user_message_id, memory_id),
            )
    reply_message_id = save_message(memory_id, "assistant", reply)
    with connect_db() as db:
        if not previous and not chat_row["title_is_custom"]:
            db.execute(
                "UPDATE chat_histories SET title = ? WHERE id = ? AND user_id = ?",
                (title_from_message(message), chat_id, user_id),
            )
        db.execute(
            "UPDATE chat_histories SET updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        title = db.execute(
            "SELECT title FROM chat_histories WHERE id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()["title"]
    return ChatResponse(
        chat_id=chat_id,
        title=title,
        reply=reply,
        reply_message_id=reply_message_id,
    )


def perform_verification_retry(
    user_id: int,
    chat_id: str,
    user_message_id: int,
    reply_message_id: int | None,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> ChatResponse:
    memory_id = f"user-{user_id}:{chat_id}"
    with connect_db() as db:
        chat_row = db.execute(
            "SELECT title FROM chat_histories WHERE id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
        source_message = db.execute(
            """
            SELECT content, context_content FROM messages
            WHERE id = ? AND session_id = ? AND role = 'user'
            """,
            (user_message_id, memory_id),
        ).fetchone()
    if chat_row is None or source_message is None:
        raise RuntimeError("The stored analysis request could not be found")
    notes = extract_stored_analysis_notes(source_message["context_content"])
    if not notes:
        raise RuntimeError("This chat does not contain reusable analysis notes")
    reply, _ = verify_analysis_notes(
        source_message["content"],
        notes,
        cancel_check,
        progress_callback,
    )
    if cancel_check and cancel_check():
        raise AnalysisCancelled("Analysis cancelled by user")
    with connect_db() as db:
        existing_reply = None
        if reply_message_id is not None:
            existing_reply = db.execute(
                """
                SELECT id FROM messages
                WHERE id = ? AND session_id = ? AND role = 'assistant'
                """,
                (reply_message_id, memory_id),
            ).fetchone()
        if existing_reply is not None:
            db.execute(
                "UPDATE messages SET content = ? WHERE id = ?",
                (reply, reply_message_id),
            )
            saved_reply_id = reply_message_id
        else:
            cursor = db.execute(
                """
                INSERT INTO messages(session_id, role, content)
                VALUES (?, 'assistant', ?)
                """,
                (memory_id, reply),
            )
            saved_reply_id = int(cursor.lastrowid)
        db.execute(
            "UPDATE chat_histories SET updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            (chat_id, user_id),
        )
    return ChatResponse(
        chat_id=chat_id,
        title=chat_row["title"],
        reply=reply,
        reply_message_id=saved_reply_id,
    )


def progress_event_text(
    stage: str, current: int, total: int, job_kind: str = "chat"
) -> str:
    if stage == "queued":
        if job_kind == "project_analysis":
            return "Project function analysis queued for Ollama"
        return (
            "Verification retry queued for Ollama"
            if job_kind == "verification_retry"
            else "Request queued for Ollama"
        )
    if stage == "analyzing":
        return f"Analysing source: section {current} of {total}"
    if stage == "processing":
        return f"Processing large request: section {current} of {total}"
    if stage == "consolidating":
        return f"Consolidating analysis: group {current} of {total}"
    if stage == "consolidating_context":
        return f"Consolidating request context: group {current} of {total}"
    if stage == "analysis_complete":
        return "Large-input analysis completed"
    if stage == "inventory_built":
        return "Verified source inventory built"
    if stage == "finalizing":
        return "Preparing final response"
    if stage == "verifying":
        return (
            "Retrying evidence-checked analysis"
            if job_kind == "verification_retry"
            else "Building evidence-checked analysis"
        )
    if stage == "repairing_evidence":
        return "Repairing source evidence"
    if stage == "thinking":
        return "Generating response"
    if stage == "completed":
        if job_kind == "project_analysis":
            return "Project function analysis completed"
        return "Analysis completed" if job_kind == "verification_retry" else "Request completed"
    if stage == "review_incomplete":
        return f"Analysis pass finished: {current} of {total} functions have complete reviews; resume to retry incomplete reviews"
    if stage == "cancelled":
        return "Process cancelled by user"
    if stage == "paused":
        return "Paused"
    if stage == "resumed":
        return "Resumed"
    if stage == "failed":
        return "Request failed"
    return stage.replace("_", " ").strip().capitalize()


def decode_progress_log(value: object) -> list[dict[str, object]]:
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(decoded, list):
        return []
    return [item for item in decoded if isinstance(item, dict)][-200:]


def active_job_elapsed_seconds(
    started_at: object,
    paused_at: object = None,
    paused_seconds: object = 0,
    *,
    now: int | None = None,
) -> int:
    """Return active run time, excluding completed and current pauses."""
    current = int(time.time()) if now is None else int(now)
    started = int(started_at or current)
    pause_started = int(paused_at) if paused_at is not None else None
    elapsed_until = min(current, pause_started) if pause_started is not None else current
    return max(0, elapsed_until - started - max(0, int(paused_seconds or 0)))


def record_chat_job_progress(
    db: sqlite3.Connection,
    job_id: str,
    stage: str,
    current: int = 0,
    total: int = 0,
    *,
    update_stage: bool = True,
) -> None:
    job = db.execute(
        """
        SELECT started_at, paused_at, paused_seconds, progress_log, job_kind
        FROM chat_jobs WHERE id = ?
        """,
        (job_id,),
    ).fetchone()
    if job is None:
        return
    now = int(time.time())
    started_at = int(job["started_at"] or now)
    events = decode_progress_log(job["progress_log"])
    key = f"{stage}:{current}:{total}"
    if not events or events[-1].get("key") != key:
        events.append(
            {
                "key": key,
                "elapsed": active_job_elapsed_seconds(
                    started_at,
                    job["paused_at"],
                    job["paused_seconds"],
                    now=now,
                ),
                "message": progress_event_text(stage, current, total, job["job_kind"]),
            }
        )
    if update_stage:
        db.execute(
            """
            UPDATE chat_jobs
            SET started_at = ?, progress_stage = ?, progress_current = ?, progress_total = ?,
                progress_log = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (started_at, stage, current, total, json.dumps(events[-200:]), job_id),
        )
    else:
        db.execute(
            """
            UPDATE chat_jobs SET started_at = ?, progress_log = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (started_at, json.dumps(events[-200:]), job_id),
        )


def record_project_job_progress(
    db: sqlite3.Connection,
    job_id: str,
    stage: str,
    current: int,
    total: int,
    file_path: str,
    symbol_name: str,
    file_current: int,
    file_total: int,
    function_current: int,
    function_total: int,
) -> None:
    job = db.execute(
        """
        SELECT started_at, paused_at, paused_seconds, progress_log
        FROM chat_jobs WHERE id = ?
        """,
        (job_id,),
    ).fetchone()
    if job is None:
        return
    now = int(time.time())
    started_at = int(job["started_at"] or now)
    events = decode_progress_log(job["progress_log"])
    key = (
        f"{stage}:{current}:{total}:{file_current}:{function_current}:"
        f"{symbol_name}"
    )
    if stage == "checking_calls":
        message = f"Checking call contracts {current} of {total}"
    else:
        compact_path = file_path.rstrip("/\\")
        compact_symbol = symbol_name.lstrip("/\\")
        message = (
            f"{compact_path}/{compact_symbol}"
            if compact_path and compact_symbol
            else compact_path or compact_symbol or "Project analysis"
        )
    if not events or (
        events[-1].get("key") != key
        and events[-1].get("message") != message
    ):
        events.append(
            {
                "key": key,
                "elapsed": active_job_elapsed_seconds(
                    started_at,
                    job["paused_at"],
                    job["paused_seconds"],
                    now=now,
                ),
                "message": message,
            }
        )
    db.execute(
        """
        UPDATE chat_jobs
        SET started_at = ?, progress_stage = ?, progress_current = ?, progress_total = ?,
            progress_file_path = ?, progress_symbol_name = ?,
            progress_file_current = ?, progress_file_total = ?,
            progress_function_current = ?, progress_function_total = ?,
            progress_log = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            started_at,
            stage,
            current,
            total,
            file_path[:1_000],
            symbol_name[:500],
            file_current,
            file_total,
            function_current,
            function_total,
            json.dumps(events[-200:]),
            job_id,
        ),
    )


def persist_failed_job_message(job_id: str, content: str) -> None:
    """Keep a terminal status visible after navigation for newly submitted chat jobs."""
    with connect_db() as db:
        job = db.execute(
            """
            SELECT user_id, chat_id, job_kind, user_message_id, reply_message_id
            FROM chat_jobs WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
        if (
            job is None
            or job["job_kind"] != "chat"
            or job["user_message_id"] is None
            or job["reply_message_id"] is not None
        ):
            return
        memory_id = f"user-{job['user_id']}:{job['chat_id']}"
        cursor = db.execute(
            "INSERT INTO messages(session_id, role, content) VALUES (?, 'assistant', ?)",
            (memory_id, content),
        )
        db.execute(
            "UPDATE chat_jobs SET reply_message_id = ? WHERE id = ?",
            (int(cursor.lastrowid), job_id),
        )
        db.execute(
            "UPDATE chat_histories SET updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            (job["chat_id"], job["user_id"]),
        )


def ollama_worker_loop() -> None:
    LOGGER.info("Dedicated Ollama job worker started")
    next_security_cleanup = time.monotonic() + SECURITY_CLEANUP_INTERVAL_SECONDS
    while not OLLAMA_WORKER_STOP.is_set() or not OLLAMA_JOB_QUEUE.empty():
        if time.monotonic() >= next_security_cleanup:
            try:
                run_database_maintenance()
            except (OSError, sqlite3.Error) as exc:
                LOGGER.error(
                "Periodic database maintenance failed",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
            next_security_cleanup = time.monotonic() + SECURITY_CLEANUP_INTERVAL_SECONDS
        try:
            job_id = OLLAMA_JOB_QUEUE.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            with JOB_CANCEL_LOCK:
                cancel_event = JOB_CANCEL_EVENTS.setdefault(job_id, threading.Event())
                if OLLAMA_WORKER_STOP.is_set():
                    cancel_event.set()
            process_chat_job(job_id, cancel_event)
        except Exception as exc:
            LOGGER.error(
                "Unhandled failure in Ollama job worker for %s",
                job_id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            with connect_db() as db:
                db.execute(
                    """
                    UPDATE chat_jobs SET status = 'failed', message = NULL,
                        error = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status IN ('queued', 'processing')
                    """,
                    (f"{type(exc).__name__}: {exc}", job_id),
                )
                record_chat_job_progress(db, job_id, "failed", update_stage=False)
            persist_failed_job_message(job_id, f"Error: {type(exc).__name__}: {exc}")
            with JOB_CANCEL_LOCK:
                JOB_CANCEL_EVENTS.pop(job_id, None)
                JOB_PAUSE_EVENTS.pop(job_id, None)
        finally:
            with JOB_CANCEL_LOCK:
                JOB_CANCEL_EVENTS.pop(job_id, None)
                JOB_PAUSE_EVENTS.pop(job_id, None)
            OLLAMA_JOB_QUEUE.task_done()
    LOGGER.info("Dedicated Ollama job worker stopped")


def start_ollama_worker() -> None:
    global OLLAMA_WORKER_THREAD
    with OLLAMA_WORKER_LIFECYCLE_LOCK:
        if OLLAMA_WORKER_THREAD is not None and OLLAMA_WORKER_THREAD.is_alive():
            return
        while True:
            try:
                OLLAMA_JOB_QUEUE.get_nowait()
            except queue.Empty:
                break
            else:
                OLLAMA_JOB_QUEUE.task_done()
        OLLAMA_WORKER_STOP.clear()
        OLLAMA_WORKER_THREAD = threading.Thread(
            target=ollama_worker_loop,
            name="ollama-job-worker",
            daemon=True,
        )
        OLLAMA_WORKER_THREAD.start()


def stop_ollama_worker() -> None:
    global OLLAMA_WORKER_THREAD
    with OLLAMA_WORKER_LIFECYCLE_LOCK:
        worker = OLLAMA_WORKER_THREAD
        if worker is None:
            return
        OLLAMA_WORKER_STOP.set()
        with JOB_CANCEL_LOCK:
            for cancel_event in JOB_CANCEL_EVENTS.values():
                cancel_event.set()
    worker.join(timeout=5)
    with OLLAMA_WORKER_LIFECYCLE_LOCK:
        if not worker.is_alive() and OLLAMA_WORKER_THREAD is worker:
            OLLAMA_WORKER_THREAD = None


def periodic_backup_worker_loop() -> None:
    LOGGER.info("Periodic database-backup worker started")
    try:
        delay = seconds_until_periodic_backup()
    except OSError as exc:
        LOGGER.error("Could not inspect periodic backups: %s", exc)
        delay = min(300, PERIODIC_BACKUP_INTERVAL_SECONDS)
    while not MAINTENANCE_STOP.wait(delay):
        try:
            create_periodic_database_backup()
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            LOGGER.error(
                "Periodic database backup failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            delay = min(300, PERIODIC_BACKUP_INTERVAL_SECONDS)
        else:
            delay = PERIODIC_BACKUP_INTERVAL_SECONDS
    LOGGER.info("Periodic database-backup worker stopped")


def start_periodic_backup_worker() -> None:
    global MAINTENANCE_THREAD
    if not CREATE_PERIODIC_BACKUPS:
        return
    with MAINTENANCE_LIFECYCLE_LOCK:
        if MAINTENANCE_THREAD is not None and MAINTENANCE_THREAD.is_alive():
            return
        MAINTENANCE_STOP.clear()
        MAINTENANCE_THREAD = threading.Thread(
            target=periodic_backup_worker_loop,
            name="periodic-database-backup-worker",
            daemon=True,
        )
        MAINTENANCE_THREAD.start()


def stop_periodic_backup_worker() -> None:
    global MAINTENANCE_THREAD
    with MAINTENANCE_LIFECYCLE_LOCK:
        worker = MAINTENANCE_THREAD
        if worker is None:
            return
        MAINTENANCE_STOP.set()
    worker.join(timeout=5)
    with MAINTENANCE_LIFECYCLE_LOCK:
        if not worker.is_alive() and MAINTENANCE_THREAD is worker:
            MAINTENANCE_THREAD = None


def process_chat_job(job_id: str, cancel_event: threading.Event) -> None:
    LOGGER.info("Ollama worker is starting chat job %s", job_id)
    with connect_db() as db:
        job = db.execute(
            "SELECT job_kind FROM chat_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    if job is not None and job["job_kind"] == "project_analysis":
        process_project_analysis_job(job_id, cancel_event)
    else:
        process_active_chat_job(job_id, cancel_event)


def process_project_analysis_job(job_id: str, cancel_event: threading.Event) -> None:
    with JOB_CANCEL_LOCK:
        pause_event = JOB_PAUSE_EVENTS.setdefault(job_id, threading.Event())
    with connect_db() as db:
        job = db.execute(
            """
            SELECT project_id, project_retry_failed, project_symbol_id,
                   cancel_requested
            FROM chat_jobs
            WHERE id = ? AND status = 'queued' AND job_kind = 'project_analysis'
            """,
            (job_id,),
        ).fetchone()
        if job is None or job["project_id"] is None:
            return
        project_id = str(job["project_id"])
        if job["cancel_requested"]:
            cancel_event.set()
        db.execute(
            "UPDATE chat_jobs SET status = 'processing', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (job_id,),
        )

    def wait_if_paused() -> None:
        if not pause_event.is_set():
            return
        with connect_db() as pause_db:
            record_chat_job_progress(pause_db, job_id, "paused", update_stage=True)
        while pause_event.is_set():
            if cancel_event.is_set():
                raise AnalysisCancelled("Analysis cancelled")
            time.sleep(0.25)
        with connect_db() as pause_db:
            record_chat_job_progress(pause_db, job_id, "resumed", update_stage=False)

    def update_progress(
        stage: str,
        current: int,
        total: int,
        file_path: str,
        symbol_name: str,
        file_current: int,
        file_total: int,
        function_current: int,
        function_total: int,
    ) -> None:
        with connect_db() as progress_db:
            record_project_job_progress(
                progress_db,
                job_id,
                stage,
                current,
                total,
                file_path,
                symbol_name,
                file_current,
                file_total,
                function_current,
                function_total,
            )

    try:
        summary = analyze_project_functions(
            connect_db,
            project_id,
            retry_failed=bool(job["project_retry_failed"]),
            selected_symbol_ids=(
                {int(job["project_symbol_id"])}
                if job["project_symbol_id"] is not None
                else None
            ),
            force_model=job["project_symbol_id"] is not None,
            cancel_check=cancel_event.is_set,
            pause_wait=wait_if_paused,
            progress_callback=update_progress,
        )
        if (
            summary.status != "cancelled"
            and not cancel_event.is_set()
            and job["project_symbol_id"] is None
        ):
            with connect_db() as db:
                call_count = int(
                    db.execute(
                        "SELECT COUNT(*) FROM project_calls WHERE project_id = ?",
                        (project_id,),
                    ).fetchone()[0]
                )
                last_progress = db.execute(
                    """
                    SELECT progress_file_path, progress_symbol_name,
                           progress_file_current, progress_file_total,
                           progress_function_current, progress_function_total
                    FROM chat_jobs WHERE id = ?
                    """,
                    (job_id,),
                ).fetchone()
            progress_values = (
                str(last_progress["progress_file_path"] or ""),
                str(last_progress["progress_symbol_name"] or ""),
                int(last_progress["progress_file_current"] or 0),
                int(last_progress["progress_file_total"] or 0),
                int(last_progress["progress_function_current"] or 0),
                int(last_progress["progress_function_total"] or 0),
            )
            update_progress(
                "checking_calls", 0, call_count, *progress_values
            )
            with connect_db() as db:
                compatibility = check_project_call_compatibility(db, project_id)
            update_progress(
                "checking_calls",
                compatibility.checked_count,
                compatibility.checked_count,
                *progress_values,
            )
    except Exception as exc:
        LOGGER.error(
            "Background project-analysis job %s failed",
            job_id,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        with connect_db() as db:
            error_text = f"{type(exc).__name__}: {exc}"[:1_000]
            db.execute(
                """
                UPDATE chat_jobs SET status = 'failed', error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (error_text, job_id),
            )
            db.execute(
                """
                UPDATE projects
                SET function_analysis_status = 'failed', function_analysis_error = ?,
                    function_analysis_updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND function_analysis_status = 'running'
                """,
                (error_text, project_id),
            )
            db.execute(
                """
                UPDATE projects
                SET call_compatibility_status = 'failed', call_compatibility_error = ?,
                    call_compatibility_updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND function_analysis_status != 'running'
                """,
                (error_text, project_id),
            )
            record_chat_job_progress(db, job_id, "failed", update_stage=False)
        return
    if summary.status == "cancelled" or cancel_event.is_set():
        with connect_db() as db:
            db.execute(
                """
                UPDATE chat_jobs SET status = 'failed', error = 'Cancelled by user.',
                    updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (job_id,),
            )
            record_chat_job_progress(db, job_id, "cancelled", update_stage=False)
        return
    with connect_db() as db:
        db.execute(
            """
            UPDATE chat_jobs SET status = 'completed', error = NULL,
                updated_at = CURRENT_TIMESTAMP WHERE id = ?
            """,
            (job_id,),
        )
        record_chat_job_progress(
            db, job_id, "completed" if summary.status == "completed" else "review_incomplete",
            summary.completed_count, summary.total_count, update_stage=False,
        )


def process_active_chat_job(job_id: str, cancel_event: threading.Event) -> None:
    with connect_db() as db:
        job = db.execute(
            """
            SELECT user_id, chat_id, message, mode, cancel_requested,
                   progress_stage, progress_current, progress_total,
                   job_kind, user_message_id, reply_message_id
            FROM chat_jobs WHERE id = ? AND status = 'queued'
            """,
            (job_id,),
        ).fetchone()
        if job is None:
            with JOB_CANCEL_LOCK:
                JOB_CANCEL_EVENTS.pop(job_id, None)
            return
        if job["cancel_requested"]:
            cancel_event.set()
        db.execute(
            "UPDATE chat_jobs SET status = 'processing', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (job_id,),
        )
    LOGGER.info("Background chat job %s acquired Ollama", job_id)

    def update_progress(stage: str, current: int, total: int) -> None:
        with connect_db() as progress_db:
            record_chat_job_progress(progress_db, job_id, stage, current, total)

    try:
        if cancel_event.is_set():
            raise AnalysisCancelled("Analysis cancelled by user")
        if job["job_kind"] == "verification_retry":
            if job["user_message_id"] is None:
                raise RuntimeError("Verification retry has no stored source message")
            result = perform_verification_retry(
                job["user_id"],
                job["chat_id"],
                int(job["user_message_id"]),
                int(job["reply_message_id"]) if job["reply_message_id"] is not None else None,
                cancel_event.is_set,
                update_progress,
            )
        else:
            if not isinstance(job["message"], str):
                raise RuntimeError("Chat job has no pending message")
            result = perform_chat(
                job["user_id"],
                job["chat_id"],
                job["message"],
                job["mode"],
                int(job["user_message_id"]) if job["user_message_id"] is not None else None,
                cancel_event.is_set,
                update_progress,
            )
    except AnalysisCancelled:
        with connect_db() as db:
            reason_row = db.execute(
                "SELECT cancel_reason FROM chat_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            cancel_reason = (
                reason_row["cancel_reason"]
                if reason_row is not None and reason_row["cancel_reason"]
                else "Cancelled by user."
            )
            db.execute(
                """
                UPDATE chat_jobs SET status = 'failed', message = NULL,
                    error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (cancel_reason, job_id),
            )
            record_chat_job_progress(db, job_id, "cancelled", update_stage=False)
        failure_message = (
            "Process cancelled by an administrator."
            if cancel_reason == "Cancelled by an administrator."
            else "Process cancelled by user."
        )
        persist_failed_job_message(job_id, failure_message)
        LOGGER.info("Background chat job %s was cancelled", job_id)
        with JOB_CANCEL_LOCK:
            JOB_CANCEL_EVENTS.pop(job_id, None)
        return
    except Exception as exc:
        LOGGER.error(
            "Background chat job %s failed",
            job_id,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        with connect_db() as db:
            error_text = f"{type(exc).__name__}: {exc}"
            db.execute(
                """
                UPDATE chat_jobs SET status = 'failed', message = NULL, error = ?,
                    updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (error_text, job_id),
            )
            record_chat_job_progress(db, job_id, "failed", update_stage=False)
        persist_failed_job_message(job_id, f"Error: {error_text}")
        with JOB_CANCEL_LOCK:
            JOB_CANCEL_EVENTS.pop(job_id, None)
        return
    with connect_db() as db:
        record_chat_job_progress(db, job_id, "completed", update_stage=False)
        db.execute(
            """
            UPDATE chat_jobs SET status = 'completed', message = NULL, reply = ?, title = ?,
                reply_message_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?
            """,
            (result.reply, result.title, result.reply_message_id, job_id),
        )
    LOGGER.info("Background chat job %s completed", job_id)
    with JOB_CANCEL_LOCK:
        JOB_CANCEL_EVENTS.pop(job_id, None)


@app.post("/api/chat", response_model=None)
def chat(payload: ChatRequest, request: Request) -> ChatResponse | JSONResponse:
    user = require_user(request)
    require_ai_work_enabled()
    job_id = str(uuid.uuid4())
    is_large_input = len(payload.message) > DIRECT_MESSAGE_CHARS
    uses_sections = payload.mode == "analyse" or is_large_input
    progress_stage = (
        "analyzing"
        if payload.mode == "analyse"
        else "processing"
        if is_large_input
        else "thinking"
    )
    progress_total = (
        len(split_large_text(payload.message, overlap=LARGE_CHUNK_OVERLAP_CHARS))
        if uses_sections
        else 0
    )
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        require_ai_work_enabled(db)
        chat_row = db.execute(
            """
            SELECT title, title_is_custom FROM chat_histories
            WHERE id = ? AND user_id = ?
            """,
            (payload.chat_id, user["id"]),
        ).fetchone()
        if chat_row is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        active = db.execute(
            """
            SELECT id FROM chat_jobs
            WHERE chat_id = ? AND user_id = ? AND status IN ('queued', 'processing')
            LIMIT 1
            """,
            (payload.chat_id, user["id"]),
        ).fetchone()
        if active is not None:
            raise HTTPException(
                status_code=409,
                detail="This chat already has a request in progress",
            )
        enforce_job_admission(
            db,
            int(user["id"]),
            len(payload.message),
            storage_bytes_to_add=len(payload.message.encode("utf-8")),
        )
        memory_id = f"user-{user['id']}:{payload.chat_id}"
        had_user_message = db.execute(
            "SELECT 1 FROM messages WHERE session_id = ? AND role = 'user' LIMIT 1",
            (memory_id,),
        ).fetchone() is not None
        cursor = db.execute(
            """
            INSERT INTO messages(session_id, role, content)
            VALUES (?, 'user', ?)
            """,
            (memory_id, payload.message),
        )
        user_message_id = int(cursor.lastrowid)
        title = chat_row["title"]
        if not had_user_message and not chat_row["title_is_custom"]:
            title = title_from_message(payload.message)
            db.execute(
                "UPDATE chat_histories SET title = ? WHERE id = ? AND user_id = ?",
                (title, payload.chat_id, user["id"]),
            )
        db.execute(
            "UPDATE chat_histories SET updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            (payload.chat_id, user["id"]),
        )
        insert_chat_job(
            db,
            """
            INSERT INTO chat_jobs(
                id, user_id, chat_id, message, mode, job_kind, user_message_id,
                status, progress_stage, progress_total, started_at
            )
            VALUES (?, ?, ?, ?, ?, 'chat', ?, 'queued', ?, ?, ?)
            """,
            (
                job_id,
                user["id"],
                payload.chat_id,
                payload.message,
                payload.mode,
                user_message_id,
                progress_stage,
                progress_total,
                int(time.time()),
            ),
            int(user["id"]),
            payload.chat_id,
        )
        record_chat_job_progress(db, job_id, "queued", update_stage=False)
    with JOB_CANCEL_LOCK:
        JOB_CANCEL_EVENTS[job_id] = threading.Event()
        JOB_PAUSE_EVENTS[job_id] = threading.Event()
    enqueue_ollama_job(job_id)
    return JSONResponse(
        status_code=202,
        content={
            "status": "queued",
            "job_id": job_id,
            "chat_id": payload.chat_id,
            "title": title,
            "job_kind": "chat",
            "mode": payload.mode,
            "progress_stage": progress_stage,
            "progress_current": 0,
            "progress_total": progress_total,
        },
    )


@app.get("/api/chat-jobs/{job_id}")
def chat_job(job_id: str, request: Request) -> dict[str, object]:
    user = require_user(request)
    with connect_db() as db:
        job = db.execute(
            """
            SELECT job.status, job.chat_id,
                   COALESCE(job.title, history.title) AS title,
                   COALESCE(job.reply, stored_reply.content) AS reply,
                   job.error, job.progress_stage, job.progress_current,
                   job.progress_total, job.job_kind, job.mode, job.project_id,
                   job.project_symbol_id,
                   job.progress_file_path, job.progress_symbol_name,
                   job.progress_file_current, job.progress_file_total,
                   job.progress_function_current, job.progress_function_total,
                   job.started_at, job.paused_at, job.paused_seconds,
                   job.progress_log,
                   project.name AS project_name,
                   project.function_analysis_status,
                   project.function_analysis_total_count,
                   project.function_analysis_completed_count,
                   project.function_analysis_failed_count,
                   project.function_analysis_skipped_count,
                   project.function_analysis_cache_hit_count,
                   project.function_analysis_model_request_count,
                   project.function_analysis_batch_request_count,
                   project.function_analysis_deterministic_count,
                   project.function_analysis_batch_fallback_count,
                   project.function_analysis_batch_error,
                   project.call_compatibility_status,
                   project.call_compatibility_checked_count,
                   project.call_compatibility_incompatible_count,
                   project.call_compatibility_unknown_count,
                   project.call_compatibility_not_checked_count
            FROM chat_jobs AS job
            LEFT JOIN chat_histories AS history
              ON history.id = job.chat_id AND history.user_id = job.user_id
            LEFT JOIN messages AS stored_reply
              ON stored_reply.id = job.reply_message_id
             AND stored_reply.session_id = ('user-' || job.user_id || ':' || job.chat_id)
             AND stored_reply.role = 'assistant'
            LEFT JOIN projects AS project
              ON project.id = job.project_id AND project.user_id = job.user_id
            WHERE job.id = ? AND job.user_id = ?
            """,
            (job_id, user["id"]),
        ).fetchone()
    if job is None:
        raise HTTPException(status_code=404, detail="Chat job not found")
    result: dict[str, object] = {
        "status": job["status"],
        "chat_id": job["chat_id"],
        "job_kind": job["job_kind"],
        "mode": job["mode"],
        "progress_stage": job["progress_stage"],
        "progress_current": job["progress_current"],
        "progress_total": job["progress_total"],
        "project_id": job["project_id"],
        "project_symbol_id": job["project_symbol_id"],
        "description_only": job["project_symbol_id"] is not None,
        "project_name": job["project_name"],
        "progress_file_path": job["progress_file_path"],
        "progress_symbol_name": job["progress_symbol_name"],
        "progress_file_current": job["progress_file_current"],
        "progress_file_total": job["progress_file_total"],
        "progress_function_current": job["progress_function_current"],
        "progress_function_total": job["progress_function_total"],
        "progress_log": decode_progress_log(job["progress_log"]),
        "elapsed_seconds": active_job_elapsed_seconds(
            job["started_at"],
            job["paused_at"],
            job["paused_seconds"],
        ),
    }
    if job["job_kind"] == "project_analysis":
        result["project"] = {
            "id": job["project_id"],
            "name": job["project_name"],
            "function_analysis_status": job["function_analysis_status"],
            "function_analysis_total_count": job["function_analysis_total_count"],
            "function_analysis_completed_count": job["function_analysis_completed_count"],
            "function_analysis_failed_count": job["function_analysis_failed_count"],
            "function_analysis_skipped_count": job["function_analysis_skipped_count"],
            "function_analysis_cache_hit_count": job["function_analysis_cache_hit_count"],
            "function_analysis_model_request_count": job["function_analysis_model_request_count"],
            "function_analysis_batch_request_count": job["function_analysis_batch_request_count"],
            "function_analysis_deterministic_count": job["function_analysis_deterministic_count"],
            "function_analysis_batch_fallback_count": job["function_analysis_batch_fallback_count"],
            "function_analysis_batch_error": job["function_analysis_batch_error"],
            "call_compatibility_status": job["call_compatibility_status"],
            "call_compatibility_checked_count": job["call_compatibility_checked_count"],
            "call_compatibility_incompatible_count": job["call_compatibility_incompatible_count"],
            "call_compatibility_unknown_count": job["call_compatibility_unknown_count"],
            "call_compatibility_not_checked_count": job["call_compatibility_not_checked_count"],
        }
        result["project"].update(call_compatibility_coverage(result["project"]))
        if job["status"] == "completed":
            return result
    if job["status"] == "completed":
        if not isinstance(job["reply"], str):
            LOGGER.error("Completed chat job %s has no reply", job_id)
            return {
                "status": "failed",
                "error": "The request completed without returning a reply.",
            }
        result.update({"chat_id": job["chat_id"], "title": job["title"], "reply": job["reply"]})
    elif job["status"] == "failed":
        result["error"] = job["error"] or "Analysis failed"
    return result


@app.post("/api/chat-jobs/{job_id}/cancel")
def cancel_chat_job(job_id: str, request: Request) -> dict[str, str]:
    user = require_user(request)
    cancelled_while_queued = False
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        job = db.execute(
            "SELECT status, job_kind, project_id FROM chat_jobs WHERE id = ? AND user_id = ?",
            (job_id, user["id"]),
        ).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="Chat job not found")
        if job["status"] in {"queued", "processing"}:
            db.execute(
                """
                UPDATE chat_jobs SET cancel_requested = 1,
                    cancel_reason = 'Cancelled by user.', updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ?
                """,
                (job_id, user["id"]),
            )
            if job["status"] == "queued":
                cursor = db.execute(
                    """
                    UPDATE chat_jobs SET status = 'failed', message = NULL,
                        error = 'Cancelled by user.', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND user_id = ? AND status = 'queued'
                    """,
                    (job_id, user["id"]),
                )
                cancelled_while_queued = cursor.rowcount > 0
                if cancelled_while_queued:
                    record_chat_job_progress(
                        db, job_id, "cancelled", update_stage=False
                    )
                    if job["project_id"] is not None:
                        refresh_project_function_analysis(db, str(job["project_id"]))
    with JOB_CANCEL_LOCK:
        event = JOB_CANCEL_EVENTS.get(job_id)
        if event:
            event.set()
    if cancelled_while_queued:
        persist_failed_job_message(job_id, "Process cancelled by user.")
        return {"status": "cancelled"}
    return {"status": "cancellation_requested"}


@app.post("/api/chat-jobs/{job_id}/pause")
def pause_chat_job(job_id: str, request: Request) -> dict[str, object]:
    user = require_user(request)
    now = int(time.time())
    with connect_db() as db:
        job = db.execute(
            """
            SELECT status, job_kind, started_at, paused_at, paused_seconds
            FROM chat_jobs
            WHERE id = ? AND user_id = ?
            """,
            (job_id, user["id"]),
        ).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="Chat job not found")
        if job["job_kind"] != "project_analysis":
            raise HTTPException(
                status_code=400,
                detail="Only project-analysis jobs can be paused",
            )
        if job["status"] not in {"queued", "processing"}:
            raise HTTPException(status_code=409, detail="This job is not active")
        paused_at = int(job["paused_at"]) if job["paused_at"] is not None else now
        if job["paused_at"] is None:
            db.execute(
                "UPDATE chat_jobs SET paused_at = ? WHERE id = ?",
                (paused_at, job_id),
            )
        record_chat_job_progress(db, job_id, "paused", update_stage=True)
        elapsed_seconds = active_job_elapsed_seconds(
            job["started_at"],
            paused_at,
            job["paused_seconds"],
            now=now,
        )
    with JOB_CANCEL_LOCK:
        JOB_PAUSE_EVENTS.setdefault(job_id, threading.Event()).set()
    return {"status": "paused", "elapsed_seconds": elapsed_seconds}


@app.post("/api/chat-jobs/{job_id}/resume")
def resume_chat_job(job_id: str, request: Request) -> dict[str, object]:
    user = require_user(request)
    now = int(time.time())
    with connect_db() as db:
        job = db.execute(
            """
            SELECT status, job_kind, started_at, paused_at, paused_seconds
            FROM chat_jobs
            WHERE id = ? AND user_id = ?
            """,
            (job_id, user["id"]),
        ).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="Chat job not found")
        if job["job_kind"] != "project_analysis":
            raise HTTPException(
                status_code=400,
                detail="Only project-analysis jobs can be resumed",
            )
        if job["status"] not in {"queued", "processing"}:
            raise HTTPException(status_code=409, detail="This job is not active")
        paused_seconds = max(0, int(job["paused_seconds"] or 0))
        if job["paused_at"] is not None:
            paused_seconds += max(0, now - int(job["paused_at"]))
            db.execute(
                """
                UPDATE chat_jobs
                SET paused_at = NULL, paused_seconds = ?
                WHERE id = ?
                """,
                (paused_seconds, job_id),
            )
        record_chat_job_progress(db, job_id, "resumed", update_stage=False)
        db.execute(
            "UPDATE chat_jobs SET progress_stage = 'resumed' WHERE id = ?",
            (job_id,),
        )
        elapsed_seconds = active_job_elapsed_seconds(
            job["started_at"],
            None,
            paused_seconds,
            now=now,
        )
    with JOB_CANCEL_LOCK:
        JOB_PAUSE_EVENTS.setdefault(job_id, threading.Event()).clear()
    return {"status": "resumed", "elapsed_seconds": elapsed_seconds}


from web_assets import (
    ADMIN_HTML,
    CHANGELOG_HTML,
    EXPIRED_HTML,
    FORBIDDEN_HTML,
    FORGOT_PASSWORD_HTML,
    HTML,
    LOGIN_HTML,
    REGISTER_HTML,
    REGISTRATION_CLOSED_HTML,
    RESET_EXPIRED_HTML,
    RESET_PASSWORD_HTML,
    VERIFY_HTML,
)

def start_ngrok() -> str | None:
    token = os.getenv("NGROK_AUTHTOKEN")
    if not token:
        return None
    try:
        from pyngrok import ngrok
    except ImportError as exc:
        raise RuntimeError("NGROK_AUTHTOKEN is set, but pyngrok is not installed") from exc
    ngrok.set_auth_token(token)
    tunnel = ngrok.connect(PORT, "http", bind_tls=True)
    return tunnel.public_url


if __name__ == "__main__":
    start_ntfy_error_notifier()
    try:
        initialise_db()
        public_url = start_ngrok()
        if public_url:
            PUBLIC_BASE_URL = public_url.rstrip("/")
            add_public_origin_to_trusted_hosts(PUBLIC_BASE_URL)
        validate_runtime_configuration()
        print(f"Local chat:  http://127.0.0.1:{PORT}")
        print(
            f"Large inputs: up to {MAX_MESSAGE_CHARS:,} characters; "
            f"Ollama chat context: {OLLAMA_CONTEXT_SIZE:,} tokens"
        )
        if OLLAMA_ADAPTIVE_ANALYSIS_CONTEXT:
            print(f"Function analysis context: adaptive {OLLAMA_ANALYSIS_CONTEXT_MIN:,}–{OLLAMA_ANALYSIS_CONTEXT_MAX:,} tokens")
        else:
            print(f"Function analysis context: fixed {OLLAMA_CONTEXT_SIZE:,} tokens")
        if public_url:
            print(f"Public chat: {public_url}")
        else:
            print("ngrok disabled: set NGROK_AUTHTOKEN to create a public URL")
        if not SMTP_HOST or not SMTP_FROM:
            print("WARNING: registration email is disabled until SMTP_HOST and SMTP_FROM are set.")
        # The correlated middleware above is the single HTTP access log. Uvicorn's
        # second access stream would duplicate every request, including two-second polls.
        uvicorn.run(app, host=HOST, port=PORT, access_log=False)
    except Exception:
        LOGGER.exception("Application terminated unexpectedly")
        raise
    finally:
        stop_ntfy_error_notifier()
